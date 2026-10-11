import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class IntelligenceFlowTests(unittest.TestCase):
    def _assert_preliminary_then_provider_outcome(self, provider_fails):
        from app.adapters.base import AdapterResult
        from app.config import Config
        from server import live_backend

        evidence_ready = threading.Event()
        release_extra_pages = threading.Event()
        provider_started = threading.Event()
        release_provider = threading.Event()
        evidence_time = []

        class FakeProvider:
            model = "test-model"
            api_key = "configured"
            provider = "test"
            def analyze(self, evidence, depth, scan_context):
                provider_started.set()
                if not release_provider.wait(2):
                    raise TimeoutError("test provider gate timed out")
                if provider_fails:
                    raise RuntimeError("simulated model outage")
                return {"product_summary": "Model-verified product summary.", "target_customers": "Small teams",
                        "problem": "Manual coordination", "value_proposition": "One shared calendar",
                        "competitive_landscape": "Not assessed.", "workflows": ["Create an appointment"],
                        "evidence_refs": ["homepage"], "unknowns": [], "sources": [], "web_sources": []}

        async def completed_scan(self, domain, job_id):
            return AdapterResult(source_id=self.source_id, status="completed", raw_report="{}", checks=[], error=None)

        evidence = {"url": "https://example.com/", "retrieved_at": "2026-10-11T00:00:00Z", "sha256": "abc",
                    "ref": "homepage", "title": "TeamFlow", "description": "Planning for distributed teams",
                    "visible_text": "Keep teams aligned."}

        def fetch_with_signal(*args, **kwargs):
            callback = kwargs.get("on_homepage")
            if callback:
                evidence_time.append(time.monotonic())
                callback(evidence)
            evidence_ready.set()
            if not release_extra_pages.wait(2):
                raise TimeoutError("test page discovery gate timed out")
            return evidence

        with tempfile.TemporaryDirectory() as tmp, patch.object(Config, "DB_PATH", tmp + "/scan.db"), \
                patch("app.intelligence.IntelligenceProvider", FakeProvider), \
                patch("app.intelligence.fetch_evidence", side_effect=fetch_with_signal), \
                patch("app.adapters.ora.OraAdapter.scan", completed_scan), \
                patch("app.adapters.cloudflare.CloudflareAdapter.scan", completed_scan), \
                patch("app.adapters.growth.GrowthAdapter.scan", completed_scan):
            backend = live_backend()
            scan_id = backend["create"]("example.com")
            self.assertTrue(evidence_ready.wait(2), "homepage evidence was not fetched")

            job = backend["get"](scan_id)
            preliminary = job["intelligence"]
            self.assertIsNotNone(preliminary)
            self.assertEqual(preliminary["status"], "running")
            self.assertIsNotNone(preliminary["report"], "preliminary report must be persisted before model enrichment completes")
            self.assertEqual(preliminary["report"]["analysis_stage"], "preliminary")
            self.assertEqual(preliminary["report"]["evidence_refs"], ["homepage"])
            self.assertEqual(preliminary["report"]["sources"][0]["url"], evidence["url"])
            self.assertLess(time.monotonic() - evidence_time[0], 3.0,
                            "first useful, source-linked response must be available within three seconds of first evidence")

            release_extra_pages.set()
            self.assertTrue(provider_started.wait(2), "model enrichment did not start")
            release_provider.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                job = backend["get"](scan_id)
                if job["intelligence"]["status"] in ("completed", "partial", "failed"):
                    break
                time.sleep(.01)
            backend["shutdown"]()
            return job["intelligence"]

    def test_preliminary_report_survives_model_error(self):
        intelligence = self._assert_preliminary_then_provider_outcome(provider_fails=True)

        self.assertEqual(intelligence["status"], "partial")
        self.assertEqual(intelligence["report"]["analysis_stage"], "preliminary")
        self.assertEqual(intelligence["report"]["sources"][0]["url"], "https://example.com/")

    def test_model_result_replaces_preliminary_report(self):
        intelligence = self._assert_preliminary_then_provider_outcome(provider_fails=False)

        self.assertEqual(intelligence["status"], "completed")
        self.assertEqual(intelligence["report"]["analysis_stage"], "complete")
        self.assertEqual(intelligence["report"]["product_summary"], "Model-verified product summary.")

    def test_completed_scan_queues_persistent_public_short_analysis(self):
        from app.adapters.base import AdapterResult
        from app.config import Config
        from server import live_backend

        class FakeProvider:
            model = "test-model"
            api_key = "configured"
            def analyze(self, evidence, depth, scan_context):
                return {"product_summary": "A scheduling service.", "target_customers": "Small teams",
                        "problem": "Manual coordination", "value_proposition": "One shared calendar",
                        "competitive_landscape": "Not assessed.", "workflows": ["Create an appointment"],
                        "evidence_refs": ["homepage"], "unknowns": [], "sources": [], "web_sources": []}

        async def scan_ora(self, domain, job_id):
            return AdapterResult(source_id="ora", status="completed", raw_report="{}", checks=[], error=None)
        async def scan_cloudflare(self, domain, job_id):
            return AdapterResult(source_id="cloudflare", status="completed", raw_report="{}", checks=[], error=None)
        async def scan_growth(self, domain, job_id):
            return AdapterResult(source_id="growth", status="completed", raw_report="{}", checks=[], error=None)

        evidence = {"url": "https://example.com/pricing", "retrieved_at": "2026-10-11T00:00:00Z", "sha256": "abc"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(Config, "DB_PATH", tmp + "/scan.db"), \
                patch("app.intelligence.IntelligenceProvider", FakeProvider), \
                patch("app.intelligence.fetch_evidence", return_value=evidence), \
                patch("app.adapters.ora.OraAdapter.scan", scan_ora), \
                patch("app.adapters.cloudflare.CloudflareAdapter.scan", scan_cloudflare), \
                patch("app.adapters.growth.GrowthAdapter.scan", scan_growth):
            backend = live_backend()
            scan_id = backend["create"]("example.com", "https://example.com/pricing")
            job = None
            for _ in range(100):
                job = backend["get"](scan_id)
                if job["intelligence"] and job["intelligence"]["status"] in ("completed", "failed"):
                    break
                time.sleep(.03)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["intelligence"]["status"], "completed")
            self.assertEqual(job["intelligence"]["report"]["product_summary"], "A scheduling service.")
            self.assertNotIn("prompt", job["intelligence"]["report"])
            backend["shutdown"]()

    def test_shutdown_joins_idle_workers_before_storage_directory_closes(self):
        from app.config import Config
        from server import live_backend

        with tempfile.TemporaryDirectory() as tmp, patch.object(Config, "DB_PATH", tmp + "/scan.db"):
            backend = live_backend()
            scan_thread = next(t for t in threading.enumerate() if t.name == "pickable-scan-worker")
            intelligence_thread = next(t for t in threading.enumerate() if t.name == "pickable-intelligence-worker")
            backend["shutdown"]()
            self.assertFalse(scan_thread.is_alive())
            self.assertFalse(intelligence_thread.is_alive())
            with self.assertRaises(Exception):
                backend["get"]("missing")


if __name__ == "__main__":
    unittest.main()
