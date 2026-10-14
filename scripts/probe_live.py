"""Live research probe: run the real AgentReadyAdapter (no mocks) against real
domains and dump per-check status/applicability/observation as JSON.

Read-only GETs via app.safe_fetch (SSRF-protected). No registration, login,
payment or state change. Output is observation data for building the live
labeled set; labels themselves are assigned by a human afterwards.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.agentready import AgentReadyAdapter

DOMAINS = [
    "accounts.google.com",  # real OIDC discovery expected
    "github.com",           # big SaaS, mixed surface
    "example.com",          # static, no auth expected
    "cloudflare.com",       # Content Signals originator
    "workos.com",           # auth.md originator
    "solveny.dev",          # Pickable public demo
]


async def probe(domain: str) -> dict:
    adapter = AgentReadyAdapter(timeout=15)
    result = await adapter.scan(domain, "live-" + domain)
    checks = []
    for record in result.checks:
        try:
            detail = json.loads(record.evidence)
        except ValueError:
            detail = {}
        ev = (detail.get("evidence") or [{}])[0]
        checks.append({
            "check": record.native_check_id,
            "status": record.status,
            "applicability": detail.get("applicability"),
            "claim": detail.get("claim"),
            "locator": ev.get("locator"),
            "http_status": ev.get("http_status"),
            "observed": (ev.get("observed") or "")[:300],
        })
    return {"domain": domain, "adapter_status": result.status, "checks": checks}


async def main(domains) -> None:
    out = []
    for domain in domains:
        try:
            out.append(await probe(domain))
        except Exception as e:  # one domain never kills the sweep
            out.append({"domain": domain, "error": f"{type(e).__name__}: {e}"})
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    domains = sys.argv[1:] or DOMAINS
    asyncio.run(main(domains))
