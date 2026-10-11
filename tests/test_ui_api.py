import json
import threading
import unittest
import urllib.request
import urllib.error
from server import make_server, demo_backend


class UIPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0, demo_backend())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def test_create_read_download_history_preserve_evidence(self):
        _, raw, _ = self.request("/api/config")
        config = json.loads(raw)
        self.assertEqual(config["mode"], "demo")
        status, raw, _ = self.request("/api/scans", {"domain": "example.com"}, {"Content-Type": "application/json", "X-CSRF-Token": config["csrf_token"]})
        self.assertEqual(status, 202)
        job_id = json.loads(raw)["job_id"]
        _, raw, _ = self.request(f"/api/scans/{job_id}")
        job = json.loads(raw)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(job["report"]["checks"]), 7)
        self.assertEqual(job["report"]["checks"][1]["combined_status"], "FAIL")
        raw_status, raw, _ = self.request(f"/api/scans/{job_id}/raw/ora")
        self.assertEqual(raw_status, 404)
        self.assertNotIn(b"provider payload", raw)
        _, raw, _ = self.request("/api/scans")
        self.assertIn(job_id, raw.decode())
        _, raw, _ = self.request(f"/api/scans/{job_id}/preview")
        self.assertEqual(json.loads(raw)["preview"]["url"], "https://example.com/")

    def test_scan_without_token_is_rejected(self):
        status, _, _ = self.request("/api/scans", {"domain": "example.com"}, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_deep_analysis_in_demo_returns_unavailable(self):
        _, raw, _ = self.request("/api/config")
        token = json.loads(raw)["csrf_token"]
        _, raw, _ = self.request("/api/scans", {"url": "https://example.com/"},
                                 {"Content-Type": "application/json", "X-CSRF-Token": token})
        job_id = json.loads(raw)["job_id"]
        status, raw, _ = self.request(f"/api/scans/{job_id}/intelligence", {}, {"X-CSRF-Token": token})
        self.assertEqual(status, 409)
        self.assertIn("unavailable", json.loads(raw)["error"].lower())

    def test_host_and_origin_are_rejected(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("/api/config", headers=headers)
                self.assertEqual(status, 403)

    def test_invalid_domain_and_oversized_body_are_rejected(self):
        _, raw, _ = self.request("/api/config")
        headers = {"Content-Type": "application/json", "X-CSRF-Token": json.loads(raw)["csrf_token"]}
        for domain in ("127.0.0.1", "a" * 3000):
            with self.subTest(domain=domain[:20]):
                status, _, _ = self.request("/api/scans", {"domain": domain}, headers)
                self.assertEqual(status, 413 if len(domain) > 2048 else 400)

    def test_static_files_and_missing_routes(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"pickable", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/unknown")[0], 404)


if __name__ == "__main__": unittest.main()


class LiveBackendTests(unittest.TestCase):
    def test_queued_scan_completes_with_report_and_raw(self):
        import tempfile, time
        from unittest.mock import patch
        from app.adapters.base import AdapterResult
        from app.config import Config
        from server import live_backend
        raw = json.dumps({"layers": [{"checks": [{"id": "sitemap", "status": "pass"}, {"id": "link-headers-discovery", "status": "warning"}]}]})
        cf_raw = json.dumps({"checks": {"discoverability": {"linkHeaders": {"status": "fail"}}}})
        async def fake_scan(self, domain, job_id):
            return AdapterResult(source_id="ora", status="completed", raw_report=raw, checks=[], error=None)
        async def fake_growth_scan(self, domain, job_id):
            return AdapterResult(source_id="growth", status="failed", raw_report=None, checks=[], error="offline")
        async def fake_cf_scan(self, domain, job_id):
            return AdapterResult(source_id="cloudflare", status="completed", raw_report=cf_raw, checks=[], error=None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Config, "DB_PATH", tmp + "/scan.db"), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "openai"}), \
                patch("app.adapters.ora.OraAdapter.scan", fake_scan), \
                patch("app.adapters.cloudflare.CloudflareAdapter.scan", fake_cf_scan), \
                patch("app.adapters.growth.GrowthAdapter.scan", fake_growth_scan), \
                patch("app.intelligence.fetch_evidence", side_effect=RuntimeError("test offline")):
            backend = live_backend()
            job_id = backend["create"]("example.com")
            for _ in range(50):
                job = backend["get"](job_id)
                if job["status"] == "completed": break
                time.sleep(0.05)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["report"]["checks"][0]["scoring_status"], "PASS")
            self.assertEqual(job["report"]["checks"][0]["scoring_source"], "ora")
            # Ora only warned on link headers, so Cloudflare's definitive FAIL is the fallback
            self.assertEqual(job["report"]["checks"][1]["scoring_status"], "FAIL")
            self.assertEqual(job["report"]["checks"][1]["scoring_source"], "cloudflare")
            self.assertEqual(backend["raw"](job_id, "ora")["layers"][0]["checks"][0]["id"], "sitemap")
            self.assertEqual(backend["list"]()[0]["id"], job_id)
            # Keep the temporary database alive until the background analysis exits.
            for _ in range(50):
                analysis = backend["intelligence"](job_id)
                if analysis and analysis["status"] == "failed": break
                time.sleep(0.01)
            self.assertEqual(analysis["status"], "failed")
            backend["shutdown"]()
