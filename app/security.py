"""Public-only URL validation and IP-pinned HTTP. No proxy or second DNS lookup."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin


def normalize_domain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("Geçerli bir public domain girin.")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Domain boşluk veya ters eğik çizgi içeremez.")
    parts = urlsplit(value if "://" in value else "https://" + value)
    if (parts.scheme not in ("http", "https") or parts.username is not None
            or parts.password is not None or parts.port is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or "?" in value or "#" in value):
        raise ValueError("Yalnız domain girin; yol, port, kullanıcı bilgisi eklemeyin.")
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    if not host or len(host) > 253 or host.endswith(".") or "." not in host:
        raise ValueError("Geçerli bir public domain girin.")
    if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise ValueError("Yerel veya özel domain taranamaz.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP adresi yerine public domain girin.")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in host.split(".")) or host.split(".")[-1].isdigit():
        raise ValueError("Domain biçimi geçersiz.")
    return host


def public_addresses(host, port):
    if port not in (80, 443):
        raise ValueError("Yalnız standart HTTP/HTTPS portlarına izin verilir.")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Özel, yerel veya ayrılmış IP adresine erişim engellendi.")
    return addresses


def validate_url(url):
    if not isinstance(url, str) or len(url) > 4096 or "\\" in url or any(ord(c) < 33 for c in url):
        raise ValueError("Geçersiz hedef URL.")
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("HTTP/HTTPS dışı veya kullanıcı bilgili URL engellendi.")
    port = p.port or (443 if p.scheme == "https" else 80)
    if port != (443 if p.scheme == "https" else 80):
        raise ValueError("Özel port engellendi.")
    return p, port


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        # Numeric address only: the validated hostname is never resolved again.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.secure:
            try:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
            except Exception:
                self.sock.close()
                raise


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes


def request(url, method="GET", headers=None, body=None, timeout=12, follow=True, max_bytes=2_000_000):
    deadline = time.monotonic() + timeout
    headers = {"User-Agent": "Pickable/0.1", "Accept-Encoding": "identity", **(headers or {})}
    body = body.encode() if isinstance(body, str) else body
    for _ in range(6):
        p, port = validate_url(url)
        host = p.hostname.encode("idna").decode("ascii")
        ips = public_addresses(host, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("İstek zaman aşımına uğradı.")
        connection = PinnedConnection(host, port, ips[0], p.scheme == "https", remaining)
        try:
            connection.request(method, (p.path or "/") + ("?" + p.query if p.query else ""), body=body, headers=headers)
            response = connection.getresponse()
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            if follow and response.status in (301, 302, 303, 307, 308) and response_headers.get("location"):
                target = urljoin(url, response_headers["location"])
                new, _ = validate_url(target)
                if p.scheme == "https" and new.scheme != "https":
                    raise ValueError("HTTPS'ten HTTP'ye yönlendirme engellendi.")
                if (new.scheme, new.netloc) != (p.scheme, p.netloc):
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie")}
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                url = target
                continue
            data = bytearray()
            while len(data) <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Yanıt zaman aşımına uğradı.")
                # The response owns a buffered socket file, including Connection: close.
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError("Sağlayıcı yanıt boyutu sınırı aşıldı.")
            return Response(url, response.status, response_headers, bytes(data))
        finally:
            connection.close()
    raise ValueError("Yönlendirme sınırı aşıldı.")


def request_json(url, **options):
    response = request(url, **options)
    if response.status not in (200, 202):
        retry = response.headers.get("retry-after")
        suffix = f"; Retry-After: {retry}" if retry else ""
        raise ValueError(f"Sağlayıcı HTTP {response.status}{suffix}")
    data = json.loads(response.body)
    if not isinstance(data, dict):
        raise ValueError("Sağlayıcı geçerli bir JSON nesnesi döndürmedi.")
    return data
