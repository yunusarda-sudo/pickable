"""Early-access waitlist: input rules, hourly admission limits, analytics events.

The waitlist captures interest in upcoming capabilities only. It stores a
normalized email, an optional website URL and the signup source; it never
subscribes anyone to a marketing campaign and exposes no listing endpoint.
"""
from __future__ import annotations

import datetime
import re
import threading
import time
from collections import deque

# Signup source recorded with every registration so landing-page and
# scan-result conversions can be counted separately.
SIGNUP_SOURCES = ("landing_page", "scan_results")

# First-party analytics funnel. Client code reports only the three view/click
# events below; the server records the two signup outcomes itself so a success
# is never counted twice and a client can never inflate the signup count. No
# free-text, no email, no identifier travels here.
CLIENT_EVENT_NAMES = (
    "waitlist_cta_viewed",
    "waitlist_cta_clicked",
    "waitlist_modal_opened",
)
SERVER_EVENT_NAMES = (
    "waitlist_signup_succeeded",
    "waitlist_signup_failed",
)
EVENT_NAMES = CLIENT_EVENT_NAMES + SERVER_EVENT_NAMES

MAX_EMAIL_LENGTH = 254
MAX_LOCAL_LENGTH = 64
MAX_WEBSITE_LENGTH = 500

_DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?")


def signup_source(value) -> str:
    """Known sources pass through; anything else falls back to the landing CTA."""
    return value if isinstance(value, str) and value in SIGNUP_SOURCES else "landing_page"


def normalize_email(value) -> str:
    """Trim, length-check and lowercase an email for storage and uniqueness."""
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")
    email = value.strip()
    if not email or len(email) > MAX_EMAIL_LENGTH:
        raise ValueError(f"Enter an email address of at most {MAX_EMAIL_LENGTH} characters.")
    local, _, domain = email.rpartition("@")
    if not local or not domain or len(local) > MAX_LOCAL_LENGTH:
        raise ValueError("Enter a valid email address.")
    if any(ch.isspace() or ord(ch) < 32 for ch in email) or email.count("@") != 1 or ".." in email:
        raise ValueError("Enter a valid email address.")
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("Enter a valid email address, for example name@example.com.")
    return email.lower()


def normalize_website(value) -> str | None:
    """Optional http(s) website URL; blank input stores nothing."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Website URL must be text.")
    website = value.strip()
    if not website:
        return None
    if len(website) > MAX_WEBSITE_LENGTH:
        raise ValueError(f"Website URL must be at most {MAX_WEBSITE_LENGTH} characters.")
    if any(ch.isspace() for ch in website):
        raise ValueError("Website URL must not contain spaces.")
    if "://" not in website:
        website = "https://" + website
    lowered = website.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("Website URL must start with http:// or https://.")
    if len(website) > MAX_WEBSITE_LENGTH + 8:  # 8 = the longest scheme prefix we add
        raise ValueError(f"Website URL must be at most {MAX_WEBSITE_LENGTH} characters.")
    scheme, separator, rest = website.partition("://")
    host, slash, tail = rest.partition("/")
    if not separator or not _HOST_RE.fullmatch(host) or "." not in host:
        raise ValueError("Enter a valid website URL, for example https://yoursite.com.")
    if any(part in ("", ".", "..") for part in host.split(":")[0].split(".")):
        raise ValueError("Enter a valid website URL, for example https://yoursite.com.")
    fragmentless = scheme + "://" + host + (slash + tail.split("#", 1)[0] if slash else "")
    return fragmentless


def honeypot_filled(payload: dict) -> bool:
    """Automated submissions fill the hidden field; humans never see it."""
    return isinstance(payload, dict) and bool(str(payload.get("company") or "").strip())


def parse_signup(payload) -> tuple[str, str | None, str]:
    """Validate one signup request body into (email, website, source)."""
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object with an email address.")
    email = normalize_email(payload.get("email"))
    website = normalize_website(payload.get("website"))
    return email, website, signup_source(payload.get("source"))


def parse_event(payload) -> tuple[str, str]:
    """Validate one analytics event body into (name, source).

    Only the client-reported funnel events are accepted here. The signup
    outcomes are recorded server-side and are deliberately rejected, so a
    client retry can never double-count or fake a signup.
    """
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object with an event name.")
    name = payload.get("name") if payload.get("name") is not None else payload.get("event")
    if name not in CLIENT_EVENT_NAMES:
        raise ValueError("Unknown event name.")
    return name, signup_source(payload.get("source"))


class RateLimitError(OverflowError):
    def __init__(self, retry_after, message):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))
        self.message = message


class RateLimiter:
    """Atomic per-peer and global hourly budgets with a bounded peer table.

    Mirrors ScanLimiter's model: the peer IP is resolved by the server, the
    global budget always protects the worker, and a full peer table fails
    closed instead of evicting active budgets. Limits reset on restart.
    """
    def __init__(self, per_peer, per_hour, message, clock=time.monotonic, max_clients=1024):
        self.per_peer, self.per_hour, self.message = per_peer, per_hour, message
        self.clock, self.max_clients = clock, max_clients
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
            if len(self.events) >= self.per_hour:
                raise RateLimitError(self.events[0] + 3600 - now + 1, self.message)
            if peer is not None and len(peer) >= self.per_peer:
                raise RateLimitError(peer[0] + 3600 - now + 1, self.message)
            if peer is None:
                if len(self.clients) >= self.max_clients:
                    raise RateLimitError(60, self.message)
                peer = self.clients[remote_addr] = deque()
            peer.append(now)
            self.events.append(now)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class MemoryWaitlist:
    """Demo-mode store with the SQLite store's semantics but no persistence.

    Demo mode never writes to the production database, so a local demo run
    cannot pollute real signups and a restart starts clean — the same rule
    demo mode already applies to scan history.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.rows: list[dict] = []
        self.events: list[dict] = []

    def signup(self, email, website=None, source="landing_page") -> bool:
        with self._lock:
            if any(row["email"] == email for row in self.rows):
                return False
            self.rows.append({"email": email, "website": website, "source": source, "created_at": utc_now()})
            return True

    def record_event(self, name, source="landing_page") -> None:
        with self._lock:
            self.events.append({"name": name, "source": source, "created_at": utc_now()})
