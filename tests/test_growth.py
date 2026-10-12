"""Marketing playbook checker: parsing and thresholds, no network."""
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import patch

from app.adapters.growth import GrowthAdapter, page_preview, activity, build_checks, feed_dates, find_blog, parse_links, sitemap_entries, social_profiles, fetch_page
from app.reporting import build_report
from app.safe_fetch import SafeFetchError, SSRFBlocked, TimeoutError as SafeFetchTimeout

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
HOME = "https://example.com/"


class GrowthParsingTests(unittest.TestCase):
    def test_socials_are_profiles_not_share_links_or_bare_domains(self):
        links, _ = parse_links('''<a href="https://www.linkedin.com/company/acme">in</a>
            <a href="https://twitter.com/intent/tweet?text=hi">share</a><a href="https://x.com/acme">x</a>
            <a href="https://facebook.com/sharer/sharer.php?u=1">fb share</a><a href="https://instagram.com/">bare</a>''', HOME)
        self.assertEqual(social_profiles(links), {"LinkedIn": "https://www.linkedin.com/company/acme", "X": "https://x.com/acme"})

    def test_blog_index_preferred_over_post_and_subdomain_recognised(self):
        self.assertEqual(find_blog(["https://example.com/blog/launch-day", "https://example.com/blog"], HOME), "https://example.com/blog")
        self.assertEqual(find_blog(["https://example.com/blog/launch-day"], HOME), "https://example.com/blog")
        self.assertEqual(find_blog(["https://blog.example.com/"], HOME), "https://blog.example.com/")
        self.assertIsNone(find_blog(["https://other.com/blog", "https://example.com/pricing"], HOME))

    def test_feed_links_resolved_and_rss_atom_dates_parsed(self):
        _, feeds = parse_links('<link rel="alternate" type="application/rss+xml" href="/blog/feed">', HOME)
        self.assertEqual(feeds, ["https://example.com/blog/feed"])
        rss = "<rss><channel><item><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><published>2026-09-01T00:00:00Z</published></entry></feed>'
        self.assertEqual([d.date().isoformat() for d in feed_dates(rss) + feed_dates(atom)], ["2026-10-05", "2026-09-01"])

    def test_sitemap_index_and_urlset_detected(self):
        kind, rows = sitemap_entries('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap></sitemapindex>')
        self.assertEqual((kind, rows[0][0]), ("index", "https://example.com/post-sitemap.xml"))

    def test_recent_and_cadence_thresholds(self):
        dates = [NOW - timedelta(days=d) for d in (10, 40, 80, 400)] + [NOW + timedelta(days=30)]  # future date ignored
        act = activity(dates, NOW)
        self.assertEqual((act["days_since_latest"], act["posts_last_90_days"]), (10, 3))
        content = build_checks({}, "https://example.com/blog", None, "sitemap", act)["content"]
        self.assertEqual((content["blogRecent"]["status"], content["blogCadence"]["status"]), ("pass", "pass"))
        stale = build_checks({}, "https://example.com/blog", None, "feed", activity([NOW - timedelta(days=100)], NOW))["content"]
        self.assertEqual((stale["blogRecent"]["status"], stale["blogCadence"]["status"]), ("fail", "fail"))
        undated = build_checks({}, "https://example.com/blog", None, None, None)["content"]
        self.assertEqual(undated["blogRecent"]["status"], "neutral")
        none = build_checks({}, None, None, None, None)
        self.assertEqual((none["social"]["socialProfiles"]["status"], none["content"]["blogExists"]["status"]), ("fail", "fail"))

    def test_marketing_section_scores_and_waits_for_pending_checker(self):
        raw = {"checks": build_checks({"X": "https://x.com/acme"}, None, None, None, None)}
        done = build_report("example.com", {"growth": {"status": "completed", "raw": raw}})["marketing"]
        self.assertEqual((done["score"]["total"], done["score"]["final"]), (40.0, True))
        waiting = build_report("example.com", {}, pending=["growth"])["marketing"]
        self.assertFalse(waiting["score"]["final"])
        self.assertTrue(all(c["scoring_status"] == "PENDING" for c in waiting["checks"]))

    def test_preview_reads_head_and_only_keeps_https_images(self):
        html = """<head><title> Acme </title><meta property="og:image" content="/og.png">
            <meta name="description" content="We sell things"><link rel="icon" href="http://example.com/i.ico"></head>"""
        preview = page_preview(html, HOME)
        self.assertEqual((preview["title"], preview["description"], preview["image"]), ("Acme", "We sell things", "https://example.com/og.png"))
        self.assertIsNone(preview["icon"])  # plain-http icon is dropped


class GrowthFetchFailureTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ALLOWED_HOSTS": "own.test",
                              "PICKABLE_PUBLIC_URL": "https://own.test",
                              "RENDER_EXTERNAL_HOSTNAME": ""}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_optional_fetch_failures_remain_none(self):
        for error in (SafeFetchTimeout("request timed out"), SafeFetchError("connection reset"),
                      SSRFBlocked("private address")):
            with self.subTest(error=type(error).__name__):
                with patch("app.adapters.growth.safe_fetch", side_effect=error), \
                     patch("app.adapters.growth.internal_fetch") as internal:
                    self.assertIsNone(fetch_page(HOME, timeout=1))
                    internal.assert_not_called()

    def test_required_timeout_preserves_original_exception(self):
        error = SafeFetchTimeout("homepage deadline exceeded")
        with patch("app.adapters.growth.safe_fetch", side_effect=error) as public, \
             patch("app.adapters.growth.internal_fetch") as internal:
            with self.assertRaises(SafeFetchTimeout) as caught:
                fetch_page(HOME, timeout=1, required=True)
        self.assertIs(caught.exception, error)
        public.assert_called_once()
        internal.assert_not_called()

    def test_required_own_host_failure_preserves_public_error_after_fallback(self):
        for error in (SafeFetchTimeout("public timeout"), SafeFetchError("public connection reset")):
            with self.subTest(error=type(error).__name__):
                with patch("app.adapters.growth.safe_fetch", side_effect=error), \
                     patch("app.adapters.growth.internal_fetch", return_value=None) as internal:
                    with self.assertRaises(type(error)) as caught:
                        fetch_page("https://own.test/", timeout=1, required=True)
                self.assertIs(caught.exception, error)
                internal.assert_called_once()

    def test_required_foreign_ssrf_failure_does_not_retry_or_use_loopback(self):
        error = SSRFBlocked("destination resolved to a private address")
        with patch("app.adapters.growth.safe_fetch", side_effect=error) as public, \
             patch("app.adapters.growth.internal_fetch") as internal:
            with self.assertRaises(SSRFBlocked) as caught:
                fetch_page(HOME, timeout=1, required=True)
        self.assertIs(caught.exception, error)
        public.assert_called_once()
        internal.assert_not_called()

    def test_scan_homepage_timeout_retains_timeout_status_and_cause(self):
        error = SafeFetchTimeout("homepage deadline exceeded")
        with patch("app.adapters.growth.safe_fetch", side_effect=error) as public:
            result = asyncio.run(GrowthAdapter(timeout=1).scan("example.com", "job-timeout"))
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.error, str(error))
        self.assertIsNone(result.raw_report)
        self.assertEqual(result.checks, [])
        public.assert_called_once()


class OwnHostFallbackTests(unittest.TestCase):
    """Public egress'e kapalı kendi sitemiz loopback üzerinden okunabilmeli."""

    @staticmethod
    def _serve(body):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_unreachable_own_host_falls_back_to_loopback(self):
        server = self._serve("<html><head><title>Own</title></head></html>")
        try:
            env = {"PORT": str(server.server_port), "ALLOWED_HOSTS": "own.test",
                   "PICKABLE_PUBLIC_URL": "https://own.test", "RENDER_EXTERNAL_HOSTNAME": ""}
            with patch.dict(os.environ, env, clear=False):
                res = fetch_page("https://own.test/", timeout=8)
            self.assertIsNotNone(res)
            self.assertEqual(res.status, 200)
            self.assertEqual(res.final_url, "https://own.test/")
            self.assertIn(b"Own", res.body)
        finally:
            server.shutdown()
            server.server_close()

    def test_foreign_unreachable_host_stays_unreachable(self):
        with patch.dict(os.environ, {"ALLOWED_HOSTS": "", "PICKABLE_PUBLIC_URL": "",
                                     "RENDER_EXTERNAL_HOSTNAME": ""}, clear=False):
            self.assertIsNone(fetch_page("https://nonexistent-xyz123.invalid/", timeout=5))

    def test_redirect_is_not_followed_to_another_host(self):
        """Loopback yanıtı 301 bile olsa Location takip edilmez (SSRF zinciri yok)."""
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(301)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {"PORT": str(server.server_port), "ALLOWED_HOSTS": "own.test",
                   "PICKABLE_PUBLIC_URL": "https://own.test", "RENDER_EXTERNAL_HOSTNAME": ""}
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNone(fetch_page("https://own.test/", timeout=8))
        finally:
            server.shutdown()
            server.server_close()

    def test_host_with_port_entry_still_matches(self):
        server = self._serve("<html><head><title>Own</title></head></html>")
        try:
            env = {"PORT": str(server.server_port), "ALLOWED_HOSTS": "own.test:443",
                   "PICKABLE_PUBLIC_URL": "", "RENDER_EXTERNAL_HOSTNAME": ""}
            with patch.dict(os.environ, env, clear=False):
                res = fetch_page("https://www.own.test/", timeout=8)
            self.assertIsNotNone(res)
            self.assertEqual(res.final_url, "https://www.own.test/")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
