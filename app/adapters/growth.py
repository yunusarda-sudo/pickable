"""Marketing playbook checks read from the site itself: social profiles and blog activity.

Stdlib only. Every request goes through safe_fetch (public hosts, pinned DNS, size/time limits).
Raw report uses the {"checks": {group: {id: {status, message, details}}}} shape so reporting can flatten it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import http.client
import json
import os
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from app.adapters.base import Adapter, AdapterResult, CheckRecord
from app.safe_fetch import FetchResult, safe_fetch

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PickableBot/0.1)", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SOCIAL_HOSTS = {
    "linkedin.com": "LinkedIn", "x.com": "X", "twitter.com": "X", "instagram.com": "Instagram",
    "facebook.com": "Facebook", "youtube.com": "YouTube", "tiktok.com": "TikTok", "threads.net": "Threads",
    "pinterest.com": "Pinterest", "github.com": "GitHub", "medium.com": "Medium", "bsky.app": "Bluesky",
}
SHARE_MARKERS = ("/share", "/intent", "sharer", "sharearticle", "/dialog/")
BLOG_PATHS = ("/blog", "/news", "/articles", "/insights", "/journal", "/posts", "/stories", "/updates")
FEED_TYPES = ("application/rss+xml", "application/atom+xml")
FEED_GUESSES = ("feed", "rss.xml", "feed.xml", "feed.rss", "rss")  # tried under the blog, then the site root


def _own_hosts():
    """Public hostnames served by this server (normalized, www-folded)."""
    hosts = set()
    extra = os.environ.get("ALLOWED_HOSTS", "") + "," + os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    for host in extra.split(","):
        host = host.strip().lower()
        if host.count(":") == 1:  # tolerate host:port entries
            host = host.split(":")[0]
        host = host.removeprefix("www.")
        if host:
            hosts.add(host)
    public = urlsplit(os.environ.get("PICKABLE_PUBLIC_URL", "")).hostname
    if public:
        hosts.add(public.lower().removeprefix("www."))
    return hosts


def _internal_port():
    try:
        return int(os.environ.get("PORT", 8765))
    except ValueError:
        return 8765


def internal_fetch(domain, path="/", query="", timeout=8):
    """Fetch our own public site over loopback.

    Some hosts cannot reach their own public URL from inside (no hairpin
    route, filtered DNS, or the edge lockdown answering direct egress).
    The app serves the same content locally, and loopback callers pass host
    validation plus the edge lockdown, so this is a safe fallback. Fixed
    address (127.0.0.1) and the server's own port — never derived from user
    input, so no SSRF surface. Returns a FetchResult shaped like safe_fetch.
    """
    port = _internal_port()
    target = path or "/"
    if query:
        target += "?" + query
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", target, headers={"Host": f"127.0.0.1:{port}",
                                             "User-Agent": HEADERS["User-Agent"],
                                             "Accept": HEADERS["Accept"]})
        res = conn.getresponse()
        body = res.read(2_000_000 + 1)
    except Exception:
        return None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    if res.status != 200 or len(body) > 2_000_000:
        return None
    return FetchResult(status=200, headers={}, body=body, redirect_chain=[],
                       resolved_ips=["127.0.0.1"], final_url=f"https://{domain}{target}")


def fetch_page(url, timeout):
    """Public fetch first; loopback fallback when scanning our own host."""
    try:
        res = safe_fetch(url, headers=HEADERS, timeout=timeout)
    except Exception:
        res = None
    if res is not None and res.status == 200:
        return res
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host and host in _own_hosts():
        return internal_fetch(parts.hostname, parts.path or "/", parts.query, timeout)
    return None
# Calibration knobs: what "recent" and "regular" mean for a marketing blog.
RECENT_DAYS = 60
MAX_PARALLEL = 8  # concurrent requests per scan to the scanned site
CADENCE_DAYS, CADENCE_MIN_POSTS = 90, 3


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.feeds = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in FEED_TYPES and a.get("href"):
            self.feeds.append(a["href"])


class MetaParser(HTMLParser):
    """<title>, description, og:image and icon from a page head."""
    def __init__(self):
        super().__init__()
        self.meta, self.title, self._in_title, self.icon = {}, "", False, None

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "meta" and a.get("content"):
            self.meta.setdefault((a.get("property") or a.get("name") or "").lower(), a["content"])
        elif tag == "link" and "icon" in a.get("rel", "").lower() and a.get("href") and not self.icon:
            self.icon = a["href"]
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self._in_title = False if tag == "title" else self._in_title

    def handle_data(self, data):
        if self._in_title and len(self.title) < 200:
            self.title += data


def page_preview(html, base):
    """Preview card data; only https image URLs are returned (the browser loads them directly)."""
    parser = MetaParser()
    parser.feed(html)
    m = parser.meta
    https = lambda u: u if u and urlsplit(u).scheme == "https" else None
    return {"url": base,
            "title": (m.get("og:title") or parser.title).strip()[:200] or None,
            "description": (m.get("og:description") or m.get("description") or "").strip()[:300] or None,
            "image": https(urljoin(base, m.get("og:image") or m.get("twitter:image") or "") if (m.get("og:image") or m.get("twitter:image")) else None),
            "icon": https(urljoin(base, parser.icon or "/favicon.ico"))}


def fetch_preview(domain, timeout=8):
    """Quick home-page fetch for the loading screen; None if the site is slow or unreachable."""
    res = fetch_page(f"https://{domain}/", timeout)
    if res is None or res.status != 200:
        return None
    return page_preview(res.body.decode("utf-8", errors="replace"), res.final_url or f"https://{domain}/")


def parse_links(html, base):
    parser = LinkParser()
    parser.feed(html)
    return [urljoin(base, h) for h in parser.links], [urljoin(base, h) for h in parser.feeds]


def site_host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def social_profiles(links):
    """{network: profile url}, first profile per network; share/intent links are not profiles."""
    found = {}
    for url in links:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        network = next((name for domain, name in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
        if network and parts.path.strip("/") and not any(m in url.lower() for m in SHARE_MARKERS):
            found.setdefault(network, url)
    return found


def find_blog(links, home):
    """Same-site blog index link (blog.* subdomain or /blog-like path); falls back to the index of a linked post."""
    root = site_host(home)
    post_index = None
    for url in links:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        path = parts.path.lower().rstrip("/")
        if host == "blog." + root or (host == root and path in BLOG_PATHS):
            return url
        if host == root and not post_index:
            post_index = next((urljoin(url, p) for p in BLOG_PATHS if path.startswith(p + "/")), None)
    return post_index


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)  # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom / sitemap (ISO 8601)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def feed_dates(xml_text):
    """Post dates from an RSS or Atom feed."""
    dates = []
    for item in ET.fromstring(xml_text).iter():
        if local(item.tag) in ("item", "entry"):
            fields = {local(c.tag): c.text for c in item}
            d = parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date"))
            if d:
                dates.append(d)
    return dates


def sitemap_entries(xml_text):
    """(kind, [(loc, lastmod)]) where kind is 'index' or 'urlset'."""
    root = ET.fromstring(xml_text)
    rows = []
    for node in root:
        fields = {local(c.tag): (c.text or "").strip() for c in node}
        if fields.get("loc"):
            rows.append((fields["loc"], fields.get("lastmod")))
    return ("index" if local(root.tag) == "sitemapindex" else "urlset"), rows


def activity(dates, now):
    dates = sorted((d for d in dates if d <= now + timedelta(days=1)), reverse=True)
    latest = dates[0] if dates else None
    return {"latest": latest.isoformat() if latest else None,
            "days_since_latest": (now - latest).days if latest else None,
            f"posts_last_{CADENCE_DAYS}_days": sum(d >= now - timedelta(days=CADENCE_DAYS) for d in dates),
            "dated_posts_found": len(dates)}


def build_checks(profiles, blog_url, feed_url, dates_source, act):
    networks = sorted(profiles)
    socials = ({"status": "pass", "message": f"Found {len(networks)} social profile(s): {', '.join(networks)}."} if networks
               else {"status": "fail", "message": "No links to social profiles found on the home page."})
    socials["details"] = {"profiles": profiles}
    has_blog = bool(blog_url or feed_url)
    blog = ({"status": "pass", "message": f"Blog found at {blog_url or feed_url}."} if has_blog
            else {"status": "fail", "message": "No blog link or RSS/Atom feed found on the home page."})
    blog["details"] = {"blog_url": blog_url, "feed_url": feed_url}
    recent_posts = act and act[f"posts_last_{CADENCE_DAYS}_days"]
    if not has_blog:
        recent = cadence = {"status": "fail", "message": "No blog to assess."}
    elif not act or act["latest"] is None:
        recent = cadence = {"status": "neutral", "message": "Blog found, but no post dates could be read (no feed or dated sitemap)."}
    else:
        days = act["days_since_latest"]
        recent = {"status": "pass" if days <= RECENT_DAYS else "fail",
                  "message": f"Latest post was {days} day(s) ago (target: within {RECENT_DAYS})."}
        cadence = {"status": "pass" if recent_posts >= CADENCE_MIN_POSTS else "fail",
                   "message": f"{recent_posts} post(s) in the last {CADENCE_DAYS} days (target: {CADENCE_MIN_POSTS}+)."}
    for check in (recent, cadence):
        check.setdefault("details", {"dates_source": dates_source, **(act or {})})
    return {"social": {"socialProfiles": socials},
            "content": {"blogExists": blog, "blogRecent": dict(recent), "blogCadence": dict(cadence)}}


class GrowthAdapter(Adapter):
    source_id = "growth"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    async def health(self) -> str:
        return "ok"

    def get(self, url):
        res = fetch_page(url, self.timeout)
        return res if res is not None and res.status == 200 else None

    def text(self, res):
        return res.body.decode("utf-8", errors="replace")

    def fetch_all(self, urls):
        """Fetch URLs in parallel: each new TLS connection can cost seconds, so never wait on them one by one."""
        urls = list(dict.fromkeys(u for u in urls if u))
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            return dict(zip(urls, pool.map(self.get, urls)))

    def run(self, domain):
        home = self.get(f"https://{domain}/")
        if not home:
            raise RuntimeError(f"Could not load https://{domain}/")
        base = home.final_url or f"https://{domain}/"
        links, feeds = parse_links(self.text(home), base)
        profiles = social_profiles(links)
        blog_url = find_blog(links, base)
        same_host = not blog_url or urlsplit(blog_url).hostname == urlsplit(base).hostname
        # A blog.* subdomain has its own sitemap; on the main site only URLs under the blog path count.
        sitemap_url = urljoin(base if same_host else blog_url, "/sitemap.xml")
        roots = ([blog_url.rstrip("/") + "/"] if blog_url else []) + [urljoin(base, "/")]
        guesses = [] if feeds else [urljoin(r, g) for r in roots for g in FEED_GUESSES]
        # One parallel wave: blog page (for its declared feed), feed candidates and the sitemap.
        pages = self.fetch_all(([blog_url] if blog_url and not feeds else []) + feeds + guesses + ([sitemap_url] if blog_url else []))
        blog_page = pages.get(blog_url)
        blog_feeds = parse_links(self.text(blog_page), blog_page.final_url or blog_url)[1] if blog_page else []
        pages.update(self.fetch_all([u for u in blog_feeds if u not in pages]))
        feed_url, dates, dates_source = None, [], None
        for url in feeds + blog_feeds + guesses:  # same priority as before: declared feeds first, then guesses
            try:
                found = feed_dates(self.text(pages[url])) if pages.get(url) else []
            except ET.ParseError:
                found = []
            if found:
                feed_url, dates, dates_source = url, found, "feed"
                break
        if not dates and blog_url:
            path = urlsplit(blog_url).path.rstrip("/") if same_host else ""
            dates, dates_source = self.sitemap_dates(pages.get(sitemap_url) or self.get(sitemap_url), path), "sitemap"
        now = datetime.now(timezone.utc)
        act = activity(dates, now) if dates else None
        return {"scanner": "pickable-growth 0.2", "url": base, "scannedAt": now.isoformat(),
                "checks": build_checks(profiles, blog_url, feed_url, dates_source if dates else None, act)}

    def sitemap_dates(self, res, blog_path):
        # ponytail: one sitemap level + one blog-ish child sitemap; deeper indexes are skipped
        try:
            kind, rows = sitemap_entries(self.text(res)) if res else ("urlset", [])
            if kind == "index":
                child = next((loc for loc, _ in rows if any(k in loc.lower() for k in ("blog", "post", "article", "news"))), None)
                res = self.get(child) if child else None
                kind, rows = sitemap_entries(self.text(res)) if res else ("urlset", [])
        except ET.ParseError:
            return []
        prefix = blog_path.lower() + "/" if blog_path else "/"
        return [d for loc, mod in rows if urlsplit(loc).path.lower().startswith(prefix) and (d := parse_date(mod))]

    @staticmethod
    def _checks_to_records(raw: dict, job_id: str, source_id: str, observed_at: str) -> list:
        """Flatten the nested build_checks dict into CheckRecord objects."""
        status_map = {"pass": "PASS", "fail": "FAIL", "neutral": "UNKNOWN"}
        records = []
        for group_key, group in (raw.get("checks") or {}).items():
            if not isinstance(group, dict):
                continue
            for check_id, check in group.items():
                if not isinstance(check, dict) or "status" not in check:
                    continue
                records.append(CheckRecord(
                    job_id=job_id,
                    source_id=source_id,
                    native_check_id=f"{group_key}.{check_id}",
                    canonical_check_id=None,
                    status=status_map.get(check["status"], "UNKNOWN"),
                    evidence=json.dumps(check),
                    observed_at=observed_at,
                ))
        return records

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, self.run, domain)
        except Exception as exc:
            return AdapterResult(self.source_id, "failed", None, [], str(exc))
        raw_json = json.dumps(raw)
        observed_at = raw.get("scannedAt", datetime.now(timezone.utc).isoformat())
        checks = self._checks_to_records(raw, job_id, self.source_id, observed_at)
        return AdapterResult(self.source_id, "completed", raw_json, checks)
