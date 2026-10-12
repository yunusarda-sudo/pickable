"""
SQLite Storage implementation for jobs, source runs, and checks.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional


class Storage:
    def __init__(self, db_path: str = "./data/scan.db", recover_on_startup: bool = True):
        self._lock = threading.RLock()
        self.db_path = db_path
        self._closed = False
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # timeout=30: concurrent writer threads block instead of raising
        # "database is locked"; combined with the RLock below this keeps the
        # single shared connection safe. WAL lets readers proceed during a
        # writer transaction (preview/history reads during scan updates).
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA busy_timeout=30000;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
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
                    target_url TEXT,
                    owner_ip TEXT,
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
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS intelligence_jobs (
                    id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES jobs(id),
                    depth TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    model TEXT,
                    report TEXT,
                    error TEXT,
                    UNIQUE(scan_id, depth)
                );
            """)
            # Early-access waitlist. The email is stored normalized and unique
            # at the database level; no listing endpoint exposes these rows.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    website TEXT,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_waitlist_source ON waitlist(source);")
            # First-party analytics funnel rows: allowlisted names and sources
            # only, never free text, so the table cannot hold personal data.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_name ON analytics_events(name, source);")
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
            if "target_url" not in columns:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN target_url TEXT")
            if "owner_ip" not in columns:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN owner_ip TEXT")
            intel_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(intelligence_jobs)")}
            for name in ("input_tokens", "output_tokens", "total_tokens", "finish_reason"):
                if name not in intel_columns:
                    self.conn.execute(f"ALTER TABLE intelligence_jobs ADD COLUMN {name} TEXT" if name == "finish_reason" else f"ALTER TABLE intelligence_jobs ADD COLUMN {name} INTEGER")

    def _mark_interrupted_on_startup(self) -> None:
        """
        Marks any previously pending or running jobs as 'interrupted'.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN ('pending', 'running');"
            )
            self.conn.execute("UPDATE intelligence_jobs SET status='interrupted', finished_at=? WHERE status IN ('queued', 'running');",
                              (datetime.datetime.now(datetime.timezone.utc).isoformat(),))

    def _prune_analytics(self, keep_days: int = 180) -> None:
        """Bounded analytics retention: funnel rows are measurements, not records.

        Runs once at startup; the rate-limited event path keeps growth slow
        and old rows carry no value once the launch window has passed.
        """
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=keep_days)).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM analytics_events WHERE created_at < ?", (cutoff,))

    def add_waitlist_signup(self, email: str, website: Optional[str] = None, source: str = "landing_page") -> bool:
        """Insert one early-access signup; False when the email is already listed.

        Callers pass the already validated, normalized email. Uniqueness is
        enforced by the database, so concurrent duplicate submissions cannot
        both win the race.
        """
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO waitlist (email, website, source, created_at) VALUES (?, ?, ?, ?)",
                (email, website, source, datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def record_event(self, name: str, source: str = "landing_page") -> None:
        """Store one allowlisted first-party analytics event."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO analytics_events (name, source, created_at) VALUES (?, ?, ?)",
                (name, source, datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )

    def create_intelligence_job(self, scan_id: str, depth: str, model: str):
        now, job_id = datetime.datetime.now(datetime.timezone.utc).isoformat(), str(uuid.uuid4())
        with self._lock, self.conn:
            cursor = self.conn.execute("""
                INSERT INTO intelligence_jobs (id, scan_id, depth, status, created_at, model)
                VALUES (?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(scan_id, depth) DO UPDATE SET
                    status='queued', created_at=excluded.created_at, started_at=NULL,
                    finished_at=NULL, model=excluded.model, report=NULL, error=NULL,
                    input_tokens=NULL, output_tokens=NULL, total_tokens=NULL, finish_reason=NULL
                WHERE intelligence_jobs.status IN ('interrupted', 'failed', 'partial', 'unavailable')
            """, (job_id, scan_id, depth, now, model))
            queued = cursor.rowcount == 1
            row = self.conn.execute("SELECT id FROM intelligence_jobs WHERE scan_id=? AND depth=?", (scan_id, depth)).fetchone()
            job_id = row["id"]
        return job_id, queued

    def get_intelligence_job(self, scan_id: str, depth: str = "short") -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM intelligence_jobs WHERE scan_id=? AND depth=?", (scan_id, depth)).fetchone()
        if not row: return None
        result = dict(row)
        if result.get("report"):
            try: result["report"] = json.loads(result["report"])
            except (ValueError, TypeError): result["report"] = None
        return result

    def find_completed_intelligence(self, domain: str, depth: str, since_iso: str) -> Optional[Dict[str, Any]]:
        """Latest completed same-domain analysis newer than since_iso.

        Lets concurrent or repeat visitors reuse one paid model call instead
        of re-analyzing an unchanged site. ISO timestamps compare
        lexicographically because every writer uses UTC isoformat.
        """
        with self._lock:
            row = self.conn.execute(
                """SELECT ij.report AS report FROM intelligence_jobs ij
                   JOIN jobs j ON j.id = ij.scan_id
                   WHERE j.domain = ? AND ij.depth = ? AND ij.status = 'completed'
                     AND ij.finished_at IS NOT NULL AND ij.finished_at >= ?
                   ORDER BY ij.finished_at DESC LIMIT 1""",
                (domain, depth, since_iso)).fetchone()
        if not row or not row["report"]:
            return None
        try:
            return json.loads(row["report"])
        except (ValueError, TypeError):
            return None

    def count_intelligence_since(self, depth: str, since_iso: str) -> int:
        """Admission-time spend guard: how many analyses of this depth started today."""
        with self._lock:
            self._ensure_open()
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM intelligence_jobs WHERE depth = ? AND created_at >= ?",
                (depth, since_iso)).fetchone()
        return int(row["n"]) if row else 0

    def has_inflight_intelligence(self, domain: str, depth: str) -> bool:
        """Cross-process in-flight check: any queued/running analysis for this domain+depth.

        The in-memory `flight` set in server.py is per-process only. This query
        sees rows written by any worker process sharing the same SQLite file, so
        a second replica/process joins the running analysis instead of paying
        for a duplicate model call.
        """
        with self._lock:
            self._ensure_open()
            row = self.conn.execute(
                """SELECT 1 FROM intelligence_jobs ij
                   JOIN jobs j ON j.id = ij.scan_id
                   WHERE j.domain = ? AND ij.depth = ? AND ij.status IN ('queued', 'running')
                   LIMIT 1""",
                (domain, depth)).fetchone()
        return row is not None

    def try_claim_deep_budget(self, scan_id: str, model: str, today_iso: str, limit: int):
        """Atomically claim one deep-analysis slot; returns job_id or None when exhausted.

        The COUNT check and the INSERT run inside a single BEGIN IMMEDIATE
        transaction, so N processes racing to claim the last slot cannot all
        succeed: SQLite serializes the writers and only `limit` claims commit.
        Callers must treat None as "budget exhausted" without calling the model.
        """
        with self._lock:
            self._ensure_open()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM intelligence_jobs WHERE depth = 'deep' AND created_at >= ?",
                    (today_iso,)).fetchone()
                if int(row["n"]) >= limit:
                    self.conn.execute("ROLLBACK")
                    return None
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                job_id = str(uuid.uuid4())
                self.conn.execute(
                    "INSERT INTO intelligence_jobs (id, scan_id, depth, status, created_at, model) VALUES (?, ?, 'deep', 'queued', ?, ?)",
                    (job_id, scan_id, now, model))
                self.conn.execute("COMMIT")
                return job_id
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Storage is closed: workers must stop before the database is cleaned up.")

    def update_intelligence_job(self, job_id: str, status: str, report=None, error=None, usage=None, finish_reason=None) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started = now if status == "running" else None
        finished = now if status in ("completed", "partial", "unavailable", "failed", "interrupted") else None
        input_tokens = output_tokens = total_tokens = None
        if isinstance(usage, dict):
            for key, var in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
                value = usage.get(key)
                if isinstance(value, int) and value >= 0:
                    if key == "input_tokens": input_tokens = value
                    elif key == "output_tokens": output_tokens = value
                    else: total_tokens = value
        with self._lock, self.conn:
            if report is None:
                # Never wipe an already-published preliminary report with NULL:
                # keep the running summary so the UI never falls empty mid-run.
                self.conn.execute("UPDATE intelligence_jobs SET status=?, started_at=COALESCE(?, started_at), finished_at=COALESCE(?, finished_at), error=?, input_tokens=COALESCE(?, input_tokens), output_tokens=COALESCE(?, output_tokens), total_tokens=COALESCE(?, total_tokens), finish_reason=COALESCE(?, finish_reason) WHERE id=?",
                                  (status, started, finished, error, input_tokens, output_tokens, total_tokens, finish_reason, job_id))
            else:
                self.conn.execute("UPDATE intelligence_jobs SET status=?, started_at=COALESCE(?, started_at), finished_at=COALESCE(?, finished_at), report=?, error=?, input_tokens=COALESCE(?, input_tokens), output_tokens=COALESCE(?, output_tokens), total_tokens=COALESCE(?, total_tokens), finish_reason=COALESCE(?, finish_reason) WHERE id=?",
                                  (status, started, finished, json.dumps(report, ensure_ascii=False) if report is not None else None, error, input_tokens, output_tokens, total_tokens, finish_reason, job_id))

    def create_job(self, domain: str, target_url: Optional[str] = None, owner_ip: Optional[str] = None) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO jobs (id, domain, target_url, owner_ip, status, started_at) VALUES (?, ?, ?, ?, 'pending', ?);",
                (job_id, domain, target_url, owner_ip, now),
            )
        return job_id

    def update_job_status(
        self,
        job_id: str,
        status: str,
        error: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> None:
        if finished_at is None and status in ("completed", "failed", "interrupted"):
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?;",
                (status, error, finished_at, job_id),
            )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cursor.fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?;", (limit,))
            return [dict(row) for row in cursor.fetchall()]

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
        with self._lock, self.conn:
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

    def get_source_runs(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM source_runs WHERE job_id = ? ORDER BY id ASC;", (job_id,))
            return [dict(row) for row in cursor.fetchall()]

    def save_checks(self, checks_list: List[Dict[str, Any]]) -> None:
        if not checks_list:
            return
        with self._lock, self.conn:
            self.conn.executemany(
                """
                INSERT INTO checks (job_id, source_id, native_check_id, canonical_check_id, status, evidence, observed_at)
                VALUES (:job_id, :source_id, :native_check_id, :canonical_check_id, :status, :evidence, :observed_at);
                """,
                checks_list,
            )

    def get_checks(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM checks WHERE job_id = ? ORDER BY id ASC;", (job_id,))
            return [dict(row) for row in cursor.fetchall()]


    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.conn.close()
            self._closed = True
