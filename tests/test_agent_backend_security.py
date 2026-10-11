"""Regression checks for shared scanner resources exposed through the agent API."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch, MagicMock
import socket

from app.adapters.ora import OraAdapter
from app.safe_fetch import FetchResult, safe_fetch, SSRFBlocked
from app.storage import Storage


class ProviderDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_url_cannot_exfiltrate_provider_authorization(self):
        response = FetchResult(202, {}, b'{"poll_url":"https://attacker.example/poll"}')
        with patch("app.adapters.ora.safe_fetch", return_value=response) as fetch:
            with self.assertRaisesRegex(ValueError, "provider origin"):
                await OraAdapter(api_key="test-only-secret").scan("example.com", "test-job")
        self.assertEqual(fetch.call_count, 1)

    async def test_pending_provider_has_a_real_deadline(self):
        response = FetchResult(202, {}, b'{"status":"pending"}')
        with patch("app.adapters.ora.safe_fetch", return_value=response) as fetch:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(OraAdapter(timeout=0.03).scan("example.com", "test-job"), timeout=0.5)
        self.assertEqual(fetch.call_count, 1)

    async def test_nonobject_provider_report_is_not_success(self):
        with patch("app.adapters.ora.safe_fetch", return_value=FetchResult(200, {}, b'[1,2]')):
            result = await OraAdapter().scan("example.com", "test-job")
        self.assertEqual(result.status, "failed")


class ConcurrentStorageTests(unittest.TestCase):
    def test_parallel_requests_and_worker_updates_preserve_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp + "/scans.db")
            def write(i):
                job_id = storage.create_job(f"example{i}.com")
                storage.update_job_status(job_id, "completed")
                return job_id
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    ids = list(pool.map(write, range(80)))
                self.assertEqual(len(set(ids)), 80)
                self.assertTrue(all(storage.get_job(job_id)["status"] == "completed" for job_id in ids))
            finally:
                storage.close()


class CredentialRedirectTests(unittest.TestCase):
    def test_cross_origin_redirect_removes_credentials_and_recomputes_host(self):
        first, second = MagicMock(), MagicMock()
        first.status = 302
        first.getheader.side_effect = lambda name, default=None: "http://other.example/report" if name == "Location" else default
        second.status = 200
        second.getheader.return_value = None
        second.getheaders.return_value = []
        second.read.side_effect = [b"OK", b""]
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]), \
                patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = [first, second]
            safe_fetch("http://provider.example/scan", headers={"Authorization": "test-only-secret", "Cookie": "private",
                "Proxy-Authorization": "proxy-secret", "X-API-Key": "custom-secret", "Host": "provider.example"})
            sent = connection.return_value.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", sent)
            self.assertNotIn("Cookie", sent)
            self.assertNotIn("Proxy-Authorization", sent)
            self.assertNotIn("X-API-Key", sent)
            self.assertEqual(sent["Host"], "other.example")

    def test_https_redirect_cannot_downgrade_transport(self):
        response = MagicMock()
        response.status = 302
        response.getheader.side_effect = lambda name, default=None: "http://provider.example/report" if name == "Location" else default
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), \
                patch("http.client.HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(SSRFBlocked):
                safe_fetch("https://provider.example/scan")


class PublishedReportContractTests(unittest.TestCase):
    def test_required_report_fields_cover_actual_readiness_and_marketing_shapes(self):
        from server import demo_backend
        backend = demo_backend()
        report = backend["get"](backend["create"]("example.com"))["report"]
        schemas = json.loads((Path(__file__).resolve().parents[1] / "web/openapi.json").read_text())["components"]["schemas"]
        readiness = schemas["UnifiedReport"]["properties"]["checks"]["items"]["$ref"].split("/")[-1]
        marketing = schemas["MarketingReport"]["properties"]["checks"]["items"]["$ref"].split("/")[-1]
        for name, rows in ((readiness, report["checks"]), (marketing, report["marketing"]["checks"])):
            for row in rows:
                with self.subTest(schema=name, check=row["check_id"]):
                    self.assertTrue(set(schemas[name]["required"]).issubset(row))

    def test_mcp_response_size_bound_applies_to_actual_wire_serialization(self):
        from server import make_server
        from app.agent_policy import AgentPolicy
        from app.pickable_agent_routes import MAX_MCP_RESPONSE_BYTES
        job = {"id": "abcdefgh1234", "status": "completed", "domain": "example.com", "sources": [],
               "report": {"checks": [{"title": "x", "reason": "a" * 100} for _ in range(3000)]}}
        server = make_server(0, {"mode": "demo", "get": lambda _, owner_ip=None: job}, policy=AgentPolicy())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_scan_report", "arguments": {"job_id": job["id"]}}}
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/mcp", data=json.dumps(message).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
            with urllib.request.urlopen(request, timeout=3) as response:
                wire = response.read()
                self.assertLessEqual(len(wire), MAX_MCP_RESPONSE_BYTES)
                self.assertTrue(json.loads(wire)["result"]["structuredContent"]["truncation"]["truncated"])
        finally:
            server.shutdown(); server.server_close(); thread.join()
