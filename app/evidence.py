"""Page evidence: parsing, selection and fetching (stdlib + safe_fetch)."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.adapters.growth import fetch_page as own_aware_fetch
from app.security import validate_url

FETCH_HEADERS = {"User-Agent": "Pickable/0.1 SaaS intelligence", "Accept": "text/html,application/xhtml+xml"}
ASSET_RE = re.compile(r"\.(css|js|mjs|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|zip|mp4|pdf)(/|$)", re.I)
SKIP_PATH = ("login", "signin", "signup", "register", "cart", "checkout", "privacy", "terms", "cookie", "careers", "jobs")
PRIORITY = (
    (3, ("pricing", "price", "plans", "plan", "fiyat")),
    (2, ("about", "hakkimizda", "product", "features", "docs", "documentation", "integrations", "entegrasyon", "how-it-works", "getting-started")),
    (1, ("faq", "help", "support", "guide", "customers", "use-cases")),
)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.description = ""
        self.text = []
        self.links = []
        self.in_title = False
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): v or "" for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip += 1
        if tag == "title": self.in_title = True
        if tag == "meta" and attrs.get("name", "").lower() in ("description", "og:description"):
            self.description = attrs.get("content", "")[:500]
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"][:2000])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self.skip:
            self.skip -= 1
        if tag == "title": self.in_title = False

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self.skip: return
        if self.in_title and sum(map(len, self.title)) < 300: self.title.append(text)
        self.text.append(text)


def extract_page_evidence(url, status, headers, body, ref="homepage"):
    """Keep only bounded, visible page evidence; never pass raw HTML to the model."""
    parser = _PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    visible = " ".join(parser.text)
    return {"ref": ref, "url": url, "http_status": int(status),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content_type": next((v for k, v in headers.items() if k.lower() == "content-type"), "")[:120],
            "title": " ".join(parser.title)[:300], "description": parser.description[:500],
            "visible_text": visible[:12000], "sha256": hashlib.sha256(body).hexdigest(),
            "hash_kind": "sha256-response-body", "links": parser.links[:200]}


def _path_score(path):
    lowered = (path or "/").lower()
    if any(part in lowered for part in SKIP_PATH):
        return 0
    score = 0
    for value, words in PRIORITY:
        if any(word in lowered for word in words):
            score = max(score, value)
    return score


def select_research_urls(base_url, hrefs, origin_host, limit=3):
    """Pick discovered same-origin product pages; do not invent paths."""
    seen = set()
    ranked = []
    for href in hrefs or []:
        if not isinstance(href, str) or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        joined = urljoin(base_url, href)
        try:
            parts, _ = validate_url(joined)
        except ValueError:
            continue
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        if host != origin_host or ASSET_RE.search(parts.path or ""):
            continue
        clean = urlunsplit((parts.scheme.lower(), host, parts.path or "/", "", ""))
        if clean in seen or urlsplit(clean).path.rstrip("/") == urlsplit(base_url).path.rstrip("/"):
            continue
        seen.add(clean)
        ranked.append((_path_score(parts.path), clean))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [url for score, url in ranked if score > 0][:limit]


def discovered_fetch_urls(pages):
    origin, allowed, seen = None, [], set()
    for page in pages:
        url = page.get("url")
        if not isinstance(url, str):
            continue
        try:
            parts, _ = validate_url(url)
        except ValueError:
            continue
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        origin = origin or host
        for candidate in select_research_urls(url, page.get("links"), origin, limit=20):
            if candidate not in seen:
                seen.add(candidate)
                allowed.append(candidate)
    return origin, allowed


def evidence_pages(evidence):
    if isinstance(evidence, dict) and isinstance(evidence.get("pages"), list) and evidence["pages"]:
        return [page for page in evidence["pages"] if isinstance(page, dict)]
    return [evidence] if isinstance(evidence, dict) else []


def _fetch_page(url, ref, timeout=12):
    # Own-host targets reuse the growth adapter's loopback fallback: hosts
    # that cannot reach their own public URL would otherwise fail every
    # self-scan here. Foreign URLs behave exactly as before.
    # Modern SaaS homepages often exceed 1 MB of HTML; the cap must stay
    # well above that (evidence parsing needs the full body) while still
    # bounding worker memory. Anything larger fails explicitly, never silently.
    response = own_aware_fetch(url, timeout, max_redirects=4, max_bytes=3_000_000, required=True)
    if response is None:
        raise RuntimeError(f"{ref} is unreachable.")
    if response.status < 200 or response.status >= 400:
        raise RuntimeError(f"{ref} returned HTTP {response.status}.")
    return extract_page_evidence(response.final_url or url, response.status, response.headers, response.body, ref=ref)


def fetch_evidence(domain, extra_pages=1, on_homepage=None):
    url = domain if isinstance(domain, str) and domain.startswith(("https://", "http://")) else "https://" + domain + "/"
    homepage = _fetch_page(url, "homepage")
    if on_homepage is not None:
        on_homepage(homepage)
    origin = urlsplit(homepage["url"]).hostname or urlsplit(url).hostname
    extras = []
    for index, candidate in enumerate(select_research_urls(homepage["url"], homepage.get("links"), origin, extra_pages), 1):
        try:
            extras.append(_fetch_page(candidate, f"page-{index}", timeout=8))
        except Exception:
            continue
    return {"pages": [homepage, *extras]}
