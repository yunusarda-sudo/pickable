"""
Ora API adapter implementation.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any, Dict, List, Optional

from app.adapters.base import Adapter, AdapterResult, CheckRecord
from app.config import Config
from app.safe_fetch import safe_fetch, SafeFetchError


class TokenBucket:
    """Simple in-memory token bucket rate limiter: default 10 requests / minute."""

    def __init__(self, capacity: int = 10, fill_rate_per_sec: float = 10 / 60.0):
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.fill_rate = fill_rate_per_sec
        self.last_updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_updated
            self.last_updated = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.fill_rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
                self.last_updated = time.monotonic()
            else:
                self.tokens -= 1.0


class OraAdapter(Adapter):
    source_id = "ora"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ):
        self.api_key = api_key or Config.ORA_API_KEY
        self.base_url = (base_url or Config.ORA_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.rate_limiter = TokenBucket(capacity=10, fill_rate_per_sec=10 / 60.0)

    async def health(self) -> str:
        # Check health endpoint if available or assume ok
        return "ok"

    def _map_status(self, raw_status: str) -> str:
        status_norm = (raw_status or "").strip().lower()
        mapping = {
            "pass": "PASS",
            "passed": "PASS",
            "fail": "FAIL",
            "failed": "FAIL",
            "warning": "WARNING",
            "warn": "WARNING",
            "na": "NA",
            "n/a": "NA",
            "error": "ERROR",
            "pending": "PENDING",
            "unknown": "UNKNOWN",
        }
        return mapping.get(status_norm, "UNKNOWN")

    def _extract_checks(
        self, data: Dict[str, Any], job_id: str, observed_at: str
    ) -> List[CheckRecord]:
        checks: List[CheckRecord] = []

        # Parse layers[].checks[]
        layers = data.get("layers", [])
        if isinstance(layers, list):
            for layer in layers:
                if isinstance(layer, dict):
                    layer_checks = layer.get("checks", [])
                    if isinstance(layer_checks, list):
                        for c in layer_checks:
                            if isinstance(c, dict):
                                cid = c.get("check_id") or c.get("id") or "unknown"
                                raw_status = c.get("status", "unknown")
                                evidence = c.get("evidence") or c.get("details") or {}
                                evidence_json = (
                                    evidence
                                    if isinstance(evidence, str)
                                    else json.dumps(evidence)
                                )
                                checks.append(
                                    CheckRecord(
                                        job_id=job_id,
                                        source_id=self.source_id,
                                        native_check_id=str(cid),
                                        canonical_check_id=None,
                                        status=self._map_status(raw_status),
                                        evidence=evidence_json,
                                        observed_at=observed_at,
                                    )
                                )
        return checks

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        start_time = time.monotonic()
        remaining_timeout = lambda: max(1, int(self.timeout - (time.monotonic() - start_time)))

        await self.rate_limiter.acquire()

        # Step 1: POST https://ora.ai/api/scan
        scan_url = f"{self.base_url}/scan"
        payload = json.dumps({"url": f"https://{domain}"}).encode("utf-8")

        loop = asyncio.get_running_loop()
        try:
            fetch_res = await loop.run_in_executor(
                None,
                lambda: safe_fetch(
                    scan_url,
                    method="POST",
                    headers=headers,
                    body=payload,
                    timeout=remaining_timeout(),
                ),
            )
        except SafeFetchError as e:
            return AdapterResult(
                source_id=self.source_id,
                status="failed",
                raw_report=None,
                checks=[],
                error=f"Ora scan request failed: {e}",
            )
        except Exception as e:
            return AdapterResult(
                source_id=self.source_id,
                status="failed",
                raw_report=None,
                checks=[],
                error=f"Ora scan unexpected error: {e}",
            )

        # Step 2: Handle 200 or 202
        raw_report_str: Optional[str] = None
        data: Optional[Dict[str, Any]] = None

        if fetch_res.status == 200:
            raw_report_str = fetch_res.body.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw_report_str)
            except json.JSONDecodeError:
                pass
        elif fetch_res.status == 202:
            # Poll if poll_url provided, otherwise poll /score/{domain}
            body_json = {}
            try:
                body_json = json.loads(fetch_res.body.decode("utf-8", errors="replace"))
            except Exception:
                pass

            poll_url = body_json.get("poll_url") or f"{self.base_url}/score/{domain}"

            # Polling loop
            while remaining_timeout() > 0:
                await asyncio.sleep(2)
                await self.rate_limiter.acquire()
                poll_res = await loop.run_in_executor(
                    None,
                    lambda: safe_fetch(
                        poll_url,
                        method="GET",
                        headers=headers,
                        timeout=remaining_timeout(),
                    ),
                )
                if poll_res.status == 200:
                    raw_report_str = poll_res.body.decode("utf-8", errors="replace")
                    try:
                        data = json.loads(raw_report_str)
                        if data.get("status") not in ("pending", "running"):
                            break
                    except json.JSONDecodeError:
                        break
            if not data or data.get("status") in ("pending", "running"):
                raise TimeoutError("Ora scan polling timed out")
        else:
            return AdapterResult(
                source_id=self.source_id,
                status="failed",
                raw_report=fetch_res.body.decode("utf-8", errors="replace"),
                checks=[],
                error=f"Ora API returned HTTP {fetch_res.status}",
            )

        if not data:
            return AdapterResult(
                source_id=self.source_id,
                status="failed",
                raw_report=raw_report_str,
                checks=[],
                error="Invalid JSON response from Ora",
            )

        checks = self._extract_checks(data, job_id, observed_at)

        return AdapterResult(
            source_id=self.source_id,
            status="completed",
            raw_report=raw_report_str,
            checks=checks,
            error=None,
        )
