"""Edge / proxy trust helpers. Forwarding headers are honored only behind trusted proxies."""

from __future__ import annotations

import ipaddress

DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _parse_trusted_proxies(raw):
    """Parse TRUSTED_PROXIES env (comma/space separated IPs or CIDRs)."""
    networks = []
    for part in (raw or "").replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    if not networks:
        networks = [ipaddress.ip_network(c) for c in DEFAULT_TRUSTED_PROXY_CIDRS]
    return tuple(networks)


def _peer_is_trusted(peer_ip, trusted):
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in trusted)


def _real_client_ip(handler, trusted):
    """Resolve the client IP behind Traefik/Cloudflare.

    Forwarding headers are honored ONLY when the direct TCP peer is a trusted
    proxy (docker/loopback by default). Direct internet clients cannot spoof.
    Prefers CF-Connecting-IP (Cloudflare-canonical), then X-Forwarded-For.
    """
    peer = handler.client_address[0]
    if not _peer_is_trusted(peer, trusted):
        return peer
    cf_ip = (handler.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass
    xff = handler.headers.get("X-Forwarded-For") or ""
    first = xff.split(",")[0].strip()
    if first:
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            pass
    return peer


def _came_via_cloudflare(handler):
    """True when Cloudflare edge headers are present (Ray/Connecting-IP/Visitor)."""
    return bool(handler.headers.get("CF-Ray") or handler.headers.get("CF-Connecting-IP")
                or handler.headers.get("CF-Visitor") or handler.headers.get("CF-IPCountry"))
