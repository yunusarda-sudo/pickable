"""Regression tests for the agent-discovery fixes.

Covers Content Signals in robots.txt, the auth.md heading requirement,
the honest absence of OAuth discovery metadata, WebMCP exposure markers,
and the DNS-AID operator runbook.
"""

import re
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.agent_policy import AgentPolicy
from server import make_server


ROOT = Path(__file__).resolve().parent.parent


def _minimal_backend():
    return {
        "mode": "test",
        "create": lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not start scans")),
        "get": lambda job_id, owner_ip=None: None,
        "raw": lambda _job_id, _source: None,
        "preview": lambda _job_id: None,
        "list": lambda owner_ip=None: [],
    }


class AgentDiscoveryFixesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0, _minimal_backend(), policy=AgentPolicy())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, timeout=3):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def test_robots_txt_declares_content_signals(self):
        status, body, _ = self.request("/robots.txt")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        match = re.search(r"^Content-Signal:(.*)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "robots.txt must contain a Content-Signal directive")
        directives = match.group(1).lower()
        for token in ("ai-train", "search", "ai-input"):
            self.assertIn(token, directives)

    def test_auth_md_heading_contains_auth_md_and_states_no_oauth(self):
        status, body, headers = self.request("/auth.md")
        self.assertEqual(status, 200)
        self.assertIn("markdown", headers.get("Content-Type", "").lower())
        text = body.decode("utf-8")
        heading = next((line for line in text.splitlines() if line.startswith("#")), "")
        self.assertIn("auth.md", heading.lower())
        lowered = text.lower()
        self.assertIn("no oauth", lowered)
        self.assertIn("csrf", lowered)

    def test_oauth_discovery_endpoints_are_honestly_absent(self):
        for path in (
            "/.well-known/openid-configuration",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
        ):
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_webmcp_markers_present_in_served_assets(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("toolname=", html)
        self.assertIn("tooldescription", html)
        self.assertIn("toolparamdescription", html)
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("registerTool", js)
        self.assertIn("modelContext", js)
        self.assertIn("AbortController", js)
        for tool in ("pickable_start_scan", "pickable_list_scans", "pickable_get_scan_report"):
            self.assertIn(tool, js)
            self.assertIn(tool, html if tool == "pickable_start_scan" else js)

    def test_dns_aid_runbook_exists_with_zone_and_dnssec(self):
        doc = ROOT / "docs" / "DNS-AID.md"
        self.assertTrue(doc.is_file(), "docs/DNS-AID.md must exist")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("SVCB", text)
        self.assertIn("_agents", text)
        self.assertIn("key65", text)  # numeric keyNNNNN experimental params
        self.assertIn("DNSSEC", text)


if __name__ == "__main__":
    unittest.main()
