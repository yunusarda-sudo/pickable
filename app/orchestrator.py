"""
Orchestrator for running multiple adapters in parallel with isolated error handling.
"""

from __future__ import annotations

import asyncio
import datetime
import http.client
import traceback
from typing import List, Optional

from app.adapters.base import Adapter, AdapterResult
from app.safe_fetch import TimeoutError as SafeFetchTimeout
from app.storage import Storage

# Errors worth one more attempt: timeouts and connection-level blips.
# Permanent problems (SSRF blocks, HTTP 4xx, invalid payloads, bad config)
# fail fast without retrying. Note: app.safe_fetch.TimeoutError is not a
# subclass of the builtin TimeoutError, so it is listed explicitly.
RETRYABLE_ERRORS = (TimeoutError, SafeFetchTimeout, ConnectionError,
                    http.client.HTTPException)

_TRANSIENT_PATTERNS = (
    "timed out", "timeout", "deadline exceeded", "connection",
    "reset by peer", "temporary failure", "try again",
    "http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
    "rate limit", "service unavailable", "bad gateway", "gateway timeout",
    "too many requests",
)


def _is_transient_result(result: AdapterResult) -> bool:
    """A returned non-completed result is retried only if it looks transient."""
    if result.status == "timeout":
        return True
    if result.status == "failed" and isinstance(result.error, str):
        lowered = result.error.lower()
        return any(pattern in lowered for pattern in _TRANSIENT_PATTERNS)
    return False


class Orchestrator:
    def __init__(self, adapters: List[Adapter], storage: Storage,
                 max_attempts: int = 2, retry_delay: float = 2.0):
        self.adapters = adapters
        self.storage = storage
        self.max_attempts = max(1, max_attempts)
        self.retry_delay = max(0.0, retry_delay)

    async def _run_single_adapter(
        self, adapter: Adapter, domain: str, job_id: str
    ) -> AdapterResult:
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result: Optional[AdapterResult] = None
        error: Optional[str] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = await adapter.scan(domain, job_id)
                error = None
                transient = _is_transient_result(result)
            except RETRYABLE_ERRORS as e:
                result, error, transient = None, e, True
            except Exception as e:
                result, error, transient = None, e, False
            if not transient or attempt >= self.max_attempts:
                break
            await asyncio.sleep(self.retry_delay)
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if result is not None:
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
        # No usable result after all attempts: classify the last error the
        # same way the pre-retry code did (timeouts -> "timeout"), but keep
        # the stored message human-readable instead of leaking doubled
        # exception text like "TimeoutError: Request timed out: timed out".
        is_timeout = isinstance(error, (TimeoutError, SafeFetchTimeout))
        status = "timeout" if is_timeout else "failed"
        if is_timeout:
            detail = str(error)
            error_msg = ("Request timed out: the site took too long to respond."
                         if isinstance(error, SafeFetchTimeout) or "timed out" in detail.lower()
                         else (detail or "Timeout"))
        else:
            error_msg = f"{type(error).__name__}: {error}"
        self.storage.save_source_run(
            job_id=job_id,
            source_id=adapter.source_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            raw_report=None,
            error=error_msg,
        )
        return AdapterResult(
            source_id=adapter.source_id,
            status=status,
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
