"""
SQLite Storage implementation for jobs, source runs, and checks.
"""

from __future__ import annotations

import datetime
from functools import wraps
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional


def synchronized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return run


class Storage:
    def __init__(self, db_path: str = "./data/scan.db", recover_on_startup: bool = True):
        self._lock = threading.RLock()
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        if recover_on_startup:
            self._mark_interrupted_on_startup()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    raw_report TEXT,
                    error TEXT,
                    UNIQUE(job_id, source_id)
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    source_id TEXT NOT NULL,
                    native_check_id TEXT NOT NULL,
                    canonical_check_id TEXT,
                    status TEXT NOT NULL,
                    evidence TEXT,
                    observed_at TEXT NOT NULL
                );
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checks_job ON checks(job_id);"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checks_canonical ON checks(canonical_check_id);"
            )

    def _mark_interrupted_on_startup(self) -> None:
        """
        Marks any previously pending or running jobs as 'interrupted'.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN ('pending', 'running');"
            )

    @synchronized
    def create_job(self, domain: str) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO jobs (id, domain, status, started_at) VALUES (?, ?, 'pending', ?);",
                (job_id, domain, now),
            )
        return job_id

    @synchronized
    def update_job_status(
        self,
        job_id: str,
        status: str,
        error: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> None:
        if finished_at is None and status in ("completed", "failed", "interrupted"):
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?;",
                (status, error, finished_at, job_id),
            )

    @synchronized
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @synchronized
    def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?;", (limit,))
        return [dict(row) for row in cursor.fetchall()]

    @synchronized
    def save_source_run(
        self,
        job_id: str,
        source_id: str,
        status: str,
        started_at: str,
        finished_at: Optional[str],
        raw_report: Optional[str],
        error: Optional[str],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO source_runs (job_id, source_id, status, started_at, finished_at, raw_report, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, source_id) DO UPDATE SET
                    status=excluded.status,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    raw_report=excluded.raw_report,
                    error=excluded.error;
                """,
                (job_id, source_id, status, started_at, finished_at, raw_report, error),
            )

    @synchronized
    def get_source_runs(self, job_id: str) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM source_runs WHERE job_id = ? ORDER BY id ASC;",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    @synchronized
    def save_checks(self, checks_list: List[Dict[str, Any]]) -> None:
        if not checks_list:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO checks (job_id, source_id, native_check_id, canonical_check_id, status, evidence, observed_at)
                VALUES (:job_id, :source_id, :native_check_id, :canonical_check_id, :status, :evidence, :observed_at);
                """,
                checks_list,
            )

    @synchronized
    def get_checks(self, job_id: str) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM checks WHERE job_id = ? ORDER BY id ASC;",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


    @synchronized
    def close(self) -> None:
        self.conn.close()
