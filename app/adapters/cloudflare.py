"""Cloudflare isitagentready.com adapter (public, unauthenticated, no published rate limits)."""

from __future__ import annotations

import asyncio
import json

from app.adapters.base import Adapter, AdapterResult
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
        if not isinstance(json.loads(raw), dict):
            return AdapterResult(self.source_id, "failed", raw, [], "Cloudflare returned a non-object JSON response")
        return AdapterResult(self.source_id, "completed", raw, [])
