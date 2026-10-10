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
            source_id="agentready_oss",
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
        self.assertEqual(runs_by_source["agentready_oss"]["status"], "failed")
        self.assertEqual(runs_by_source["agentready_oss"]["error"], "Subprocess exit code 1")

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


if __name__ == "__main__":
    unittest.main()
