"""Marketing playbook checker: parsing and thresholds, no network."""
from datetime import datetime, timedelta, timezone
import unittest

from app.adapters.growth import page_preview, activity, build_checks, feed_dates, find_blog, parse_links, sitemap_entries, social_profiles
from app.reporting import build_report

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


if __name__ == "__main__":
    unittest.main()
