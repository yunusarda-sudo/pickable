import unittest
import os
import sqlite3
import tempfile
from app.storage import Storage


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_jobs_table_created(self):
        """Veritabani baslatildiginda jobs, source_runs ve checks tablolari olusturulmali"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("jobs", tables)
        self.assertIn("source_runs", tables)
        self.assertIn("checks", tables)

    def test_reports_persist_across_restart(self):
        """Kaydedilen raporlar ve job bilgileri baglanti kapanip acilinca korunmali"""
        job_id = self.storage.create_job("example.com")
        self.storage.update_job_status(job_id, "completed")
        self.storage.save_source_run(
            job_id=job_id,
            source_id="ora",
            status="completed",
            started_at="2026-10-10T12:00:00Z",
            finished_at="2026-10-10T12:00:05Z",
            raw_report='{"score": 90}',
            error=None,
        )
        self.storage.close()

        # Reopen with new instance
        storage2 = Storage(self.db_path)
        job = storage2.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["domain"], "example.com")

        runs = storage2.get_source_runs(job_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["source_id"], "ora")
        self.assertEqual(runs[0]["raw_report"], '{"score": 90}')
        storage2.close()

    def test_interrupted_jobs_marked_on_startup(self):
        """Baslangicta pending veya running olan job'lar 'interrupted' olarak isaretlenmeli"""
        job1 = self.storage.create_job("pending.com")
        job2 = self.storage.create_job("running.com")
        self.storage.update_job_status(job2, "running")
        job3 = self.storage.create_job("completed.com")
        self.storage.update_job_status(job3, "completed")
        self.storage.close()

        # Initialize new storage, which triggers startup interruption cleanup
        storage2 = Storage(self.db_path)
        self.assertEqual(storage2.get_job(job1)["status"], "interrupted")
        self.assertEqual(storage2.get_job(job2)["status"], "interrupted")
        self.assertEqual(storage2.get_job(job3)["status"], "completed")
        storage2.close()

    def test_interrupted_intelligence_job_can_be_requeued(self):
        scan_id = self.storage.create_job("example.com")
        job_id, created = self.storage.create_intelligence_job(scan_id, "short", "test-model")
        self.assertTrue(created)
        self.storage.update_intelligence_job(job_id, "running")
        self.storage.close()

        storage2 = Storage(self.db_path)
        self.assertEqual(storage2.get_intelligence_job(scan_id, "short")["status"], "interrupted")
        resumed_id, queued = storage2.create_intelligence_job(scan_id, "short", "test-model")
        self.assertEqual(resumed_id, job_id)
        self.assertTrue(queued)
        self.assertEqual(storage2.get_intelligence_job(scan_id, "short")["status"], "queued")
        same_id, queued_again = storage2.create_intelligence_job(scan_id, "short", "test-model")
        self.assertEqual(same_id, job_id)
        self.assertFalse(queued_again)
        storage2.close()

    def test_source_failure_does_not_lose_other_source(self):
        """Bir kaynagin basarisiz olmasi diger kaynagin basarili sonucunu kaybettirmemeli"""
        job_id = self.storage.create_job("partial.com")
        self.storage.save_source_run(
            job_id=job_id,
            source_id="ora",
            status="completed",
            started_at="2026-10-10T12:00:00Z",
            finished_at="2026-10-10T12:00:05Z",
            raw_report='{"score": 85}',
            error=None,
        )
        self.storage.save_source_run(
            job_id=job_id,
            source_id="mock_b",
            status="failed",
            started_at="2026-10-10T12:00:00Z",
            finished_at="2026-10-10T12:00:02Z",
            raw_report=None,
            error="Subprocess exit code 1",
        )

        runs = self.storage.get_source_runs(job_id)
        runs_by_source = {r["source_id"]: r for r in runs}
        self.assertEqual(runs_by_source["ora"]["status"], "completed")
        self.assertEqual(runs_by_source["ora"]["raw_report"], '{"score": 85}')
        self.assertEqual(runs_by_source["mock_b"]["status"], "failed")
        self.assertEqual(runs_by_source["mock_b"]["error"], "Subprocess exit code 1")

    def test_unknown_status_preserved(self):
        """UNKNOWN/NA gibi statuler hicbir yerde degistirilmeden checks tablosunda korunmali"""
        job_id = self.storage.create_job("example.com")
        self.storage.save_checks([
            {
                "job_id": job_id,
                "source_id": "ora",
                "native_check_id": "check_unknown",
                "canonical_check_id": None,
                "status": "UNKNOWN",
                "evidence": '{"info": "unclear"}',
                "observed_at": "2026-10-10T12:00:00Z",
            },
            {
                "job_id": job_id,
                "source_id": "ora",
                "native_check_id": "check_na",
                "canonical_check_id": None,
                "status": "NA",
                "evidence": '{"info": "not applicable"}',
                "observed_at": "2026-10-10T12:00:00Z",
            },
        ])

        checks = self.storage.get_checks(job_id)
        statuses = {c["native_check_id"]: c["status"] for c in checks}
        self.assertEqual(statuses["check_unknown"], "UNKNOWN")
        self.assertEqual(statuses["check_na"], "NA")

    def test_concurrency_pragmas_configured(self):
        """Paylasilan baglanti WAL + busy_timeout ile acilmali (kilitlenme yerine bekler)"""
        mode = self.storage.conn.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        timeout = self.storage.conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        self.assertGreaterEqual(timeout, 5000)

    def test_close_is_idempotent_and_guards_late_writes(self):
        """close() iki kez cagrilabilir; kapanis sonrasi yazma anlasilir hatayla reddedilir"""
        self.storage.close()
        self.storage.close()
        with self.assertRaises(RuntimeError):
            self.storage.has_inflight_intelligence("example.com", "short")

    def test_parallel_writes_preserve_all_jobs(self):
        """8 yazar thread 80 job yazdiginda kayip/yinelenme olmamali (readonly/disk I/O regresyonu)"""
        from concurrent.futures import ThreadPoolExecutor

        def write(i):
            job_id = self.storage.create_job(f"load{i}.test")
            self.storage.update_job_status(job_id, "completed")
            return job_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(write, range(80)))
        self.assertEqual(len(set(ids)), 80)
        self.assertTrue(all(self.storage.get_job(job_id)["status"] == "completed" for job_id in ids))

    def test_waitlist_and_events_tables_created(self):
        """waitlist ve analytics_events tablolari ve indeksleri kurulmali"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index';")
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("waitlist", tables)
        self.assertIn("analytics_events", tables)
        self.assertIn("idx_waitlist_source", indexes)
        self.assertIn("idx_analytics_name", indexes)

    def test_waitlist_signup_dedupes_and_persists_normalized_email(self):
        """Ayni e-posta tekrar kaydedilmemeli; kayit normalize edilmis halde tutulmali.

        normalize_email() zaten kucuk harfe cevirir; storage katmani bu normalize
        edilmis degeri oldugu gibi saklar ve UNIQUE index ile tekillestirir.
        """
        self.assertTrue(self.storage.add_waitlist_signup("a@example.com", "https://a.example", "landing_page"))
        self.assertFalse(self.storage.add_waitlist_signup("a@example.com", None, "scan_results"))

        rows = self.storage.conn.execute("SELECT email, website, source FROM waitlist").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), ("a@example.com", "https://a.example", "landing_page"))

    def test_waitlist_signup_persists_across_restart(self):
        """Kayitlar baglanti yeniden acildiginda korunmali"""
        self.storage.add_waitlist_signup("keep@example.com", None, "scan_results")
        self.storage.close()

        reopened = Storage(self.db_path)
        try:
            rows = reopened.conn.execute("SELECT email, source FROM waitlist").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("keep@example.com", "scan_results")])
        finally:
            reopened.close()

    def test_record_event_persists_and_allows_duplicates(self):
        """Aynı funnel olayi birden fazla kez kaydedilebilmeli"""
        self.storage.record_event("waitlist_cta_viewed", "landing_page")
        self.storage.record_event("waitlist_cta_viewed", "landing_page")
        self.storage.record_event("waitlist_modal_opened", "scan_results")

        rows = self.storage.conn.execute(
            "SELECT name, source FROM analytics_events ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ("waitlist_cta_viewed", "landing_page"),
            ("waitlist_cta_viewed", "landing_page"),
            ("waitlist_modal_opened", "scan_results"),
        ])

    def test_record_event_persists_across_restart(self):
        """Analytics olaylari baglanti yeniden acildiginda korunmali"""
        self.storage.record_event("waitlist_cta_clicked", "scan_results")
        self.storage.close()

        reopened = Storage(self.db_path)
        try:
            rows = reopened.conn.execute("SELECT name, source FROM analytics_events").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("waitlist_cta_clicked", "scan_results")])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
