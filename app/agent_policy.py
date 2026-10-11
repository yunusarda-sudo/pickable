"""Server-local agent configuration and bounded scan admission controls."""
from collections import deque
from dataclasses import dataclass
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from app.security import normalize_domain


class RequestError(ValueError):
    def __init__(self, status, message, code="invalid_request"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


class ScanRateLimitError(OverflowError):
    def __init__(self, retry_after):
        super().__init__("Scan limit reached. Retry after the indicated number of seconds.")
        self.retry_after = max(1, int(retry_after))


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


@dataclass(frozen=True, repr=False)
class AgentPolicy:
    public_url: str = "https://solveny.dev"
    agent_api_key: str = ""
    mcp_scan_enabled: bool = False
    scans_per_hour: int = 5
    global_scans_per_hour: int = 30

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        public_url = env.get("PICKABLE_PUBLIC_URL", "https://solveny.dev").rstrip("/")
        p = urlsplit(public_url)
        local = p.hostname in ("127.0.0.1", "localhost", "::1")
        if (not p.hostname or p.scheme not in ("http", "https")
                or (p.scheme != "https" and not local) or p.username or p.password
                or p.path or p.query or p.fragment or any(c.isspace() for c in public_url)):
            raise ValueError("PICKABLE_PUBLIC_URL must be an HTTPS origin (HTTP is allowed for localhost).")
        # Accessing port also rejects malformed ports, before any discovery is served.
        p.port
        if not local:
            normalize_domain(p.hostname)
        key = env.get("PICKABLE_AGENT_API_KEY", "")
        if key and (len(key) < 32 or any(c.isspace() for c in key) or not key.isascii()):
            raise ValueError("PICKABLE_AGENT_API_KEY must contain at least 32 ASCII characters without whitespace.")
        return cls(public_url, key, env.get("PICKABLE_MCP_SCAN_ENABLED", "0") == "1" and bool(key),
                   _integer(env, "PICKABLE_SCANS_PER_HOUR", 5, 1, 60),
                   _integer(env, "PICKABLE_GLOBAL_SCANS_PER_HOUR", 30, 1, 600))

    def agent_authorized(self, authorization):
        if not self.agent_api_key or not isinstance(authorization, str):
            return False
        scheme, _, credential = authorization.partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(credential.encode(), self.agent_api_key.encode())


class ScanLimiter:
    """Atomic per-peer and global limits, with a bounded peer table.

    Forwarding headers are deliberately not trusted. Behind a reverse proxy the
    per-peer budget may be shared; the global budget always protects the worker.
    Limits reset on restart and are intended for this single-process deployment.
    """
    def __init__(self, policy, clock=time.monotonic, max_clients=1024):
        self.policy, self.clock, self.max_clients = policy, clock, max_clients
        self.clients, self.events, self.lock = {}, deque(), threading.Lock()

    def admit(self, remote_addr):
        now = self.clock()
        with self.lock:
            for key, events in list(self.clients.items()):
                while events and events[0] <= now - 3600:
                    events.popleft()
                if not events:
                    del self.clients[key]
            while self.events and self.events[0] <= now - 3600:
                self.events.popleft()
            peer = self.clients.get(remote_addr)
            if len(self.events) >= self.policy.global_scans_per_hour:
                raise ScanRateLimitError(self.events[0] + 3600 - now + 1)
            if peer is not None and len(peer) >= self.policy.scans_per_hour:
                raise ScanRateLimitError(peer[0] + 3600 - now + 1)
            if peer is None:
                if len(self.clients) >= self.max_clients:
                    # Fail closed rather than evicting active peers and resetting limits.
                    raise ScanRateLimitError(60)
                peer = self.clients[remote_addr] = deque()
            peer.append(now)
            self.events.append(now)
