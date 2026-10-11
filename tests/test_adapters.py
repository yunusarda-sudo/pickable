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


def _ora_payload(status="pass", **overrides):
    payload = {"layers": [{"id": "content", "checks": [
        {"id": "llms-txt-exists", "status": status, "details": "TEST"}]}]}
    payload.update(overrides)
    return json.dumps(payload)


def _ora_response(raw):
    return FetchResult(status=200, headers={"Content-Type": "application/json"},
                       body=raw.encode("utf-8"), redirect_chain=[], resolved_ips=["93.184.216.34"])


class TestOraFreshness(unittest.IsolatedAsyncioTestCase):
    async def scan_with(self, raws):
        calls = []
        responses = [_ora_response(raw) for raw in raws]

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return responses[min(len(calls) - 1, len(responses) - 1)]

        adapter = OraAdapter(api_key="test_key", base_url="https://ora.ai/api")
        with patch("app.adapters.ora.safe_fetch", fake_fetch):
            result = await adapter.scan("example.com", "job_1")
        return result, calls

    @staticmethod
    def post_bodies(calls):
        return [json.loads(call[1]["body"].decode("utf-8")) for call in calls]

    async def test_stale_cache_triggers_single_forced_refresh(self):
        stale = _ora_payload("fail", servedFromCache=True, resultAgeSeconds=9999)
        fresh = _ora_payload("pass", scannedAt="2026-10-11T12:00:00Z")
        result, calls = await self.scan_with([stale, fresh])

        bodies = self.post_bodies(calls)
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("force", bodies[0])
        self.assertTrue(bodies[1].get("force"))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.raw_report, fresh)

    async def test_fresh_cache_is_reused_without_force(self):
        cached = _ora_payload("pass", servedFromCache=True, resultAgeSeconds=10)
        result, calls = await self.scan_with([cached])

        bodies = self.post_bodies(calls)
        self.assertEqual(len(bodies), 1)
        self.assertNotIn("force", bodies[0])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.raw_report, cached)

    async def test_uncached_result_needs_no_refresh(self):
        plain = _ora_payload("pass", scannedAt="2026-10-11T12:00:00Z")
        result, calls = await self.scan_with([plain])

        self.assertEqual(len(self.post_bodies(calls)), 1)
        self.assertEqual(result.status, "completed")

    async def test_persistently_stale_cache_is_used_after_one_refresh(self):
        stale = _ora_payload("fail", servedFromCache=True, resultAgeSeconds=9999)
        result, calls = await self.scan_with([stale, stale])

        bodies = self.post_bodies(calls)
        self.assertEqual(len(bodies), 2)
        self.assertTrue(bodies[1].get("force"))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.raw_report, stale)


if __name__ == "__main__":
    unittest.main()
