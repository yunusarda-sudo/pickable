"""Public-only URL validation and IP-pinned HTTP. No proxy or second DNS lookup.

SSRF policy (IP safety, URL vetting, DNS verification) is canonical in
app.ssrf. This module keeps the historical ValueError-based public API;
the actual HTTP round-trip delegates to app.safe_fetch so redirect,
pinning and size-limit behaviour cannot diverge.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, parse_qsl

from app.ssrf import is_ip_safe as _ip_safe
from app.ssrf import resolve_and_verify as _resolve
from app.ssrf import validate_url as _canonical_validate


def normalize_domain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("Enter a valid public domain.")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Domain cannot contain spaces or backslashes.")
    parts = urlsplit(value if "://" in value else "https://" + value)
    if (parts.scheme not in ("http", "https") or parts.username is not None
            or parts.password is not None or parts.port is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or "?" in value or "#" in value):
        raise ValueError("Enter just the domain; no path, port or credentials.")
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    if not host or len(host) > 253 or host.endswith(".") or "." not in host:
        raise ValueError("Enter a valid public domain.")
    if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise ValueError("Local or private domains cannot be scanned.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a public domain instead of an IP address.")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in host.split(".")) or host.split(".")[-1].isdigit():
        raise ValueError("Invalid domain format.")
    return host


def normalize_site_url(value):
    """Accept a public site URL including a path; return its normalized host and fetch URL."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("Enter a valid public website URL.")
    value = value.strip()
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Website URL contains invalid characters.")
    candidate = value if "://" in value else "https://" + value
    parts, _ = validate_url(candidate)
    sensitive_query_names = {"token", "accesstoken", "refreshtoken", "idtoken", "secret", "password", "passwd",
                             "auth", "authorization", "bearer", "apikey", "accesskey", "clientsecret",
                             "code", "session", "signature", "sig"}
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
        if normalized_key in sensitive_query_names:
            raise ValueError("Remove credentials or authentication parameters from the website URL.")
    host = normalize_domain(parts.hostname)
    target = urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    return host, target


def public_addresses(host, port):
    """Resolve once; require every answer to be public. Raises ValueError."""
    return _resolve(host, port)


def validate_url(url):
    """Parse and vet an http(s) URL. Canonical logic lives in app.ssrf."""
    return _canonical_validate(url)


class PinnedConnection(http.client.HTTPConnection):
    """Kept for backward compatibility; new code paths use app.safe_fetch.

    Previously this pinned the TCP socket to a pre-resolved IP. The live
    implementation now lives in app.safe_fetch (single redirect/size-limit
    policy); this class is no longer used internally.
    """

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
    """SSRF-pinned HTTP request. Delegates to app.safe_fetch (single policy).

    Keeps the historical signature and ValueError/TimeoutError contract;
    redirect, DNS-pinning and size-limit behaviour are defined once in
    app.safe_fetch so the two clients cannot diverge.
    """
    from app.safe_fetch import (
        SafeFetchError,
        TimeoutError as FetchTimeout,
        safe_fetch,
    )

    merged = {"User-Agent": "Pickable/0.1", "Accept-Encoding": "identity", **(headers or {})}
    payload = body.encode() if isinstance(body, str) else body
    try:
        result = safe_fetch(
            url,
            method=method,
            headers=merged,
            body=payload,
            timeout=timeout,
            max_redirects=5 if follow else 0,
            max_bytes=max_bytes,
        )
    except FetchTimeout as e:
        raise TimeoutError(str(e)) from e
    except SafeFetchError as e:
        raise ValueError(str(e)) from e
    return Response(
        url=result.final_url or url,
        status=result.status,
        headers={k.lower(): v for k, v in result.headers.items()},
        body=result.body,
    )


def request_json(url, **options):
    response = request(url, **options)
    if response.status not in (200, 202):
        retry = response.headers.get("retry-after")
        suffix = f"; Retry-After: {retry}" if retry else ""
        raise ValueError(f"Provider returned HTTP {response.status}{suffix}")
    data = json.loads(response.body)
    if not isinstance(data, dict):
        raise ValueError("Provider did not return a valid JSON object.")
    return data
