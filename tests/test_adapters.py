import unittest
import json
import os
from unittest.mock import patch, MagicMock
from app.adapters.ora import OraAdapter, TokenBucket
from app.adapters.cloudflare import CloudflareAdapter
from app.safe_fetch import FetchResult


class TestAdapters(unittest.IsolatedAsyncioTestCase):
    async def cloudflare_result(self, raw):
        mock_fetch = MagicMock(return_value=FetchResult(
            status=200, headers={"Content-Type": "application/json"},
            body=raw.encode("utf-8"), redirect_chain=[], resolved_ips=["93.184.216.34"],
        ))
        with patch("app.adapters.cloudflare.safe_fetch", mock_fetch):
            return await CloudflareAdapter().scan("example.com", "job_123")

    async def test_cloudflare_accepts_grouped_status_bearing_checks(self):
        raw = json.dumps({"url": "https://example.com", "score": 42, "checks": {
            "discoverability": {"sitemap": {"status": "pass", "message": "Found sitemap"},
                                "linkHeaders": {"status": "fail"}},
            "content": {"markdownNegotiation": {"status": "pass"}},
            "authentication": {"oauthDiscovery": {"status": "skip"}},
        }})
        result = await self.cloudflare_result(raw)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source_id, "cloudflare")
        self.assertEqual(result.raw_report, raw)

    async def test_cloudflare_rejects_malformed_empty_and_error_payloads_preserving_raw(self):
        payloads = ["{bad", "null", "[]", "{}", '{"error":"Scan failed"}',
                    '{"checks":{}}', '{"checks":[]}',
                    '{"checks":{"discoverability":{"sitemap":{"message":"missing status"}}}}',
                    '{"checks":{"discoverability":{"sitemap":"pass"}}}']
        for raw in payloads:
            with self.subTest(raw=raw):
                result = await self.cloudflare_result(raw)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.raw_report, raw)
                self.assertEqual(result.checks, [])
                self.assertTrue(result.error)

    async def test_ora_adapter_success_mapping(self):
        sample_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "ora_sample.json"
        )
        with open(sample_path, "r", encoding="utf-8") as f:
            sample_json = f.read()

        adapter = OraAdapter(api_key="test_key", base_url="https://ora.ai/api")
        mock_fetch = MagicMock(return_value=FetchResult(
            status=200,
            headers={"Content-Type": "application/json"},
            body=sample_json.encode("utf-8"),
            redirect_chain=[],
            resolved_ips=["93.184.216.34"],
        ))

        with patch("app.adapters.ora.safe_fetch", mock_fetch):
            result = await adapter.scan("randevuo.com.tr", "job_123")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source_id, "ora")
        self.assertEqual(result.raw_report, sample_json)
        self.assertEqual(len(result.checks), 3)

        checks_by_id = {c.native_check_id: c for c in result.checks}
        self.assertEqual(checks_by_id["robots_txt_exists"].status, "PASS")
        self.assertEqual(checks_by_id["llms_txt_exists"].status, "FAIL")
        self.assertEqual(checks_by_id["sitemap_xml_exists"].status, "WARNING")


if __name__ == "__main__":
    unittest.main()
