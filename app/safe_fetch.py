"""
SSRF-protected HTTP client (Standard Library only).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class SafeFetchError(Exception):
    """Base exception for safe_fetch errors."""


class SSRFBlocked(SafeFetchError):
    """Raised when an IP or URL scheme violates SSRF safety rules."""


class TimeoutError(SafeFetchError):
    """Raised when request times out."""


class TooManyRedirects(SafeFetchError):
    """Raised when maximum redirects count is exceeded."""


class ResponseTooLarge(SafeFetchError):
    """Raised when response body exceeds max_bytes limit."""


@dataclass
class FetchResult:
    status: int
    headers: Dict[str, str]
    body: bytes
    redirect_chain: List[str] = field(default_factory=list)
    resolved_ips: List[str] = field(default_factory=list)
    final_url: str = ""


def is_ip_safe(ip_str: str) -> bool:
    """
    Validates if an IP address is a safe public IP.
    Blocks:
    - RFC 1918 (10/8, 172.16/12, 192.168/16)
    - Loopback (127/8, ::1)
    - Link-local (169.254/16, fe80::/10)
    - Cloud metadata (169.254.169.254)
    - IPv6 ULA (fc00::/7)
    - Unspecified / broadcast / multicast / reserved
    - IPv4 mapped IPv6 addresses pointing to private ranges
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Check IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False

    # Explicit cloud metadata check
    if str(ip) == "169.254.169.254":
        return False

    return True


def resolve_and_verify(hostname: str, port: int) -> List[str]:
    """
    Resolves hostname via socket.getaddrinfo once.
    Verifies that all resolved IPs are safe and public.
    Returns list of verified IP strings.
    """
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFBlocked(f"DNS resolution failed for {hostname}: {e}") from e

    if not addr_infos:
        raise SSRFBlocked(f"No IP addresses resolved for {hostname}")

    verified_ips: List[str] = []
    for info in addr_infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if not is_ip_safe(ip_str):
            raise SSRFBlocked(f"SSRF blocked: IP {ip_str} for {hostname} is private or restricted")
        if ip_str not in verified_ips:
            verified_ips.append(ip_str)

    return verified_ips


def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 10,
    max_redirects: int = 5,
    max_bytes: int = 10_000_000,
) -> FetchResult:
    """
    SSRF-protected HTTP client.
    1. URL scheme verification (only http/https).
    2. Resolves hostname DNS (socket.getaddrinfo) once.
    3. Verifies ALL resolved IPs are public.
    4. Pins IP: HTTP connection is made directly to the resolved IP with Host header.
    5. Reads response up to max_bytes.
    6. Follows up to max_redirects safely.
    """
    current_url = url
    redirect_chain: List[str] = []
    all_resolved_ips: List[str] = []

    for redirect_step in range(max_redirects + 1):
        parsed = urllib.parse.urlsplit(current_url)

        # 1. Scheme check
        if parsed.scheme.lower() not in ("http", "https"):
            raise SSRFBlocked(f"Disallowed scheme: '{parsed.scheme}'. Only http and https are permitted.")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFBlocked(f"Invalid URL without hostname: {current_url}")
        if parsed.username is not None or parsed.password is not None:
            raise SSRFBlocked("URLs with embedded credentials are blocked.")

        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        if port != (443 if parsed.scheme.lower() == "https" else 80):
            raise SSRFBlocked("Non-standard destination ports are blocked.")

        # 2 & 3. DNS resolution & IP safety check
        verified_ips = resolve_and_verify(hostname, port)
        all_resolved_ips.extend(verified_ips)

        # 4. Pin IP: Connect directly to the first resolved IP
        pinned_ip = verified_ips[0]

        req_headers = dict(headers or {})
        # Ensure Host header is preserved
        if "Host" not in req_headers and "host" not in req_headers:
            req_headers["Host"] = hostname if (parsed.port is None) else f"{hostname}:{parsed.port}"

        conn: http.client.HTTPConnection
        try:
            if parsed.scheme.lower() == "https":
                context = ssl.create_default_context()
                # Connect to hostname for SNI and TLS certificate verification,
                # but pin the actual TCP socket connection to pinned_ip
                conn = http.client.HTTPSConnection(
                    hostname,
                    port=port,
                    timeout=timeout,
                    context=context,
                )
                conn._create_connection = (
                    lambda address, timeout=timeout, source_address=None, ip=pinned_ip, p=port:
                        socket.create_connection((ip, p), timeout, source_address)
                )
            else:
                conn = http.client.HTTPConnection(
                    pinned_ip,
                    port=port,
                    timeout=timeout,
                )

            req_path = parsed.path or "/"
            if parsed.query:
                req_path += f"?{parsed.query}"

            conn.request(method, req_path, body=body, headers=req_headers)
            resp = conn.getresponse()

            # Check status code for redirect
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    # Treat redirect without Location header as regular response
                    pass
                else:
                    if redirect_step >= max_redirects:
                        conn.close()
                        raise TooManyRedirects(f"Max redirects ({max_redirects}) exceeded.")

                    redirect_chain.append(current_url)
                    # Resolve relative redirect URLs against current URL
                    next_url = urllib.parse.urljoin(current_url, location)
                    next_parts = urllib.parse.urlsplit(next_url)
                    if parsed.scheme.lower() == "https" and next_parts.scheme.lower() != "https":
                        conn.close()
                        raise SSRFBlocked("HTTPS to HTTP redirects are blocked.")
                    if resp.status == 303 or (resp.status in (301, 302) and method.upper() == "POST"):
                        method, body = "GET", None
                        headers = {key: value for key, value in (headers or {}).items()
                                   if key.lower() not in ("content-type", "content-length", "transfer-encoding")}
                    if (next_parts.scheme, next_parts.netloc) != (parsed.scheme, parsed.netloc):
                        if body is not None:
                            conn.close()
                            raise SSRFBlocked("Request body cannot be forwarded to a different origin.")
                        # Do not forward caller-defined credentials (for example X-API-Key)
                        # to a different authority. Host is rebuilt from the redirect target.
                        req_headers = {key: value for key, value in req_headers.items()
                                       if key.lower() in ("user-agent", "accept", "accept-encoding")}
                        headers = req_headers
                    current_url = next_url
                    conn.close()
                    continue

            # Read response body with max_bytes limit
            content_length_header = resp.getheader("Content-Length")
            if content_length_header:
                try:
                    if int(content_length_header) > max_bytes:
                        conn.close()
                        raise ResponseTooLarge(
                            f"Response Content-Length ({content_length_header}) exceeds limit {max_bytes}"
                        )
                except ValueError:
                    pass

            resp_body_chunks = []
            total_read = 0
            chunk_size = 64 * 1024

            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > max_bytes:
                    conn.close()
                    raise ResponseTooLarge(f"Response body exceeded {max_bytes} bytes")
                resp_body_chunks.append(chunk)

            resp_body = b"".join(resp_body_chunks)
            resp_headers = {k: v for k, v in resp.getheaders()}

            conn.close()
            return FetchResult(
                status=resp.status,
                headers=resp_headers,
                body=resp_body,
                redirect_chain=redirect_chain,
                resolved_ips=all_resolved_ips,
                final_url=current_url,
            )

        except (socket.timeout, TimeoutError) as e:
            raise TimeoutError(f"Request timed out: {e}") from e
        except SafeFetchError:
            raise
        except Exception as e:
            if "timed out" in str(e).lower():
                raise TimeoutError(f"Request timed out: {e}") from e
            raise SafeFetchError(f"HTTP request failed: {e}") from e

    raise TooManyRedirects(f"Max redirects ({max_redirects}) exceeded.")
