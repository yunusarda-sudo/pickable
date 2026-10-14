"""Live pilot eval: run the real adapter (real network, no mocks) against the
20 human-verified cases in tests/fixtures/agentready_live_cases.json and report
agreement. Manual use only -- never wired into CI (network-dependent).

Usage: python3 scripts/eval_agentready_live.py
Exit 0 when every live case agrees with its human label.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.agentready import AgentReadyAdapter

CASES_PATH = ROOT / "tests" / "fixtures" / "agentready_live_cases.json"


async def scan_domain(domain: str) -> dict:
    adapter = AgentReadyAdapter(timeout=15)
    result = await adapter.scan(domain, "live-" + domain)
    out = {}
    for record in result.checks:
        try:
            detail = json.loads(record.evidence)
        except ValueError:
            detail = {}
        out[record.native_check_id] = {
            "status": record.status,
            "applicability": detail.get("applicability"),
        }
    return out


async def main() -> int:
    cases = json.loads(CASES_PATH.read_text())["cases"]
    by_domain: dict[str, list] = {}
    for case in cases:
        by_domain.setdefault(case["domain"], []).append(case)
    mismatches = 0
    checked = 0
    for domain, group in by_domain.items():
        try:
            got = await scan_domain(domain)
        except Exception as e:  # noqa: BLE001 -- one domain never kills the sweep
            print(f"ERROR {domain}: {type(e).__name__}: {e}")
            mismatches += len(group)
            continue
        for case in group:
            checked += 1
            entry = got.get(case["check"], {})
            ok = (entry.get("status") == case["expected_status"]
                  and entry.get("applicability") == case["expected_applicability"])
            mark = "OK " if ok else "MISMATCH "
            print(f"{mark}{case['id']} {case['domain']} {case['check']}: "
                  f"expected {case['expected_applicability']}/{case['expected_status']}, "
                  f"got {entry.get('applicability')}/{entry.get('status')}")
            if not ok:
                mismatches += 1
    print(f"live agreement: {checked - mismatches}/{checked}")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
