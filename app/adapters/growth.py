"""Marketing playbook checks read from the site itself: social profiles and blog activity.

Stdlib only. Every request goes through safe_fetch (public hosts, pinned DNS, size/time limits).
Raw report uses the {"checks": {group: {id: {status, message, details}}}} shape so reporting can flatten it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from app.adapters.base import Adapter, AdapterResult
from app.safe_fetch import safe_fetch

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
# Calibration knobs: what "recent" and "regular" mean for a marketing blog.
RECENT_DAYS = 60
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
        try:
            res = safe_fetch(url, headers=HEADERS, timeout=self.timeout)
        except Exception:
            return None
        return res if res.status == 200 else None

    def text(self, res):
        return res.body.decode("utf-8", errors="replace")

    def run(self, domain):
        home = self.get(f"https://{domain}/")
        if not home:
            raise RuntimeError(f"Could not load https://{domain}/")
        base = home.final_url or f"https://{domain}/"
        links, feeds = parse_links(self.text(home), base)
        profiles = social_profiles(links)
        blog_url = find_blog(links, base)
        if blog_url and not feeds and (page := self.get(blog_url)):
            feeds = parse_links(self.text(page), page.final_url or blog_url)[1]
        roots = [blog_url.rstrip("/") + "/"] if blog_url else []
        candidates = feeds or [urljoin(r, g) for r in roots + [urljoin(base, "/")] for g in FEED_GUESSES]
        feed_url, dates, dates_source = None, [], None
        for url in candidates[:8]:
            res = self.get(url)
            try:
                found = feed_dates(self.text(res)) if res else []
            except ET.ParseError:
                found = []
            if found:
                feed_url, dates, dates_source = url, found, "feed"
                break
        if not dates and blog_url:
            same_host = urlsplit(blog_url).hostname == urlsplit(base).hostname
            # A blog.* subdomain has its own sitemap; on the main site only URLs under the blog path count.
            dates = self.sitemap_dates(base if same_host else blog_url, urlsplit(blog_url).path.rstrip("/") if same_host else "")
            dates_source = "sitemap"
        now = datetime.now(timezone.utc)
        act = activity(dates, now) if dates else None
        return {"scanner": "pickable-growth 0.1", "url": base, "scannedAt": now.isoformat(),
                "checks": build_checks(profiles, blog_url, feed_url, dates_source if dates else None, act)}

    def sitemap_dates(self, base, blog_path):
        # ponytail: one sitemap level + one blog-ish child sitemap; deeper indexes are skipped
        res = self.get(urljoin(base, "/sitemap.xml"))
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

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        raw = await asyncio.get_running_loop().run_in_executor(None, self.run, domain)
        return AdapterResult(self.source_id, "completed", json.dumps(raw), [])
