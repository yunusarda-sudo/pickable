"""Spend-guard tests: reuse, in-flight joins, and the deep daily budget.

No network and no model calls leave these tests: the provider is faked and
scan adapters complete instantly. The goal is to prove that concurrent or
repeat visitors for one domain pay for a single analysis, and that deep
research stops cleanly when the daily budget is gone.
"""
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.agent_policy import AgentPolicy
from app.config import Config


BASE_POLICY = dict(public_url="http://127.0.0.1:8902", agent_api_key="",
                   mcp_scan_enabled=False, scans_per_hour=60,
                   global_scans_per_hour=600)


class FakeProvider:
    model = "test-model"
    api_key = "configured"
    provider = "test"
    calls = 0
    calls_lock = threading.Lock()
    release = threading.Event()
    block = False

    def analyze(self, evidence, depth, scan_context):
        with FakeProvider.calls_lock:
            FakeProvider.calls += 1
        if FakeProvider.block:
            assert FakeProvider.release.wait(30), "test provider gate timed out"
        return {"product_summary": "Model summary.", "target_customers": "Teams",
                "problem": "Coordination", "value_proposition": "Simpler",
                "competitive_landscape": "Not assessed.", "workflows": [],
                "evidence_refs": ["homepage"], "unknowns": [],
                "sources": [], "web_sources": []}


def _completed_scan(self, domain, job_id):
    from app.adapters.base import AdapterResult
    return AdapterResult(source_id=self.source_id, status="completed",
                         raw_report="{}", checks=[], error=None)


def _make_backend(policy, tmp):
    from server import live_backend
    return live_backend(policy=policy)


