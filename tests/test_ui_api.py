import json
import threading
import unittest
import urllib.request
import urllib.error
from ui_preview import make_server


class UIPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
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
        self.assertEqual(job["report"]["checks"][1]["combined_status"], "DIFFERENT")
        _, raw, headers = self.request(f"/api/scans/{job_id}/raw/ora")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(json.loads(raw)["layers"][0]["checks"][1]["status"], "fail")
        _, raw, _ = self.request("/api/scans")
        self.assertIn(job_id, raw.decode())

    def test_scan_without_token_is_rejected(self):
        status, _, _ = self.request("/api/scans", {"domain": "example.com"}, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

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
                self.assertEqual(status, 400)

    def test_static_files_and_missing_routes(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"pickable", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/unknown")[0], 404)


if __name__ == "__main__": unittest.main()
