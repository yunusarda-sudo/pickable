import unittest
import tempfile
import os
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

from app.storage import Storage
from app.waitlist import (
    CLIENT_EVENT_NAMES,
    EVENT_NAMES,
    RateLimiter,
    RateLimitError,
    MemoryWaitlist,
    parse_event,
    parse_signup,
    honeypot_filled,
    normalize_email,
    signup_source,
    utc_now,
)


class TestWaitlistValidation(unittest.TestCase):
    def test_normalize_email_lowercases_and_trims(self):
        self.assertEqual(normalize_email("  YOU@Example.COM  "), "you@example.com")

    def test_normalize_email_rejects_blank_and_garbage(self):
        for bad in ("", "   ", "not-an-email", "no-domain@", "@", "a@b", "a b@x.com"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_email(bad)

    def test_signup_source_falls_back_to_landing_page(self):
        self.assertEqual(signup_source("scan_results"), "scan_results")
        self.assertEqual(signup_source("nonsense"), "landing_page")
        self.assertEqual(signup_source(None), "landing_page")

    def test_parse_signup_accepts_optional_website_and_source(self):
        email, website, source = parse_signup({"email": "a@b.com", "source": "scan_results"})
        self.assertEqual((email, website, source), ("a@b.com", None, "scan_results"))
        _, website, _ = parse_signup({"email": "a@b.com", "website": "https://a.b"})
        self.assertEqual(website, "https://a.b")

    def test_parse_signup_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            parse_signup(["a@b.com"])

    def test_honeypot_filled_detects_any_nonempty_company(self):
        self.assertTrue(honeypot_filled({"company": "spam"}))
        self.assertTrue(honeypot_filled({"company": "   spam   "}))
        self.assertFalse(honeypot_filled({"company": ""}))
        self.assertFalse(honeypot_filled({"company": None}))
        self.assertFalse(honeypot_filled({"email": "a@b.com"}))
        self.assertFalse(honeypot_filled("not a dict"))

    def test_parse_event_accepts_name_key(self):
        name, source = parse_event({"name": "waitlist_cta_viewed", "source": "landing_page"})
        self.assertEqual((name, source), ("waitlist_cta_viewed", "landing_page"))

    def test_parse_event_rejects_unknown_and_client_authored_outcomes(self):
        for bad in ({"name": "not_a_real_event"}, {"name": "waitlist_signup_succeeded"},
                    {"name": "waitlist_signup_failed"}, {}):
            with self.assertRaises(ValueError, msg=bad):
                parse_event(bad)
        # The signup outcomes still exist for the server to record, but clients
        # cannot author them.
        self.assertIn("waitlist_signup_succeeded", EVENT_NAMES)
        self.assertIn("waitlist_signup_failed", EVENT_NAMES)
        for name in CLIENT_EVENT_NAMES:
            self.assertEqual(parse_event({"name": name})[0], name)


class TestRateLimiter(unittest.TestCase):
    def test_per_peer_budget_then_retry_after(self):
        clock = {"now": 1000.0}
        limiter = RateLimiter(2, 100, "Slow down.", clock=lambda: clock["now"])
        limiter.admit("1.1.1.1")
        limiter.admit("1.1.1.1")
        with self.assertRaises(RateLimitError) as ctx:
            limiter.admit("1.1.1.1")
        self.assertGreaterEqual(ctx.exception.retry_after, 1)
        self.assertEqual(ctx.exception.message, "Slow down.")

    def test_other_peers_are_unaffected_until_global_budget(self):
        clock = {"now": 0.0}
        limiter = RateLimiter(1, 100, "Slow down.", clock=lambda: clock["now"])
        limiter.admit("1.1.1.1")
        limiter.admit("2.2.2.2")  # a different peer still fits

    def test_global_budget_is_shared_across_peers(self):
        clock = {"now": 0.0}
        limiter = RateLimiter(10, 2, "Slow down.", clock=lambda: clock["now"])
        limiter.admit("1.1.1.1")
        limiter.admit("2.2.2.2")
        with self.assertRaises(RateLimitError):
            limiter.admit("3.3.3.3")

    def test_budget_resets_after_an_hour(self):
        clock = {"now": 0.0}
        limiter = RateLimiter(1, 100, "Slow down.", clock=lambda: clock["now"])
        limiter.admit("1.1.1.1")
        with self.assertRaises(RateLimitError):
            limiter.admit("1.1.1.1")
        clock["now"] = 3601.0
        limiter.admit("1.1.1.1")  # the old entry expired, so this is admitted


class TestMemoryWaitlist(unittest.TestCase):
    def test_signup_is_deduplicated_by_email(self):
        store = MemoryWaitlist()
        self.assertTrue(store.signup("a@b.com"))
        self.assertFalse(store.signup("a@b.com"))
        self.assertEqual(len(store.rows), 1)

    def test_record_event_appends_rows(self):
        store = MemoryWaitlist()
        store.record_event("waitlist_cta_viewed", "landing_page")
        self.assertEqual(len(store.events), 1)
        self.assertEqual(store.events[0]["name"], "waitlist_cta_viewed")


class TestStorageWaitlist(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_signup_persists_and_deduplicates_across_restart(self):
        self.assertTrue(self.storage.add_waitlist_signup("a@b.com", None, "landing_page"))
        self.assertFalse(self.storage.add_waitlist_signup("a@b.com", None, "landing_page"))
        self.storage.close()

        reopened = Storage(self.db_path)
        try:
            rows = reopened.conn.execute("SELECT email, source FROM waitlist").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("a@b.com", "landing_page")])
            self.assertFalse(reopened.add_waitlist_signup("a@b.com", None, "landing_page"))
        finally:
            reopened.close()

    def test_parse_signup_then_storage_dedupes_case_insensitively(self):
        """Sunucu parse_signup ile kucuk harfe cevirir; storage bu normalize
        edilmis degerle UNIQUE index sayesinde tekillestirir."""
        first = parse_signup({"email": "A@Example.COM"})
        second = parse_signup({"email": "a@example.com"})
        self.assertEqual(first[0], "a@example.com")
        self.assertEqual(second[0], "a@example.com")

        self.assertTrue(self.storage.add_waitlist_signup(*first))
        self.assertFalse(self.storage.add_waitlist_signup(*second))
        rows = self.storage.conn.execute("SELECT email FROM waitlist").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("a@example.com",)])

    def test_signup_records_website_and_source(self):
        self.assertTrue(self.storage.add_waitlist_signup("a@b.com", "https://a.b", "scan_results"))
        row = self.storage.conn.execute(
            "SELECT website, source FROM waitlist WHERE email = ?", ("a@b.com",)
        ).fetchone()
        self.assertEqual(tuple(row), ("https://a.b", "scan_results"))

    def test_record_event_stores_name_and_source(self):
        self.storage.record_event("waitlist_cta_viewed", "scan_results")
        row = self.storage.conn.execute(
            "SELECT name, source FROM analytics_events"
        ).fetchone()
        self.assertEqual(tuple(row), ("waitlist_cta_viewed", "scan_results"))

    def test_utc_now_is_iso_formatted(self):
        stamp = utc_now()
        self.assertIn("T", stamp)


