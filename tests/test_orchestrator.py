import unittest
import asyncio
import time
from unittest.mock import MagicMock
from app.adapters.base import Adapter, CheckRecord, AdapterResult
from app.orchestrator import Orchestrator
from app.storage import Storage
import tempfile
import os


class SlowAdapter(Adapter):
    source_id = "slow_adapter"

    def __init__(self, delay=0.2, should_fail=False, timeout=False):
        self.delay = delay
        self.should_fail = should_fail
        self.timeout = timeout

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        await asyncio.sleep(self.delay)
        if self.timeout:
            raise TimeoutError("Adapter timed out")
        if self.should_fail:
            raise RuntimeError("Adapter crashed")
        return AdapterResult(
            source_id=self.source_id,
            status="completed",
            raw_report='{"ok": true}',
            checks=[
                CheckRecord(
                    job_id=job_id,
                    source_id=self.source_id,
                    native_check_id="check_1",
                    canonical_check_id=None,
                    status="PASS",
                    evidence="{}",
                    observed_at="2026-10-10T12:00:00Z",
                )
            ],
        )

    async def health(self) -> str:
        return "ok"


class TestOrchestrator(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_orch.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_parallel_execution_faster_than_serial(self):
        """Adapter'lar paralel calismali ve toplam sure seri toplamdan kisa olmali"""
        ad1 = SlowAdapter(delay=0.2)
        ad1.source_id = "ora"
        ad2 = SlowAdapter(delay=0.2)
        ad2.source_id = "agentready_oss"

        orch = Orchestrator(adapters=[ad1, ad2], storage=self.storage)
        t0 = time.perf_counter()
        job_id = await orch.run_scan("test.com")
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.35, "Parallel execution should take ~0.2s, not 0.4s+")
        job = self.storage.get_job(job_id)
        self.assertEqual(job["status"], "completed")

        runs = self.storage.get_source_runs(job_id)
        self.assertEqual(len(runs), 2)
        for r in runs:
            self.assertEqual(r["status"], "completed")

    async def test_one_source_failure_isolated(self):
        """Biri patlarsa digeri etkilenmez, basarili olan kaydedilir"""
        ad1 = SlowAdapter(delay=0.05, should_fail=False)
        ad1.source_id = "ora"
        ad2 = SlowAdapter(delay=0.05, should_fail=True)
        ad2.source_id = "agentready_oss"

        orch = Orchestrator(adapters=[ad1, ad2], storage=self.storage)
        job_id = await orch.run_scan("test.com")

        job = self.storage.get_job(job_id)
        # Even with one failure, the overall job completes with partial data
        self.assertEqual(job["status"], "completed")

        runs = {r["source_id"]: r for r in self.storage.get_source_runs(job_id)}
        self.assertEqual(runs["ora"]["status"], "completed")
        self.assertEqual(runs["agentready_oss"]["status"], "failed")
        self.assertIn("Adapter crashed", runs["agentready_oss"]["error"])

        checks = self.storage.get_checks(job_id)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["source_id"], "ora")

    async def test_timeout_one_source_does_not_kill_other(self):
        """Bir kaynagin timeout olmasi diger kaynagin sonucunu engellemez"""
        ad1 = SlowAdapter(delay=0.05, should_fail=False)
        ad1.source_id = "ora"
        ad2 = SlowAdapter(delay=0.05, timeout=True)
        ad2.source_id = "agentready_oss"

        orch = Orchestrator(adapters=[ad1, ad2], storage=self.storage)
        job_id = await orch.run_scan("test.com")

        runs = {r["source_id"]: r for r in self.storage.get_source_runs(job_id)}
        self.assertEqual(runs["ora"]["status"], "completed")
        self.assertEqual(runs["agentready_oss"]["status"], "timeout")

    async def test_both_sources_fail_returns_partial(self):
        """Her iki adapter da hata verse bile job sonlanir ve hata durumlari kaydedilir"""
        ad1 = SlowAdapter(delay=0.05, should_fail=True)
        ad1.source_id = "ora"
        ad2 = SlowAdapter(delay=0.05, timeout=True)
        ad2.source_id = "agentready_oss"

        orch = Orchestrator(adapters=[ad1, ad2], storage=self.storage)
        job_id = await orch.run_scan("test.com")

        job = self.storage.get_job(job_id)
        self.assertEqual(job["status"], "completed")

        runs = {r["source_id"]: r for r in self.storage.get_source_runs(job_id)}
        self.assertEqual(runs["ora"]["status"], "failed")
        self.assertEqual(runs["agentready_oss"]["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
