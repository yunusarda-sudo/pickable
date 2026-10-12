"""Single source of truth for SSRF protection (stdlib only).

Both app.security (public facade, ValueError API) and app.safe_fetch
(richer FetchResult + typed exceptions) build on these primitives so a
security patch can never land in one client and miss the other.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import List
from urllib.parse import urlsplit


def is_ip_safe(ip_str: str) -> bool:
    """True only for globally routable public IPs.

    Blocks RFC1918, loopback, link-local, cloud metadata (169.254.169.254),
    IPv6 ULA, unspecified/broadcast/multicast/reserved, and IPv4-mapped
    IPv6 addresses pointing at private ranges (e.g. ::ffff:127.0.0.1).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if (not ip.is_global or ip.is_private or ip.is_loopback
            or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified):
        return False

    if str(ip) == "169.254.169.254":
        return False

    return True


def validate_url(url):
    """Parse and vet an http(s) URL. Returns (parts, port). Raises ValueError."""
    if not isinstance(url, str) or len(url) > 4096 or "\\" in url or any(ord(c) < 33 for c in url):
        raise ValueError("Invalid target URL.")
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("Non-HTTP(S) URLs or URLs with credentials are blocked.")
    port = p.port or (443 if p.scheme == "https" else 80)
    if port != (443 if p.scheme == "https" else 80):
        raise ValueError("Non-standard port blocked.")
    return p, port


def resolve_and_verify(hostname: str, port: int) -> List[str]:
    """Resolve once via getaddrinfo; require every answer to be public.

    Returns deduplicated IP strings. Raises ValueError (callers map this
    to their own exception type) so both facades share one DNS policy.
    """
    if port not in (80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports are allowed.")
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {hostname}: {e}") from e
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise ValueError(f"No IP addresses resolved for {hostname}")
    for ip in addresses:
        if not is_ip_safe(ip):
            raise ValueError(f"Access to private, local or reserved IP addresses is blocked (blocked: {ip}).")
    return addresses
