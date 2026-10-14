"""Cloudflare isitagentready.com adapter (public, unauthenticated, no published rate limits)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.adapters.base import Adapter, AdapterResult, CheckRecord
from app.safe_fetch import safe_fetch

SCAN_URL = "https://isitagentready.com/api/scan"


class CloudflareAdapter(Adapter):
    source_id = "cloudflare"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    async def health(self) -> str:
        return "ok"

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        # Errors propagate; the orchestrator records them as a failed source run.
        res = await asyncio.get_running_loop().run_in_executor(None, lambda: safe_fetch(
            SCAN_URL, method="POST", headers={"Content-Type": "application/json"},
            body=json.dumps({"url": f"https://{domain}"}).encode(), timeout=self.timeout))
        raw = res.body.decode("utf-8", errors="replace")
        if res.status != 200:
            return AdapterResult(self.source_id, "failed", raw, [], f"Cloudflare API returned HTTP {res.status}")
        try:
            data = json.loads(raw)
        except ValueError:
            return AdapterResult(self.source_id, "failed", raw, [], "Cloudflare returned invalid JSON")
        if not isinstance(data, dict):
            return AdapterResult(self.source_id, "failed", raw, [], "Cloudflare returned a non-object JSON response")
        groups = data.get("checks")
        rows = [(cid, check) for group in groups.values() if isinstance(group, dict)
                for cid, check in group.items() if isinstance(check, dict)
                and isinstance(check.get("status"), str)] if isinstance(groups, dict) else []
        if data.get("error") or not rows:
            return AdapterResult(self.source_id, "failed", raw, [], "Cloudflare returned no valid scan checks or a provider error")
        observed_at = data.get("scannedAt")
        if not isinstance(observed_at, str):
            observed_at = datetime.now(timezone.utc).isoformat()
        statuses = {"pass": "PASS", "fail": "FAIL", "warning": "WARNING", "warn": "WARNING",
                    "na": "NOT_APPLICABLE", "skip": "NOT_APPLICABLE", "not_applicable": "NOT_APPLICABLE",
                    "error": "ERROR", "timeout": "ERROR", "failed": "ERROR"}
        checks = [CheckRecord(job_id, self.source_id, cid, None,
                              statuses.get(check["status"], "UNKNOWN"), json.dumps(check), observed_at)
                  for cid, check in rows]
        return AdapterResult(self.source_id, "completed", raw, checks)
