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

    def test_security_headers_present_and_stack_not_advertised(self):
        status, _, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn("Pickable", headers.get("Server", ""))
        self.assertIn("max-age=31536000", headers.get("Strict-Transport-Security", ""))
        self.assertEqual(headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertIn("camera=()", headers.get("Permissions-Policy", ""))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")


class OriginHardeningTests(unittest.TestCase):
    def _serve(self, env):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, env):
            server = make_server(0, demo_backend())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_port}"

    def _get(self, base, path, headers=None):
        request = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def test_require_cf_blocks_direct_but_allows_edge_and_health(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from server import _parse_trusted_proxies, make_server
        trusted = _parse_trusted_proxies("")

        def edge(path, peer, headers):
            handler = SimpleNamespace(client_address=(peer, 1234), headers=headers or {})
            with patch.dict(os.environ, {"PICKABLE_REQUIRE_CF": "1"}):
                server = make_server(0, demo_backend())
            try:
                return server.RequestHandlerClass._edge_allowed(handler, path)
            finally:
                server.server_close()

        # Direct internet client, no edge headers -> blocked (origin IP leak alone is useless).
        self.assertFalse(edge("/", "198.51.100.9", {}))
        # Proxy peer without edge headers (direct-to-Traefik) -> blocked.
        self.assertFalse(edge("/", "172.18.0.3", {"X-Forwarded-For": "198.51.100.9"}))
        # Real Cloudflare-routed request through the proxy -> allowed.
        self.assertTrue(edge("/", "172.18.0.3", {"CF-Ray": "abc-FRA", "CF-Connecting-IP": "203.0.113.7"}))
        # Loopback (VPS-side curl) and health probes stay open.
        self.assertTrue(edge("/", "127.0.0.1", {}))
        self.assertTrue(edge("/api/health", "198.51.100.9", {}))

        server, thread, base = self._serve({"PICKABLE_REQUIRE_CF": "1"})
        try:
            status, _, _ = self._get(base, "/api/health")
            self.assertEqual(status, 200)
            status, _, _ = self._get(base, "/", {"CF-Ray": "abc123-FRA", "CF-Connecting-IP": "203.0.113.7"})
            self.assertEqual(status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_forwarding_headers_trusted_only_behind_proxy(self):
        from types import SimpleNamespace
        from server import _parse_trusted_proxies, _real_client_ip
        trusted = _parse_trusted_proxies("")
        via_proxy = SimpleNamespace(client_address=("172.18.0.3", 1234),
                                    headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(_real_client_ip(via_proxy, trusted), "203.0.113.7")
        direct = SimpleNamespace(client_address=("198.51.100.9", 1234),
                                 headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(_real_client_ip(direct, trusted), "198.51.100.9")


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
