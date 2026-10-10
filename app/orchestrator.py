"""
Orchestrator for running multiple adapters in parallel with isolated error handling.
"""

from __future__ import annotations

import asyncio
import datetime
import traceback
from typing import List, Optional

from app.adapters.base import Adapter, AdapterResult
from app.storage import Storage


class Orchestrator:
    def __init__(self, adapters: List[Adapter], storage: Storage):
        self.adapters = adapters
        self.storage = storage

    async def _run_single_adapter(
        self, adapter: Adapter, domain: str, job_id: str
    ) -> AdapterResult:
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            result = await adapter.scan(domain, job_id)
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.storage.save_source_run(
                job_id=job_id,
                source_id=adapter.source_id,
                status=result.status,
                started_at=started_at,
                finished_at=finished_at,
                raw_report=result.raw_report,
                error=result.error,
            )
            # Save individual checks
            checks_dicts = [
                {
                    "job_id": c.job_id,
                    "source_id": c.source_id,
                    "native_check_id": c.native_check_id,
                    "canonical_check_id": c.canonical_check_id,
                    "status": c.status,
                    "evidence": c.evidence,
                    "observed_at": c.observed_at,
                }
                for c in result.checks
            ]
            self.storage.save_checks(checks_dicts)
            return result
        except TimeoutError as e:
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.storage.save_source_run(
                job_id=job_id,
                source_id=adapter.source_id,
                status="timeout",
                started_at=started_at,
                finished_at=finished_at,
                raw_report=None,
                error=str(e) or "Timeout",
            )
            return AdapterResult(
                source_id=adapter.source_id,
                status="timeout",
                raw_report=None,
                checks=[],
                error=str(e),
            )
        except Exception as e:
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.storage.save_source_run(
                job_id=job_id,
                source_id=adapter.source_id,
                status="failed",
                started_at=started_at,
                finished_at=finished_at,
                raw_report=None,
                error=error_msg,
            )
            return AdapterResult(
                source_id=adapter.source_id,
                status="failed",
                raw_report=None,
                checks=[],
                error=error_msg,
            )

    async def run_scan(self, domain: str, job_id: Optional[str] = None) -> str:
        """
        Creates job if not passed, runs all adapters concurrently, and updates status.
        """
        if job_id is None:
            job_id = self.storage.create_job(domain)

        self.storage.update_job_status(job_id, "running")

        # Run all adapters concurrently with asyncio.gather(return_exceptions=True)
        tasks = [
            self._run_single_adapter(adapter, domain, job_id)
            for adapter in self.adapters
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Update overall job status to 'completed'
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.storage.update_job_status(
            job_id, status="completed", finished_at=finished_at
        )

        return job_id
