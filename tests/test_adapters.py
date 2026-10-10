import unittest
import json
import os
from unittest.mock import patch, MagicMock
from app.adapters.ora import OraAdapter, TokenBucket
from app.adapters.agentready_oss import AgentReadyOSSAdapter
from app.safe_fetch import FetchResult


class TestAdapters(unittest.IsolatedAsyncioTestCase):
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

    async def test_oss_adapter_sample_parsing(self):
        sample_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "oss_sample.json"
        )
        with open(sample_path, "r", encoding="utf-8") as f:
            sample_json = f.read()

        adapter = AgentReadyOSSAdapter()

        def fake_subprocess(target_url, output_path, temp_dir):
            with open(output_path, "w", encoding="utf-8") as out:
                out.write(sample_json)

        with patch.object(adapter, "_run_subprocess", side_effect=fake_subprocess):
            result = await adapter.scan("randevuo.com.tr", "job_123")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source_id, "agentready_oss")
        self.assertEqual(result.raw_report, sample_json)
        checks_by_id = {c.native_check_id: c for c in result.checks}
        self.assertEqual(checks_by_id["robots_txt"].status, "PASS")
        self.assertEqual(checks_by_id["llms_txt"].status, "FAIL")
        self.assertEqual(checks_by_id["rate_limiting"].status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
