#!/usr/bin/env python3
"""
agent_ready_scan.py - Agent Readiness scanner (Python 3.8+, stdlib only).

Probes a site for the standards AI agents use to discover, read and transact
with it. Reproduces the 22 checks / 5 categories / level 0-5 model used by
Cloudflare's isitagentready.com, and adds an "extended" track of checks that
matter for agents but sit outside that ladder.

Usage:
    python agent_ready_scan.py https://example.com
    python agent_ready_scan.py example.com --json report.json --markdown report.md
    python agent_ready_scan.py example.com --deep      # also fetch linked scripts
    python agent_ready_scan.py example.com --remote    # cross-check vs official API
    python agent_ready_scan.py example.com --only discoverability,discovery
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

VERSION = "1.0.0"
UA = "AgentReadyScan/" + VERSION + " (+https://github.com/matteobaccan/AgentReady)"
DOH = "https://cloudflare-dns.com/dns-query"
MAX_BYTES = 512 * 1024

PASS, FAIL, WARN, NEUTRAL, SKIP, ERROR = "pass", "fail", "warn", "neutral", "skip", "error"

AI_BOTS = [
    "gptbot", "chatgpt-user", "oai-searchbot", "google-extended", "googleother",
    "ccbot", "anthropic-ai", "claudebot", "claude-web", "claude-searchbot",
    "perplexitybot", "perplexity-user", "bytespider", "cohere-ai",
    "meta-externalagent", "applebot-extended", "amazonbot", "youbot", "diffbot",
]

LEVEL_NAMES = {
    0: "Not Ready",
    1: "Basic Web Presence",
    2: "Bot-Aware",
    3: "Agent-Readable",
    4: "Agent-Integrated",
    5: "Agent-Native",
}

CATEGORY_TITLES = {
    "discoverability": "Discoverability",
    "contentAccessibility": "Content Accessibility",
    "botAccessControl": "Bot Access Control",
    "discovery": "API, Auth & MCP Discovery",
    "commerce": "Commerce",
    "extended": "Extended (beyond the 0-5 ladder)",
}


# --------------------------------------------------------------------------- #
# HTTP / DNS plumbing
# --------------------------------------------------------------------------- #

@dataclass
class Resp:
    url: str
    status: int
    headers: dict
    body: bytes
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        if m:
            charset = m.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    @property
    def ctype(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CTX = ssl.create_default_context()


def fetch(url, method="GET", headers=None, body=None, timeout=12, follow=True) -> Resp:
    """One HTTP request. Never raises; failures come back as a Resp with .error."""
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    handlers = [urllib.request.HTTPSHandler(context=_CTX)]
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
            return Resp(r.geturl(), r.status,
                        dict((k.lower(), v) for k, v in r.headers.items()),
                        raw, elapsed_ms=int((time.time() - t0) * 1000))
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(MAX_BYTES)
        except Exception:
            pass
        return Resp(url, e.code, dict((k.lower(), v) for k, v in (e.headers or {}).items()),
                    raw, elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return Resp(url, 0, {}, b"", error=type(e).__name__ + ": " + str(e),
                    elapsed_ms=int((time.time() - t0) * 1000))


def doh(name: str, rrtype: str, timeout=8) -> dict:
    """DNS-over-HTTPS JSON query. Returns {} on transport failure."""
    q = urllib.parse.urlencode({"name": name, "type": rrtype, "do": "1"})
    r = fetch(DOH + "?" + q, headers={"Accept": "application/dns-json"}, timeout=timeout)
    return r.json() or {} if r.ok else {}


def doh_answers(name: str, rrtype: str) -> list:
    return [a for a in doh(name, rrtype).get("Answer", []) if a.get("data")]


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #

@dataclass
class Check:
    id: str
    category: str
    title: str
    status: str = SKIP
    message: str = ""
    details: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    spec: list = field(default_factory=list)

    def ev(self, label, outcome, summary):
        self.evidence.append({"label": label, "outcome": outcome, "summary": summary})
        return self

    def set(self, status, message, **details):
        self.status, self.message = status, message
        self.details.update(details)
        return self


class Site:
    """Fetch-once, share-everywhere view of the target."""

    def __init__(self, base: str, deep: bool = False, timeout: int = 12):
        self.base = base.rstrip("/")
        p = urllib.parse.urlparse(self.base)
        self.host = p.netloc
        self.scheme = p.scheme
        self.apex = self.host.split(":")[0]
        self.deep = deep
        self.timeout = timeout
        self._cache = {}

    def url(self, path: str) -> str:
        return self.base + path if path.startswith("/") else path

    def get(self, path: str, **kw) -> Resp:
        key = (path, tuple(sorted((kw.get("headers") or {}).items())),
               kw.get("method", "GET"), kw.get("follow", True))
        if key not in self._cache:
            self._cache[key] = fetch(self.url(path), timeout=self.timeout, **kw)
        return self._cache[key]

    @property
    def home(self) -> Resp:
        return self.get("/")

    @property
    def robots(self) -> Resp:
        return self.get("/robots.txt")

    @property
    def robots_parsed(self) -> dict:
        if "_robots" not in self._cache:
            r = self.robots
            self._cache["_robots"] = parse_robots(r.text) if (r.ok and "html" not in r.ctype) else \
                {"groups": [], "sitemaps": [], "agentmaps": [], "signals": []}
        return self._cache["_robots"]


# --------------------------------------------------------------------------- #
# robots.txt parsing
# --------------------------------------------------------------------------- #

def parse_robots(text: str) -> dict:
    """Parse robots.txt into groups, sitemaps, agentmaps and content signals.

    Content-Signal is read even inside comments, because contentsignals.org
    publishes it as a commented directive in some generators.
    """
    groups, sitemaps, agentmaps, signals = [], [], [], []
    cur = None
    for raw in text.splitlines():
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith("#"):
            inner = stripped.lstrip("#").strip()
            if inner.lower().startswith("content-signal"):
                line = inner
            else:
                continue
        else:
            line = stripped.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if cur is None or cur["rules"]:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(value.lower())
        elif directive == "sitemap":
            sitemaps.append(value)
        elif directive == "agentmap":
            agentmaps.append(value)
        elif directive == "content-signal":
            sig = {"userAgent": cur["agents"][0] if cur and cur["agents"] else "*"}
            for part in value.split(","):
                if "=" in part:
                    k, _, v = part.partition("=")
                    sig[k.strip().lower().replace("-", "_")] = v.strip().lower()
            signals.append(sig)
        elif cur is not None:
            cur["rules"].append((directive, value))
    return {"groups": groups, "sitemaps": sitemaps, "agentmaps": agentmaps, "signals": signals}


# --------------------------------------------------------------------------- #
# Category 1 - Discoverability
# --------------------------------------------------------------------------- #

def check_robots_txt(s: Site) -> Check:
    c = Check("robotsTxt", "discoverability", "robots.txt",
              spec=["https://www.rfc-editor.org/rfc/rfc9309.html"])
    r = s.robots
    if r.error:
        return c.set(ERROR, "Could not fetch /robots.txt (" + r.error + ")")
    c.ev("GET /robots.txt", "positive" if r.ok else "negative",
         "HTTP " + str(r.status) + " " + (r.ctype or "no content-type"))
    if not r.ok:
        return c.set(FAIL, "/robots.txt returned HTTP " + str(r.status))
    if "html" in r.ctype:
        return c.set(FAIL, "/robots.txt is served as HTML (soft 404)")
    p = s.robots_parsed
    if not p["groups"]:
        return c.set(FAIL, "robots.txt has no valid User-agent directive")
    c.ev("Validate structure", "positive",
         str(len(p["groups"])) + " User-agent group(s), " +
         str(len(p["sitemaps"])) + " Sitemap directive(s)")
    return c.set(PASS, "robots.txt exists with valid format",
                 groups=len(p["groups"]), sitemaps=p["sitemaps"],
                 agentmaps=p["agentmaps"], bytes=len(r.body))


def check_sitemap(s: Site) -> Check:
    c = Check("sitemap", "discoverability", "XML sitemap",
              spec=["https://www.sitemaps.org/protocol.html"])
    from_robots = s.robots_parsed["sitemaps"]
    if from_robots:
        c.ev("Parse robots.txt", "positive",
             "Found " + str(len(from_robots)) + " Sitemap directive(s)")
    candidates = list(from_robots) + [s.url(p) for p in
                                      ("/sitemap.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml")]
    seen = set()
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        r = fetch(u, timeout=s.timeout)
        if not r.ok:
            c.ev("GET " + u, "neutral", "HTTP " + str(r.status or "error"))
            continue
        body = r.text.lstrip()
        if "<urlset" in body or "<sitemapindex" in body:
            fmt = "sitemapindex" if "<sitemapindex" in body else "urlset"
            n = len(re.findall(r"<loc>", body))
            c.ev("GET " + u, "positive", "Valid " + fmt + " with " + str(n) + " <loc> entries")
            return c.set(PASS, "sitemap.xml exists with valid structure",
                         url=u, format=fmt, locs=n, fromRobotsTxt=u in from_robots)
        if body.startswith("http"):
            c.ev("GET " + u, "positive", "Plain-text sitemap")
            return c.set(PASS, "text sitemap found", url=u, format="txt")
        c.ev("GET " + u, "negative", "200 but not a valid sitemap document")
    return c.set(FAIL, "No valid sitemap found")


AGENT_RELS = {"alternate", "describedby", "service-desc", "service-doc", "api-catalog",
              "ai-catalog", "llms-txt", "author", "license", "canonical"}


def parse_link_header(value: str) -> list:
    out = []
    for part in re.findall(r"<[^>]*>[^,]*", value):
        m = re.match(r"<([^>]*)>(.*)", part)
        if not m:
            continue
        params = dict(re.findall(r'(\w+)\s*=\s*"?([^";]+)"?', m.group(2)))
        entry = {"uri": m.group(1)}
        for k, v in params.items():
            entry[k] = v.strip()
        out.append(entry)
    return out


def check_link_headers(s: Site) -> Check:
    c = Check("linkHeaders", "discoverability", "Link response headers",
              spec=["https://www.rfc-editor.org/rfc/rfc8288.html"])
    r = s.home
    if r.error:
        return c.set(ERROR, "Could not fetch homepage (" + r.error + ")")
    lh = r.header("link")
    if not lh:
        c.ev("GET /", "negative", "No Link header present in response")
        return c.set(FAIL, "No Link headers found on target page")
    links = parse_link_header(lh)
    rels = sorted(set(rel for l in links for rel in l.get("rel", "").split()))
    useful = sorted(set(rels) & AGENT_RELS)
    c.ev("GET /", "positive", "Link header with rel(s): " + (", ".join(rels) or "none"))
    if not useful:
        return c.set(WARN, "Link header present but no agent-relevant rel (" + ", ".join(rels) + ")",
                     rels=rels, links=links)
    return c.set(PASS, "Link header advertises " + ", ".join(useful), rels=rels, links=links)


def check_dns_aid(s: Site) -> Check:
    c = Check("dnsAid", "discoverability", "DNS for AI Discovery (DNS-AID)",
              spec=["https://datatracker.ietf.org/doc/draft-mozleywilliams-dnsop-dnsaid/",
                    "https://aid.agentcommunity.org/docs/specification"])
    domains = [s.apex]
    if s.apex.startswith("www."):
        domains.append(s.apex[4:])
    found, attempted, unresolvable = [], [], False
    for d in domains:
        for label in ("_index", "_a2a", "_mcp"):
            for rr in ("SVCB", "HTTPS"):
                name = "%s._agents.%s" % (label, d)
                attempted.append(rr + " " + name)
                res = doh(name, rr)
                if not res or res.get("Status") == 2:
                    unresolvable = True
                    continue
                for a in res.get("Answer", []):
                    found.append({"name": name, "type": rr, "data": a.get("data")})
        for name in ("_index._agents." + d, "_agent." + d):
            attempted.append("TXT " + name)
            for a in doh_answers(name, "TXT"):
                found.append({"name": name, "type": "TXT", "data": a.get("data")})
    if found:
        c.ev("DoH lookups", "positive", str(len(found)) + " agent DNS record(s)")
        return c.set(PASS, "DNS-AID records published (" + str(len(found)) + ")",
                     records=found, queriesAttempted=attempted)
    if unresolvable:
        return c.set(NEUTRAL, "Could not reliably determine DNS-AID support (SERVFAIL)",
                     queriesAttempted=attempted)
    c.ev("DoH lookups", "negative", "No _agents records (NXDOMAIN)")
    return c.set(FAIL, "No DNS-AID records found", queriesAttempted=attempted)


# --------------------------------------------------------------------------- #
# Category 2 - Content Accessibility
# --------------------------------------------------------------------------- #

MD_ACCEPT = {"accept": "text/markdown;q=1.0, text/plain;q=0.8, text/html;q=0.1",
             "cache-control": "no-cache"}


def looks_like_markdown(text: str) -> bool:
    if "<html" in text[:2000].lower() or "<!doctype html" in text[:200].lower():
        return False
    hits = 0
    if re.search(r"^#{1,6}\s+\S", text, re.M):
        hits += 1
    if re.search(r"^\s*[-*+]\s+\S", text, re.M):
        hits += 1
    if re.search(r"\[[^\]]+\]\([^)]+\)", text):
        hits += 1
    if re.search(r"^```", text, re.M):
        hits += 1
    return hits >= 1


def check_markdown_negotiation(s: Site) -> Check:
    c = Check("markdownNegotiation", "contentAccessibility", "Markdown content negotiation",
              spec=["https://developers.cloudflare.com/fundamentals/reference/markdown-for-agents/",
                    "https://www.rfc-editor.org/rfc/rfc7763.html"])
    r = s.get("/", headers=MD_ACCEPT)
    if r.error:
        return c.set(ERROR, "Could not fetch homepage (" + r.error + ")")
    c.ev("GET / with Accept: text/markdown", "neutral", "Content-Type: " + (r.ctype or "?"))
    if "markdown" in r.ctype:
        vary = "accept" in r.header("vary").lower()
        if not vary:
            return c.set(WARN, "Markdown returned, but no 'Vary: Accept' header (cache poisoning risk)",
                         contentType=r.ctype)
        return c.set(PASS, "Site supports Markdown content negotiation",
                     contentType=r.ctype, vary=r.header("vary"))
    if r.ctype in ("text/plain", "") and looks_like_markdown(r.text):
        return c.set(WARN, "Markdown-looking body returned as " + (r.ctype or "no type") +
                     " (should be text/markdown)", contentType=r.ctype)
    # fallback: explicit .md route
    for path in ("/index.md", "/README.md"):
        rr = s.get(path)
        if rr.ok and looks_like_markdown(rr.text):
            c.ev("GET " + path, "positive", "Explicit markdown route exists")
            return c.set(WARN, "No Accept negotiation, but an explicit markdown route exists at " + path,
                         markdownRoute=path)
    return c.set(FAIL, "Site does not support Markdown for Agents", contentType=r.ctype)


# --------------------------------------------------------------------------- #
# Category 3 - Bot Access Control
# --------------------------------------------------------------------------- #

def check_robots_ai_rules(s: Site) -> Check:
    c = Check("robotsTxtAiRules", "botAccessControl", "AI bot rules in robots.txt",
              spec=["https://www.rfc-editor.org/rfc/rfc9309.html"])
    r = s.robots
    if not r.ok or "html" in r.ctype:
        return c.set(FAIL, "No robots.txt to evaluate AI bot rules against")
    p = s.robots_parsed
    named, wildcard, blocked = [], False, []
    for g in p["groups"]:
        for a in g["agents"]:
            if a == "*":
                wildcard = True
            if a in AI_BOTS:
                named.append(a)
                if any(d == "disallow" and v == "/" for d, v in g["rules"]):
                    blocked.append(a)
    if named:
        c.ev("Scan User-agent groups", "positive",
             "Explicit rules for: " + ", ".join(sorted(set(named))))
        msg = "Explicit AI bot rules for " + str(len(set(named))) + " crawler(s)"
        if blocked:
            msg += "; fully disallowed: " + ", ".join(sorted(set(blocked)))
        return c.set(PASS, msg, namedBots=sorted(set(named)),
                     blockedBots=sorted(set(blocked)), checkedBots=AI_BOTS)
    if wildcard:
        c.ev("Scan User-agent groups", "positive",
             "Wildcard User-agent: * group covers AI crawlers")
        return c.set(PASS, "No AI-specific bot rules; wildcard rules apply to all crawlers "
                           "including AI bots", namedBots=[], checkedBots=AI_BOTS)
    return c.set(FAIL, "robots.txt has neither AI-specific rules nor a wildcard group")


def check_content_signals(s: Site) -> Check:
    c = Check("contentSignals", "botAccessControl", "Content Signals",
              spec=["https://contentsignals.org/",
                    "https://datatracker.ietf.org/doc/draft-romm-aipref-contentsignals/"])
    r = s.robots
    if not r.ok or "html" in r.ctype:
        return c.set(FAIL, "No robots.txt to read Content Signals from")
    sigs = s.robots_parsed["signals"]
    if not sigs:
        c.ev("Parse robots.txt", "negative", "No Content-Signal directive found")
        return c.set(FAIL, "No Content Signals declared in robots.txt")
    known = {"search", "ai_input", "ai-input", "ai_train", "ai-train"}
    declared = sorted(set(k for sig in sigs for k in sig if k != "userAgent"))
    unknown = [k for k in declared if k not in known]
    c.ev("Parse robots.txt", "positive",
         str(len(sigs)) + " Content-Signal directive(s): " + ", ".join(declared))
    if unknown:
        return c.set(WARN, "Content Signals found but with unknown key(s): " + ", ".join(unknown),
                     signals=sigs)
    return c.set(PASS, "Content Signals found in robots.txt", signals=sigs, signalCount=len(sigs))


def check_web_bot_auth(s: Site) -> Check:
    c = Check("webBotAuth", "botAccessControl", "Web Bot Auth",
              spec=["https://datatracker.ietf.org/doc/draft-meunier-web-bot-auth-architecture/",
                    "https://www.rfc-editor.org/rfc/rfc9421.html"])
    r = s.get("/.well-known/http-message-signatures-directory")
    if not r.ok:
        c.ev("GET /.well-known/http-message-signatures-directory", "negative",
             "HTTP " + str(r.status or "error"))
        return c.set(NEUTRAL, "Web Bot Auth directory not found (informational only)")
    data = r.json()
    keys = (data or {}).get("keys", [])
    c.ev("GET /.well-known/http-message-signatures-directory", "positive",
         "JWKS with " + str(len(keys)) + " key(s)")
    if not keys:
        return c.set(WARN, "Web Bot Auth directory present but contains no keys")
    return c.set(PASS, "Web Bot Auth signature directory published", keyCount=len(keys))


# --------------------------------------------------------------------------- #
# Category 4 - API, Auth & MCP Discovery
# --------------------------------------------------------------------------- #

def check_api_catalog(s: Site) -> Check:
    c = Check("apiCatalog", "discovery", "API Catalog (RFC 9727)",
              spec=["https://www.rfc-editor.org/rfc/rfc9727.html"])
    r = s.get("/.well-known/api-catalog",
              headers={"Accept": "application/linkset+json, application/json"})
    if not r.ok:
        c.ev("GET /.well-known/api-catalog", "negative", "HTTP " + str(r.status or "error"))
        return c.set(FAIL, "API Catalog not found")
    data = r.json()
    if not isinstance(data, dict) or "linkset" not in data:
        return c.set(WARN, "/.well-known/api-catalog exists but is not a valid linkset document",
                     contentType=r.ctype)
    n = len(data.get("linkset") or [])
    c.ev("GET /.well-known/api-catalog", "positive", "Valid linkset with " + str(n) + " entries")
    return c.set(PASS, "API Catalog published with " + str(n) + " entries", entries=n)


def check_oauth_discovery(s: Site) -> Check:
    c = Check("oauthDiscovery", "discovery", "OAuth / OIDC discovery",
              spec=["https://www.rfc-editor.org/rfc/rfc8414.html",
                    "https://openid.net/specs/openid-connect-discovery-1_0.html"])
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        r = s.get(path)
        if r.ok and isinstance(r.json(), dict):
            d = r.json()
            if "issuer" in d or "authorization_endpoint" in d:
                c.ev("GET " + path, "positive", "issuer=" + str(d.get("issuer", "?")))
                extras = {}
                if "agent_auth" in d:
                    extras["agentAuth"] = d["agent_auth"]
                    c.ev("Inspect metadata", "positive", "agent_auth block present (auth.md)")
                return c.set(PASS, "OAuth/OIDC discovery metadata published at " + path,
                             path=path, issuer=d.get("issuer"), **extras)
        c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
    return c.set(FAIL, "No OAuth/OIDC discovery metadata found")


def check_oauth_protected_resource(s: Site) -> Check:
    c = Check("oauthProtectedResource", "discovery", "OAuth Protected Resource (RFC 9728)",
              spec=["https://www.rfc-editor.org/rfc/rfc9728.html"])
    r = s.get("/.well-known/oauth-protected-resource")
    if r.ok and isinstance(r.json(), dict) and "resource" in r.json():
        d = r.json()
        c.ev("GET /.well-known/oauth-protected-resource", "positive",
             "resource=" + str(d.get("resource")))
        return c.set(PASS, "OAuth Protected Resource Metadata published",
                     resource=d.get("resource"),
                     authorizationServers=d.get("authorization_servers"))
    c.ev("GET /.well-known/oauth-protected-resource", "negative", "HTTP " + str(r.status or "error"))
    # RFC 9728 also allows advertising via WWW-Authenticate on a 401
    wa = s.home.header("www-authenticate")
    if "resource_metadata" in wa.lower():
        c.ev("Inspect WWW-Authenticate", "positive", wa[:120])
        return c.set(PASS, "Protected Resource Metadata advertised via WWW-Authenticate",
                     wwwAuthenticate=wa)
    return c.set(FAIL, "No OAuth Protected Resource Metadata found")


def _homepage_markdown(s: Site) -> str:
    """Body a markdown-negotiating site returns for '/'. Used to spot soft 404s:
    such sites happily answer /anything.md with the homepage."""
    r = s.get("/", headers=MD_ACCEPT)
    return r.text.strip() if (r.ok and "html" not in r.ctype) else ""


def check_auth_md(s: Site) -> Check:
    c = Check("authMd", "discovery", "auth.md agent registration",
              spec=["https://workos.com/auth-md", "https://github.com/workos/auth.md"])
    home_md = _homepage_markdown(s)
    for path in ("/auth.md", "/.well-known/auth.md"):
        r = s.get(path, headers={"Accept": "text/markdown, text/plain, */*"})
        if not (r.ok and "html" not in r.ctype and looks_like_markdown(r.text)):
            c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
            continue
        if home_md and r.text.strip() == home_md:
            c.ev("GET " + path, "negative",
                 "200 but the body is the homepage markdown (soft 404)")
            continue
        if not re.search(r"\b(register|registration|oauth|credential|scope|agent)\b",
                         r.text, re.I):
            c.ev("GET " + path, "negative",
                 "Markdown returned but it does not describe agent registration")
            continue
        c.ev("GET " + path, "positive", "Markdown document, " + str(len(r.body)) + " bytes")
        return c.set(PASS, "auth.md published at " + path, path=path, bytes=len(r.body))
    return c.set(FAIL, "auth.md not found")


def check_mcp_server_card(s: Site) -> Check:
    c = Check("mcpServerCard", "discovery", "MCP Server Card",
              spec=["https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2127",
                    "https://modelcontextprotocol.io/"])
    for path in ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json",
                 "/.well-known/mcp/server-cards.json"):
        r = s.get(path)
        if r.ok and isinstance(r.json(), (dict, list)):
            d = r.json()
            card = d[0] if isinstance(d, list) and d else d
            name = (card.get("serverInfo") or {}).get("name") or card.get("name")
            if not name:
                c.ev("GET " + path, "negative", "JSON present but no server name field")
                continue
            c.ev("GET " + path, "positive", "Server card for '" + str(name) + "'")
            url = card.get("url") or (card.get("endpoints") or {}).get("jsonrpc")
            return c.set(PASS, "MCP Server Card published at " + path,
                         path=path, name=name, endpoint=url,
                         transport=(card.get("transport") or {}).get("type"))
        c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
    # last resort: a live streamable-http endpoint at /mcp
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "agent-ready-scan", "version": VERSION}}}
    r = s.get("/mcp", method="POST", body=json.dumps(init),
              headers={"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-11-25"})
    if r.ok and ("jsonrpc" in r.text or "event:" in r.text):
        c.ev("POST /mcp", "positive", "Live MCP endpoint responds to initialize")
        return c.set(WARN, "Live MCP endpoint at /mcp but no discoverable server card",
                     endpoint=s.url("/mcp"))
    return c.set(FAIL, "MCP Server Card not found at any candidate path")


def check_a2a_agent_card(s: Site) -> Check:
    c = Check("a2aAgentCard", "discovery", "A2A Agent Card",
              spec=["https://a2a-protocol.org/latest/specification/",
                    "https://a2a-protocol.org/latest/topics/agent-discovery/"])
    # /.well-known/agent.json is the pre-v0.3 location. A card found only there
    # is real but published at a deprecated path, so it warns rather than
    # passes: agents following the current spec will not find it.
    for path, current in (("/.well-known/agent-card.json", True),
                          ("/.well-known/agent.json", False)):
        r = s.get(path)
        d = r.json() if r.ok else None
        if not (isinstance(d, dict) and ("name" in d or "protocolVersion" in d)):
            c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
            continue
        skills = d.get("skills") or []
        c.ev("GET " + path, "positive" if current else "neutral",
             "'" + str(d.get("name", "?")) + "' with " + str(len(skills)) + " skill(s)")
        details = dict(path=path, name=d.get("name"), skills=len(skills),
                       url=d.get("url"),
                       extensions=[e.get("uri") for e in
                                   ((d.get("capabilities") or {}).get("extensions") or [])])
        if current:
            return c.set(PASS, "A2A Agent Card published at " + path, **details)
        return c.set(WARN, "A2A Agent Card found at the deprecated path " + path +
                     "; the current spec expects /.well-known/agent-card.json",
                     deprecatedPath=True, **details)
    return c.set(FAIL, "A2A Agent Card not found")


def check_agent_skills(s: Site) -> Check:
    c = Check("agentSkills", "discovery", "Agent Skills index",
              spec=["https://schemas.agentskills.io/discovery/0.2.0/schema.json",
                    "https://code.claude.com/docs/en/skills"])
    for path in ("/.well-known/agent-skills/index.json", "/.well-known/skills/index.json"):
        r = s.get(path)
        d = r.json() if r.ok else None
        if isinstance(d, dict) and isinstance(d.get("skills"), list):
            n = len(d["skills"])
            c.ev("GET " + path, "positive", "Index with " + str(n) + " skill(s)")
            if n == 0:
                return c.set(WARN, "Agent Skills index exists but lists no skills", path=path)
            return c.set(PASS, "Agent Skills index exists with valid JSON",
                         path=path, count=n,
                         names=[x.get("name") for x in d["skills"][:20]])
        c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
    return c.set(FAIL, "Agent Skills index not found")


WEBMCP_PAT = re.compile(
    r"(navigator|window|document)\s*\.\s*modelContext|modelContext\s*\.\s*(provideContext|registerTool)"
    r"|provideContext\s*\(|registerTool\s*\(", re.I)


def check_webmcp(s: Site) -> Check:
    c = Check("webMcp", "discovery", "WebMCP (navigator/document.modelContext)",
              spec=["https://github.com/webmachinelearning/webmcp",
                    "https://patrickbrosset.com/articles/2026-02-23-webmcp-updates-clarifications-and-next-steps/"])
    r = s.home
    if r.error:
        return c.set(ERROR, "Could not fetch homepage (" + r.error + ")")
    html = r.text
    if WEBMCP_PAT.search(html):
        c.ev("Scan inline HTML", "positive", "modelContext usage found in page source")
        return c.set(PASS, "WebMCP tool registration found in page source", source="inline")
    c.ev("Scan inline HTML", "neutral", "No modelContext usage in the HTML itself")
    if not s.deep:
        return c.set(FAIL, "No WebMCP tools detected in page source "
                           "(re-run with --deep to scan linked scripts)")
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)[:12]
    for src in scripts:
        u = urllib.parse.urljoin(r.url, src)
        if urllib.parse.urlparse(u).netloc != s.host:
            continue
        js = fetch(u, timeout=s.timeout)
        if js.ok and WEBMCP_PAT.search(js.text):
            c.ev("Scan " + src, "positive", "modelContext usage found in bundled script")
            return c.set(PASS, "WebMCP tool registration found in " + src, source=u)
    c.ev("Scan linked scripts", "negative",
         "Checked " + str(len(scripts)) + " same-origin script(s), no modelContext usage")
    return c.set(FAIL, "No WebMCP tools detected on page load")


def check_ard(s: Site) -> Check:
    c = Check("ard", "discovery", "ARD capability manifest (ai-catalog.json)",
              spec=["https://agenticresourcediscovery.org/spec/",
                    "https://developers.googleblog.com/announcing-the-agentic-resource-discovery-specification/"])
    mechanisms = []
    agentmaps = s.robots_parsed["agentmaps"]
    if agentmaps:
        mechanisms.append("robots.txt Agentmap")
        c.ev("Parse robots.txt", "positive", str(len(agentmaps)) + " Agentmap directive(s)")
    else:
        c.ev("Parse robots.txt", "neutral", "No Agentmap directives found")
    html_links = re.findall(r'<link[^>]+rel=["\']ai-catalog["\'][^>]*>', s.home.text, re.I)
    if html_links:
        mechanisms.append("<link rel=ai-catalog>")
        c.ev("Parse page head", "positive", str(len(html_links)) + " ai-catalog link tag(s)")
    else:
        c.ev("Parse page head", "neutral", "No catalog links found")
    r = s.get("/.well-known/ai-catalog.json", headers={"Accept": "application/json"})
    manifest = r.json() if r.ok else None
    if isinstance(manifest, dict) and "entries" in manifest:
        mechanisms.append("/.well-known/ai-catalog.json")
        n = len(manifest.get("entries") or [])
        c.ev("GET /.well-known/ai-catalog.json", "positive",
             "specVersion=" + str(manifest.get("specVersion")) + ", " + str(n) + " entries")
        return c.set(PASS, "ARD capability manifest published with " + str(n) + " entries",
                     entries=n, specVersion=manifest.get("specVersion"),
                     discoveryMechanisms=mechanisms)
    c.ev("GET /.well-known/ai-catalog.json", "negative", "HTTP " + str(r.status or "error"))
    for label in ("_catalog", "_search"):
        for rr in ("TXT", "SRV"):
            ans = doh_answers(label + "._agents." + s.apex, rr)
            if ans:
                mechanisms.append("DNS " + rr + " " + label)
                c.ev("DoH " + rr + " " + label + "._agents." + s.apex, "positive",
                     str(len(ans)) + " record(s)")
    if mechanisms:
        return c.set(WARN, "ARD advertised via " + ", ".join(mechanisms) +
                     " but no valid manifest fetched", discoveryMechanisms=mechanisms)
    return c.set(FAIL, "ARD capability manifest not found", discoveryMechanisms=[])


# --------------------------------------------------------------------------- #
# Category 5 - Commerce
# --------------------------------------------------------------------------- #

COMMERCE_HINTS = re.compile(
    r'"@type"\s*:\s*"(Product|Offer|AggregateOffer)"|add[-_ ]?to[-_ ]?cart|/checkout|/cart\b'
    r'|shopify|woocommerce|magento|bigcommerce|prestashop|snipcart', re.I)


def detect_commerce(s: Site) -> tuple:
    hits = sorted(set(m.group(0).lower() for m in COMMERCE_HINTS.finditer(s.home.text)))[:8]
    return bool(hits), hits


def check_x402(s: Site, is_commerce: bool) -> Check:
    c = Check("x402", "commerce", "x402 payment protocol",
              spec=["https://www.x402.org/", "https://github.com/coinbase/x402"])
    for path in ("/", "/api", "/api/v1"):
        r = s.get(path)
        if r.status == 402:
            body = r.json() or {}
            c.ev("GET " + path, "positive", "HTTP 402 Payment Required")
            return c.set(PASS, "x402 payment challenge served at " + path,
                         path=path, accepts=body.get("accepts"))
        c.ev("GET " + path, "neutral", path + " returned " + str(r.status or "error") + " (not 402)")
    if "x-payment" in s.home.headers or "x402" in s.home.header("www-authenticate").lower():
        return c.set(PASS, "x402 headers advertised on homepage")
    msg = "x402 payment protocol not detected"
    return c.set(FAIL if is_commerce else NEUTRAL,
                 msg if is_commerce else msg + " (not a commerce site)")


def check_mpp(s: Site, is_commerce: bool) -> Check:
    c = Check("mpp", "commerce", "Machine Payments Protocol (MPP)",
              spec=["https://machinepayments.org/"])
    for path in ("/.well-known/mpp.json", "/openapi.json"):
        r = s.get(path)
        if r.ok:
            txt = r.text.lower()
            if "mpp" in txt or "x-mpp" in txt or "machine-payments" in txt:
                c.ev("GET " + path, "positive", "MPP metadata present")
                return c.set(PASS, "MPP payment discovery found at " + path, path=path)
            c.ev("GET " + path, "neutral", path + " returned 200 but no MPP metadata")
        else:
            c.ev("GET " + path, "neutral", path + " returned " + str(r.status or "error"))
    msg = "MPP payment discovery not detected"
    return c.set(FAIL if is_commerce else NEUTRAL,
                 msg if is_commerce else msg + " (not a commerce site)")


def check_ucp(s: Site, is_commerce: bool) -> Check:
    c = Check("ucp", "commerce", "Universal Commerce Protocol (UCP)",
              spec=["https://universalcommerce.org/"])
    for path in ("/.well-known/ucp", "/.well-known/ucp.json"):
        r = s.get(path)
        if r.ok and r.json() is not None:
            c.ev("GET " + path, "positive", "UCP profile document")
            return c.set(PASS, "UCP profile published at " + path, path=path)
        c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
    msg = "UCP profile not found"
    return c.set(FAIL if is_commerce else NEUTRAL,
                 msg if is_commerce else msg + " (not a commerce site)")


def check_acp(s: Site, is_commerce: bool) -> Check:
    c = Check("acp", "commerce", "Agentic Commerce Protocol (ACP)",
              spec=["https://www.agenticcommerce.dev/"])
    for path in ("/.well-known/acp.json", "/.well-known/agentic-commerce.json"):
        r = s.get(path)
        if r.ok and r.json() is not None:
            c.ev("GET " + path, "positive", "ACP discovery document")
            return c.set(PASS, "ACP discovery document published at " + path, path=path)
        c.ev("GET " + path, "negative", "HTTP " + str(r.status or "error"))
    msg = "ACP discovery document not found"
    return c.set(FAIL if is_commerce else NEUTRAL,
                 msg if is_commerce else msg + " (not a commerce site)")


def check_ap2(s: Site, is_commerce: bool, a2a: Check) -> Check:
    c = Check("ap2", "commerce", "Agent Payments Protocol (AP2)",
              spec=["https://ap2-protocol.org/"])
    if a2a.status != PASS:
        c.ev("Depends on A2A Agent Card", "negative", "No A2A Agent Card found")
        msg = "AP2 not detected (no A2A Agent Card)"
        return c.set(FAIL if is_commerce else NEUTRAL,
                     msg if is_commerce else msg + " (not a commerce site)")
    exts = a2a.details.get("extensions") or []
    if any("ap2" in str(e).lower() or "payment" in str(e).lower() for e in exts):
        c.ev("Inspect A2A extensions", "positive", ", ".join(str(e) for e in exts))
        return c.set(PASS, "AP2 payment extension declared on the A2A Agent Card", extensions=exts)
    msg = "A2A Agent Card found but no AP2 payment extension declared"
    return c.set(FAIL if is_commerce else NEUTRAL, msg)


# --------------------------------------------------------------------------- #
# Extended track (not part of the 0-5 ladder)
# --------------------------------------------------------------------------- #

def check_llms_txt(s: Site) -> Check:
    c = Check("llmsTxt", "extended", "llms.txt", spec=["https://llmstxt.org/"])
    r = s.get("/llms.txt")
    if not (r.ok and "html" not in r.ctype):
        c.ev("GET /llms.txt", "negative", "HTTP " + str(r.status or "error"))
        return c.set(FAIL, "No /llms.txt published")
    if r.text.strip() == _homepage_markdown(s):
        c.ev("GET /llms.txt", "negative", "200 but the body is the homepage (soft 404)")
        return c.set(FAIL, "No /llms.txt published (server answers any path with the homepage)")
    has_h1 = bool(re.search(r"^#\s+\S", r.text, re.M))
    links = len(re.findall(r"\[[^\]]+\]\([^)]+\)", r.text))
    full = s.get("/llms-full.txt")
    c.ev("GET /llms.txt", "positive",
         str(len(r.body)) + " bytes, " + str(links) + " link(s)")
    if not has_h1:
        return c.set(WARN, "/llms.txt exists but has no H1 title (llmstxt.org requires one)")
    return c.set(PASS, "/llms.txt published" + (" (+ llms-full.txt)" if full.ok else ""),
                 bytes=len(r.body), links=links, llmsFullTxt=full.ok)


def check_structured_data(s: Site) -> Check:
    c = Check("structuredData", "extended", "Schema.org JSON-LD",
              spec=["https://schema.org/", "https://json-ld.org/"])
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        s.home.text, re.I | re.S)
    if not blocks:
        return c.set(FAIL, "No JSON-LD structured data on the homepage")
    types, invalid = [], 0
    for b in blocks:
        try:
            data = json.loads(b.strip())
        except Exception:
            invalid += 1
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            types += t if isinstance(t, list) else ([t] if t else [])
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack += graph
    types = sorted(set(t for t in types if isinstance(t, str)))
    c.ev("Parse <script type=application/ld+json>", "positive",
         str(len(blocks)) + " block(s): " + (", ".join(types) or "no @type"))
    if invalid:
        return c.set(WARN, str(invalid) + " of " + str(len(blocks)) +
                     " JSON-LD block(s) failed to parse", types=types)
    if not types:
        return c.set(WARN, "JSON-LD blocks present but none declare an @type", blocks=len(blocks))
    return c.set(PASS, "JSON-LD present: " + ", ".join(types), types=types, blocks=len(blocks))


def check_html_semantics(s: Site) -> Check:
    c = Check("htmlSemantics", "extended", "Semantic HTML & metadata")
    h = s.home.text
    issues, good = [], []
    (good if re.search(r"<h1[\s>]", h, re.I) else issues).append("h1")
    (good if re.search(r"<(main|article)[\s>]", h, re.I) else issues).append("main/article")
    (good if re.search(r'<meta[^>]+name=["\']description["\']', h, re.I) else issues).append("meta description")
    (good if re.search(r'<link[^>]+rel=["\']canonical["\']', h, re.I) else issues).append("canonical")
    (good if re.search(r'<html[^>]+lang=', h, re.I) else issues).append("html lang")
    (good if re.search(r'<meta[^>]+property=["\']og:', h, re.I) else issues).append("OpenGraph")
    imgs = re.findall(r"<img\b[^>]*>", h, re.I)
    no_alt = [i for i in imgs if not re.search(r'\balt\s*=', i, re.I)]
    c.ev("Parse homepage", "positive" if not issues else "neutral",
         "Present: " + (", ".join(good) or "none"))
    det = {"present": good, "missing": issues, "images": len(imgs), "imagesWithoutAlt": len(no_alt)}
    if no_alt:
        issues.append(str(len(no_alt)) + "/" + str(len(imgs)) + " <img> without alt")
    if not issues:
        return c.set(PASS, "Homepage exposes h1, landmarks, description, canonical, lang and OG", **det)
    return c.set(WARN if len(issues) <= 2 else FAIL, "Missing: " + ", ".join(issues), **det)


def check_server_rendered(s: Site) -> Check:
    c = Check("serverRendered", "extended", "Content readable without JavaScript")
    h = s.home.text
    stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", stripped)
    words = len(re.findall(r"\w{3,}", text))
    c.ev("Strip scripts and tags", "neutral", str(words) + " words in server-rendered HTML")
    if words >= 250:
        return c.set(PASS, "Homepage returns " + str(words) + " words of text without JavaScript",
                     words=words)
    if words >= 60:
        return c.set(WARN, "Only " + str(words) + " words in the initial HTML - "
                     "most agents never run your JavaScript", words=words)
    return c.set(FAIL, "Initial HTML is essentially empty (" + str(words) + " words); "
                 "content requires client-side rendering", words=words)


def check_https_and_caching(s: Site) -> Check:
    c = Check("transport", "extended", "HTTPS redirect & cache validators")
    notes, problems = [], []
    if s.scheme == "https":
        http = fetch("http://" + s.host + "/", timeout=s.timeout, follow=False)
        if http.status in (301, 308):
            notes.append("HTTP permanently redirects to HTTPS")
        elif http.status in (302, 307):
            problems.append("HTTP->HTTPS redirect is temporary (" + str(http.status) + ")")
        elif http.status:
            problems.append("HTTP does not redirect to HTTPS (" + str(http.status) + ")")
    r = s.home
    if r.header("etag") or r.header("last-modified"):
        notes.append("cache validators present")
    else:
        problems.append("no ETag or Last-Modified (agents cannot revalidate cheaply)")
    if r.header("cache-control"):
        notes.append("Cache-Control: " + r.header("cache-control")[:60])
    c.ev("Inspect transport", "positive" if not problems else "neutral", "; ".join(notes) or "none")
    if not problems:
        return c.set(PASS, "; ".join(notes), notes=notes)
    return c.set(WARN, "; ".join(problems), notes=notes, problems=problems)


def check_status_sanity(s: Site) -> Check:
    c = Check("statusSanity", "extended", "Honest HTTP status codes")
    r = s.get("/this-page-should-not-exist-agent-ready-probe")
    if r.status == 404:
        c.ev("GET missing path", "positive", "Correct 404")
        return c.set(PASS, "Missing pages return a real 404")
    if r.status in (410, 451):
        return c.set(PASS, "Missing pages return " + str(r.status))
    if r.ok:
        return c.set(FAIL, "Missing pages return HTTP 200 (soft 404) - agents cannot tell "
                     "a real page from an error", status=r.status)
    return c.set(WARN, "Missing pages return HTTP " + str(r.status or "error"), status=r.status)


def check_security_txt(s: Site) -> Check:
    c = Check("securityTxt", "extended", "security.txt",
              spec=["https://www.rfc-editor.org/rfc/rfc9116.html"])
    for path in ("/.well-known/security.txt", "/security.txt"):
        r = s.get(path)
        if r.ok and "contact:" in r.text.lower():
            c.ev("GET " + path, "positive", "Contact directive present")
            return c.set(PASS, "security.txt published at " + path, path=path)
    return c.set(WARN, "No security.txt - agents and researchers have no machine-readable "
                 "contact channel")


def check_feed(s: Site) -> Check:
    c = Check("feed", "extended", "RSS / Atom feed")
    links = re.findall(r'<link[^>]+type=["\'](application/(?:rss|atom)\+xml)["\'][^>]*>',
                       s.home.text, re.I)
    if links:
        return c.set(PASS, "Feed advertised in <head> (" + ", ".join(sorted(set(links))) + ")")
    for path in ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"):
        r = s.get(path)
        if r.ok and ("<rss" in r.text[:400] or "<feed" in r.text[:400]):
            return c.set(WARN, "Feed exists at " + path + " but is not linked from <head>", path=path)
    return c.set(NEUTRAL, "No RSS/Atom feed found (optional, but the cheapest change-notification "
                 "channel for agents)")


# --------------------------------------------------------------------------- #
# Level ladder
# --------------------------------------------------------------------------- #

# Level 4 is unlocked by ANY ONE of these four "callable surface" checks.
# Derived from 20 reference reports spanning levels 0-5; see docs/METHODOLOGY.md.
# Note what is NOT here: webMcp and ard never appear as the sole discovery pass
# on any fixture, so whether they also unlock level 4 is untestable with the
# available evidence. They are excluded conservatively.
L4_POOL = ["apiCatalog", "mcpServerCard", "a2aAgentCard", "agentSkills"]

ANY_SURFACE = "__any_discovery_surface__"

LADDER = [
    (1, ["robotsTxt", "sitemap"]),
    (2, ["robotsTxtAiRules", "contentSignals"]),
    (3, ["markdownNegotiation"]),
    (4, [ANY_SURFACE]),
    (5, ["oauthDiscovery"]),
]

REQ_DESCRIPTIONS = {
    "robotsTxt": "Publish a valid robots.txt (RFC 9309)",
    "sitemap": "Publish an XML sitemap and reference it from robots.txt",
    "robotsTxtAiRules": "Cover AI crawlers with explicit or wildcard robots.txt rules",
    "contentSignals": "Declare AI content usage preferences with Content Signals in robots.txt",
    "markdownNegotiation": "Support Accept: text/markdown content negotiation",
    ANY_SURFACE: "Expose at least one callable surface: an API catalog, an MCP "
                 "server card, an A2A agent card or an agent skills index",
    "oauthDiscovery": "Publish OAuth/OIDC discovery metadata so agents can "
                      "authenticate rather than only read",
}


def compute_level(by_id: dict) -> tuple:
    """Return (level, next_target, [unmet requirement ids]).

    The ladder is sequential: a check that would satisfy a higher rung does
    not count until every lower rung is satisfied.
    """
    passed = set(k for k, c in by_id.items() if c.status == PASS)
    level = 0
    for target, reqs in LADDER:
        unmet = []
        for req in reqs:
            if req == ANY_SURFACE:
                if not any(x in passed for x in L4_POOL):
                    unmet.append(req)
            elif req not in passed:
                unmet.append(req)
        if unmet:
            return level, target, unmet
        level = target
    return 5, None, []


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def normalize(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    return p.scheme + "://" + p.netloc + (p.path.rstrip("/") if p.path != "/" else "")


def run_scan(target: str, deep=False, timeout=12, only=None, extended=True) -> dict:
    s = Site(normalize(target), deep=deep, timeout=timeout)
    t0 = time.time()

    # Warm the shared fetches once, serially, so every check reuses them.
    s.home, s.robots, s.robots_parsed

    if s.home.error and s.robots.error:
        return {"url": s.base, "error": "Site unreachable: " + s.home.error,
                "level": 0, "levelName": LEVEL_NAMES[0], "checks": {}}

    is_commerce, hints = detect_commerce(s)

    jobs = [
        check_robots_txt, check_sitemap, check_link_headers, check_dns_aid,
        check_markdown_negotiation,
        check_robots_ai_rules, check_content_signals, check_web_bot_auth,
        check_api_catalog, check_oauth_discovery, check_oauth_protected_resource,
        check_auth_md, check_mcp_server_card, check_a2a_agent_card,
        check_agent_skills, check_webmcp, check_ard,
    ]
    if extended:
        jobs += [check_llms_txt, check_structured_data, check_html_semantics,
                 check_server_rendered, check_https_and_caching, check_status_sanity,
                 check_security_txt, check_feed]

    results = []
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for c in pool.map(lambda f: _safe(f, s), jobs):
            results.append(c)

    by_id = dict((c.id, c) for c in results)
    a2a = by_id["a2aAgentCard"]
    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        commerce = list(pool.map(lambda f: _safe(f, s, is_commerce),
                                 [check_x402, check_mpp, check_ucp, check_acp]))
    commerce.append(check_ap2(s, is_commerce, a2a))
    results += commerce
    by_id.update(dict((c.id, c) for c in commerce))

    level, nxt, unmet = compute_level(by_id)

    grouped = {}
    for c in results:
        if only and c.category not in only:
            continue
        grouped.setdefault(c.category, {})[c.id] = {
            "title": c.title, "status": c.status, "message": c.message,
            "details": c.details, "evidence": c.evidence, "spec": c.spec,
        }

    return {
        "url": s.base,
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scanner": "agent_ready_scan.py " + VERSION,
        "durationMs": int((time.time() - t0) * 1000),
        "level": level,
        "levelName": LEVEL_NAMES[level],
        "nextLevel": None if nxt is None else {
            "target": nxt, "name": LEVEL_NAMES[nxt],
            "requirements": [{"check": "anyDiscoverySurface" if u == ANY_SURFACE else u,
                              "description": REQ_DESCRIPTIONS.get(u, u)}
                             for u in unmet],
        },
        "isCommerce": is_commerce,
        "commerceSignals": hints,
        "checks": grouped,
    }


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        name = fn.__name__.replace("check_", "")
        return Check(name, "discovery", name).set(
            ERROR, "Check crashed: " + type(e).__name__ + ": " + e.__str__())


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

ICON = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", NEUTRAL: "n/a ", SKIP: "skip", ERROR: "ERR "}


def render_text(rep: dict, verbose=False) -> str:
    if rep.get("error"):
        return "ERROR " + rep["url"] + ": " + rep["error"]
    out = []
    out.append("=" * 78)
    out.append("Agent Readiness: " + rep["url"])
    out.append("Level " + str(rep["level"]) + "/5 - " + rep["levelName"] +
               ("   [commerce site]" if rep["isCommerce"] else ""))
    out.append("=" * 78)
    for cat, checks in rep["checks"].items():
        counts = {}
        for c in checks.values():
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        summary = " ".join(k + ":" + str(v) for k, v in sorted(counts.items()))
        out.append("")
        out.append("## " + CATEGORY_TITLES.get(cat, cat) + "   (" + summary + ")")
        for cid, c in checks.items():
            out.append("  [%s] %-24s %s" % (ICON.get(c["status"], "?"), cid, c["message"]))
            if verbose:
                for e in c["evidence"]:
                    out.append("           - %s: %s" % (e["label"], e["summary"]))
    nxt = rep.get("nextLevel")
    out.append("")
    if nxt:
        out.append("Next level -> " + str(nxt["target"]) + " " + nxt["name"] + ", requires:")
        for r in nxt["requirements"]:
            out.append("  * " + r["check"] + ": " + r["description"])
    else:
        out.append("Top of the ladder: level 5 Agent-Native.")
    out.append("")
    return "\n".join(out)


def render_markdown(rep: dict) -> str:
    if rep.get("error"):
        return "# Agent Readiness\n\n**ERROR** " + rep["url"] + ": " + rep["error"] + "\n"
    L = []
    L.append("# Agent Readiness report - " + rep["url"])
    L.append("")
    L.append("**Level " + str(rep["level"]) + "/5 - " + rep["levelName"] + "**  ")
    L.append("Scanned " + rep["scannedAt"] + " by `" + rep["scanner"] + "`" +
             ("  ·  detected as a commerce site" if rep["isCommerce"] else ""))
    L.append("")
    nxt = rep.get("nextLevel")
    if nxt:
        L.append("## Next level: " + str(nxt["target"]) + " - " + nxt["name"])
        L.append("")
        for r in nxt["requirements"]:
            L.append("- **" + r["check"] + "** - " + r["description"])
        L.append("")
    for cat, checks in rep["checks"].items():
        L.append("## " + CATEGORY_TITLES.get(cat, cat))
        L.append("")
        L.append("| Check | Status | Result |")
        L.append("|---|---|---|")
        for cid, c in checks.items():
            L.append("| `" + cid + "` | " + c["status"].upper() + " | " +
                     c["message"].replace("|", "\\|") + " |")
        L.append("")
    fails = [(cid, c) for checks in rep["checks"].values() for cid, c in checks.items()
             if c["status"] in (FAIL, WARN)]
    if fails:
        L.append("## What to fix, with specs")
        L.append("")
        for cid, c in fails:
            L.append("### " + cid + " - " + c["title"])
            L.append("")
            L.append(c["message"])
            L.append("")
            for u in c.get("spec", []):
                L.append("- Spec: <" + u + ">")
            L.append("")
    return "\n".join(L)


def remote_scan(target: str) -> dict:
    r = fetch("https://isitagentready.com/api/scan", method="POST",
              body=json.dumps({"url": normalize(target)}),
              headers={"Content-Type": "application/json"}, timeout=120)
    return r.json() or {"error": "remote scan failed: HTTP " + str(r.status) + " " + r.error}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan a site for AI agent readiness.")
    ap.add_argument("url")
    ap.add_argument("--json", metavar="FILE", help="write the full JSON report")
    ap.add_argument("--markdown", metavar="FILE", help="write a Markdown report")
    ap.add_argument("--deep", action="store_true", help="also fetch linked scripts (WebMCP)")
    ap.add_argument("--remote", action="store_true",
                    help="also run the official isitagentready.com scan and compare levels")
    ap.add_argument("--only", help="comma-separated categories to display")
    ap.add_argument("--no-extended", action="store_true", help="skip the extended track")
    ap.add_argument("--verbose", "-v", action="store_true", help="print per-check evidence")
    ap.add_argument("--timeout", type=int, default=12)
    a = ap.parse_args(argv)

    only = set(x.strip() for x in a.only.split(",")) if a.only else None
    rep = run_scan(a.url, deep=a.deep, timeout=a.timeout, only=only,
                   extended=not a.no_extended)

    if a.remote:
        rr = remote_scan(a.url)
        rep["remote"] = {"level": rr.get("level"), "levelName": rr.get("levelName"),
                         "error": rr.get("error")}

    print(render_text(rep, verbose=a.verbose))
    if rep.get("remote"):
        r = rep["remote"]
        print("Official isitagentready.com: level " + str(r.get("level")) + " " +
              str(r.get("levelName")) + (" (" + r["error"] + ")" if r.get("error") else ""))
        print("")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, ensure_ascii=False)
        print("JSON report  -> " + a.json)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(rep))
        print("Markdown     -> " + a.markdown)

    return 0 if rep.get("level", 0) >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