class TestLiveWaitlistAPI(unittest.TestCase):
    def test_signup_survives_backend_restart_and_stale_tokens_cannot_write(self):
        from app.config import Config
        from server import live_backend, make_server

        def request(base, path, body=None, token=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-CSRF-Token"] = token
            req = urllib.request.Request(
                base + path, headers=headers,
                data=json.dumps(body).encode() if body is not None else None,
            )
            try:
                response = urllib.request.urlopen(req, timeout=3)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        with tempfile.TemporaryDirectory() as tmp, patch.object(Config, "DB_PATH", os.path.join(tmp, "scan.db")):
            old_token = None
            for restart in range(2):
                backend = live_backend()
                server = make_server(0, backend)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    _, config = request(base, "/api/config")
                    token = config["csrf_token"]
                    if old_token:
                        status, data = request(base, "/api/waitlist", {"email": "lost@example.com"}, old_token)
                        self.assertEqual(status, 403)
                        self.assertEqual(data["code"], "invalid_csrf_token")
                    status, data = request(base, "/api/waitlist", {
                        "email": " Real@Example.COM ", "website": "example.com", "source": "scan_results",
                    }, token)
                    self.assertEqual(status, 201 if restart == 0 else 200)
                    self.assertEqual(data["status"], "created" if restart == 0 else "already_registered")
                    old_token = token
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                    backend["shutdown"]()
            storage = Storage(Config.DB_PATH)
            try:
                rows = storage.conn.execute("SELECT email, website, source FROM waitlist").fetchall()
                self.assertEqual([tuple(row) for row in rows], [("real@example.com", "https://example.com", "scan_results")])
                self.assertEqual(storage.conn.execute(
                    "SELECT COUNT(*) FROM analytics_events WHERE name='waitlist_signup_succeeded'"
                ).fetchone()[0], 1)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