def _wait_scan(backend, scan_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = backend["get"](scan_id)
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise TimeoutError("scan did not finish")


def _wait_intel(backend, scan_id, depth="short", timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = backend["get"](scan_id)
        intel = (job.get("intelligence") or {}) if depth == "short" else None
        if depth != "short":
            raise ValueError("use get_intelligence for deep")
        if intel and intel.get("status") in ("completed", "partial", "failed"):
            return intel
        time.sleep(0.05)
    raise TimeoutError("intelligence did not finish")


def _fake_evidence(domain, extra_pages=1, on_homepage=None):
    page = {"ref": "homepage", "url": "https://" + str(domain).split("/")[-1] + "/",
            "title": "Test Product", "description": "Test description",
            "visible_text": "Test product for teams.", "links": [],
            "retrieved_at": "2026-10-11T00:00:00+00:00", "sha256": "abc",
            "http_status": 200}
    if on_homepage:
        on_homepage(page)
    return {"pages": [page]}


class IntelEconomyTests(unittest.TestCase):
    def _patched(self, tmp):
        return (patch.object(Config, "DB_PATH", tmp + "/scan.db"),
                patch("app.intelligence.IntelligenceProvider", FakeProvider),
                patch("app.intelligence.fetch_evidence", side_effect=_fake_evidence),
                patch("app.adapters.ora.OraAdapter.scan", _completed_scan),
                patch("app.adapters.cloudflare.CloudflareAdapter.scan", _completed_scan),
                patch("app.adapters.growth.GrowthAdapter.scan", _completed_scan))

    def _policy(self, **over):
        args = dict(BASE_POLICY)
        args.update(over)
        args.setdefault("intel_reuse_hours", 24)
        args.setdefault("deep_per_day", 50)
        return AgentPolicy(**args)

    def test_repeat_domain_reuses_completed_short_without_new_model_call(self):
        FakeProvider.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(), tmp)
                first = backend["create"]("reuse.test")
                _wait_scan(backend, first)
                intel1 = _wait_intel(backend, first)
                self.assertEqual(intel1["status"], "completed")
                self.assertEqual(FakeProvider.calls, 1)
                second = backend["create"]("reuse.test")
                _wait_scan(backend, second)
                intel2 = _wait_intel(backend, second)
                self.assertEqual(intel2["status"], "completed")
                self.assertEqual(FakeProvider.calls, 1,
                                 "second visitor must reuse the first analysis")
                self.assertEqual(intel2["report"]["product_summary"],
                                 intel1["report"]["product_summary"])
                backend["shutdown"]()
            finally:
                for p in patches: p.stop()

    def test_reuse_disabled_when_ttl_is_zero(self):
        FakeProvider.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(intel_reuse_hours=0), tmp)
                for _ in range(2):
                    scan_id = backend["create"]("nocache.test")
                    _wait_scan(backend, scan_id)
                    _wait_intel(backend, scan_id)
                self.assertEqual(FakeProvider.calls, 2)
                backend["shutdown"]()
            finally:
                for p in patches: p.stop()

    def test_concurrent_same_domain_pays_once(self):
        FakeProvider.calls = 0
        FakeProvider.block = True
        FakeProvider.release.clear()
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(), tmp)
                first = backend["create"]("rush.test")
                _wait_scan(backend, first)
                # Leader short starts inside the blocking provider; wait for it.
                deadline = time.monotonic() + 20
                while FakeProvider.calls < 1 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(FakeProvider.calls, 1)
                second = backend["create"]("rush.test")
                _wait_scan(backend, second)
                # Follower must not pay while the leader is in flight: no row
                # yet (readers retry on the next poll) and no second call.
                time.sleep(2)
                self.assertEqual(FakeProvider.calls, 1)
                job = backend["get"](second)
                self.assertIsNone(job["intelligence"],
                                  "follower waits for the leader via polling, not a duplicate call")
                FakeProvider.release.set()
                intel1 = _wait_intel(backend, first)
                intel2 = _wait_intel(backend, second)
                self.assertEqual(intel1["status"], "completed")
                self.assertEqual(intel2["status"], "completed")
                self.assertEqual(intel2["report"]["product_summary"],
                                 intel1["report"]["product_summary"])
                self.assertEqual(FakeProvider.calls, 1,
                                 "stampede must share the in-flight run")
                backend["shutdown"]()
            finally:
                FakeProvider.block = False
                FakeProvider.release.set()
                for p in patches: p.stop()

    def test_deep_budget_stops_second_call_with_409_meaning(self):
        FakeProvider.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(deep_per_day=1), tmp)
                first = backend["create"]("budget-a.test")
                _wait_scan(backend, first)
                _wait_intel(backend, first)
                before = FakeProvider.calls
                backend["deep"](first)
                deadline = time.monotonic() + 20
                while FakeProvider.calls < before + 1 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(FakeProvider.calls, before + 1)
                # Different domain: no reuse applies, so the global daily
                # budget must stop the second paid call.
                second = backend["create"]("budget-b.test")
                _wait_scan(backend, second)
                _wait_intel(backend, second)
                pre_deep = FakeProvider.calls
                with self.assertRaises(ValueError) as ctx:
                    backend["deep"](second)
                self.assertIn("budget", str(ctx.exception).lower())
                self.assertEqual(FakeProvider.calls, pre_deep,
                                 "exhausted budget must not call the model")
                backend["shutdown"]()
            finally:
                for p in patches: p.stop()

    def test_twenty_concurrent_same_domain_pays_once(self):
        FakeProvider.calls = 0
        FakeProvider.block = True
        FakeProvider.release.clear()
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(), tmp)
                first = backend["create"]("stampede20.test")
                _wait_scan(backend, first)
                deadline = time.monotonic() + 20
                while FakeProvider.calls < 1 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(FakeProvider.calls, 1)
                ids, errors = [], []
                ids_lock = threading.Lock()

                def follower():
                    try:
                        scan_id = backend["create"]("stampede20.test")
                        with ids_lock:
                            ids.append(scan_id)
                    except Exception as error:  # noqa: BLE001
                        with ids_lock:
                            errors.append(error)

                threads = [threading.Thread(target=follower) for _ in range(19)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=30)
                self.assertEqual(errors, [])
                self.assertEqual(len(ids), 19)
                for scan_id in ids:
                    _wait_scan(backend, scan_id)
                time.sleep(2)
                self.assertEqual(FakeProvider.calls, 1,
                                 "no follower may start a second paid call")
                FakeProvider.release.set()
                for scan_id in [first, *ids]:
                    intel = _wait_intel(backend, scan_id)
                    self.assertEqual(intel["status"], "completed")
                self.assertEqual(FakeProvider.calls, 1)
                backend["shutdown"]()
            finally:
                FakeProvider.block = False
                FakeProvider.release.set()
                for p in patches: p.stop()

    def test_deep_budget_race_claims_at_most_limit_across_handles(self):
        import datetime
        from app.storage import Storage
        with tempfile.TemporaryDirectory() as tmp:
            primary = Storage(tmp + "/scan.db")
            # Second handle on the same file: simulates a second worker
            # process sharing the database.
            secondary = Storage(tmp + "/scan.db", recover_on_startup=False)
            try:
                scans = [primary.create_job(f"race{i}.test", f"https://race{i}.test/", None)
                         for i in range(70)]
                today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
                won, won_lock = [], threading.Lock()

                def claim(index):
                    handle = primary if index % 2 == 0 else secondary
                    try:
                        job_id = handle.try_claim_deep_budget(scans[index], "m", today, 50)
                    except Exception:  # noqa: BLE001
                        job_id = None
                    with won_lock:
                        won.append(job_id)

                threads = [threading.Thread(target=claim, args=(i,)) for i in range(70)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=30)
                granted = [job_id for job_id in won if job_id]
                self.assertEqual(len(granted), 50)
                self.assertEqual(len(set(granted)), 50)
                self.assertEqual(primary.count_intelligence_since("deep", "0000-01-01"), 50)
            finally:
                primary.close()
                secondary.close()

    def test_inflight_analysis_visible_across_handles(self):
        from app.storage import Storage
        with tempfile.TemporaryDirectory() as tmp:
            primary = Storage(tmp + "/scan.db")
            secondary = Storage(tmp + "/scan.db", recover_on_startup=False)
            try:
                self.assertFalse(secondary.has_inflight_intelligence("shared.test", "short"))
                scan_id = primary.create_job("shared.test", "https://shared.test/", None)
                job_id, _ = primary.create_intelligence_job(scan_id, "short", "m")
                primary.update_intelligence_job(job_id, "running")
                self.assertTrue(secondary.has_inflight_intelligence("shared.test", "short"))
                primary.update_intelligence_job(job_id, "completed", report={"analysis_stage": "complete"})
                self.assertFalse(secondary.has_inflight_intelligence("shared.test", "short"))
            finally:
                primary.close()
                secondary.close()

    def test_shutdown_stops_workers_before_storage_close(self):
        FakeProvider.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patched(tmp)
            for p in patches: p.start()
            try:
                backend = _make_backend(self._policy(), tmp)
                scan_id = backend["create"]("goodbye.test")
                _wait_scan(backend, scan_id)
                _wait_intel(backend, scan_id)
                backend["shutdown"]()
                backend["shutdown"]()  # idempotent
                time.sleep(0.3)
                alive = [t.name for t in threading.enumerate()
                         if t.name in ("pickable-scan-worker", "pickable-intelligence-worker")
                         and t.is_alive()]
                self.assertEqual(alive, [])
            finally:
                for p in patches: p.stop()


class IntelReuseStorageTests(unittest.TestCase):
    def test_stale_completed_analysis_is_not_reused(self):
        from app.storage import Storage
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp + "/scan.db")
            scan_id = storage.create_job("stale.test", "https://stale.test/", None)
            storage.update_job_status(scan_id, "completed")
            job_id, created = storage.create_intelligence_job(scan_id, "short", "m")
            self.assertTrue(created)
            storage.update_intelligence_job(job_id, "completed", report={"analysis_stage": "complete"})
            storage.conn.execute(
                "UPDATE intelligence_jobs SET finished_at = '2000-01-01T00:00:00' WHERE id = ?",
                (job_id,))
            storage.conn.commit()
            self.assertIsNone(storage.find_completed_intelligence(
                "stale.test", "short", "2026-01-01T00:00:00"))
            fresh = storage.find_completed_intelligence(
                "stale.test", "short", "1999-01-01T00:00:00")
            self.assertEqual(fresh, {"analysis_stage": "complete"})
            self.assertEqual(storage.count_intelligence_since("short", "1999-01-01T00:00:00"), 1)
            self.assertEqual(storage.count_intelligence_since("deep", "1999-01-01T00:00:00"), 0)


if __name__ == "__main__":
    unittest.main()
