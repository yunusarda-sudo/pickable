"""Sprint B eval: precision, N/A accuracy, evidence coverage, reproducibility.

Runs the manually labeled cases in tests/fixtures/agentready_labeled_cases.json
against AgentReadyAdapter with a mocked transport (no outbound I/O).
Each case is scanned TWICE: the second run is the independent retest;
any run1 != run2 status is a reproducibility failure.

A mismatch is tagged via app.agentready.with_review so false positives
stay auditable without rewriting stored evidence:
  predicted FAIL but truth is not FAIL  -> false_positive
  predicted PASS/N-A but truth is FAIL  -> false_negative
  anything else mismatched                -> needs_retest

Exit 0 only when accuracy, N/A accuracy, evidence coverage and
reproducibility are all 1.0 and no fabrication is suggested.
Live-site pilot (real network) is still required before scoring use;
this mocked set guards regressions, it does not replace the pilot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agentready as ar
from app.adapters.agentready import AgentReadyAdapter

CASES_PATH = ROOT / "tests" / "fixtures" / "agentready_labeled_cases.json"

STATUS_ENUM = set(ar.STATUS_ENUM)


class FakeResp:
    def __init__(self, status, body="", content_type=""):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type} if content_type else {}


def make_fake(mocks):
    def fake(url, timeout=12):
        # DNS-over-HTTPS endpoints: default is a real empty answer set
        # (NO_RECORDS), overridable per case via the "__doh__" mock.
        if "dns-query" in url or "dns.google" in url:
            spec = mocks.get("__doh__")
            if spec is None:
                return FakeResp(200, '{"Status": 0, "Answer": []}',
                                "application/dns-json"), None
            if "error" in spec:
                return None, spec["error"]
            return FakeResp(spec.get("status", 200), spec.get("body", ""),
                            spec.get("content_type", "application/dns-json")), None
        # Homepage fetch (WebMCP static marker scan): "__homepage__" mock.
        from urllib.parse import urlsplit as _split
        try:
            _path = _split(url).path or "/"
        except Exception:
            _path = url
        if _path in ("", "/"):
            spec = mocks.get("__homepage__")
            if spec is None:
                return FakeResp(404, ""), None
            if "error" in spec:
                return None, spec["error"]
            return FakeResp(spec.get("status", 200), spec.get("body", ""),
                            spec.get("content_type", "text/html")), None
        for suffix, spec in mocks.items():
            if suffix.startswith("__"):
                continue
            if url.endswith(suffix):
                if "error" in spec:
                    return None, spec["error"]
                return FakeResp(spec.get("status", 404), spec.get("body", ""),
                                spec.get("content_type", "")), None
        return FakeResp(404, ""), None
    return fake


def scan_once(case):
    adapter = AgentReadyAdapter()
    with patch("app.adapters.agentready._fetch", side_effect=make_fake(case.get("mocks", {}))):
        result = asyncio.run(adapter.scan(case["domain"], "eval-" + case["id"]))
    out = {}
    for record in result.checks:
        try:
            detail = json.loads(record.evidence)
        except ValueError:
            detail = {}
        out[record.native_check_id] = {
            "status": record.status,
            "applicability": detail.get("applicability"),
            "claim": detail.get("claim"),
            "reason": detail.get("reason"),
            "spec_url": detail.get("spec_url"),
            "next_action": detail.get("next_action"),
            "evidence": detail.get("evidence"),
            "raw": detail,
        }
    return out


_FABRICATE_VERBS = ("publish", "add ", "create", "deploy", "serve ", "declare")
_CONDITIONAL = ("only", "unless", "if ", "when ", "none ", "keep ", "do not",
                "never", "retry", "manual")


def fabrication_free(entry) -> bool:
    """No record may unconditionally tell the publisher to create endpoints.

    PASS advisories (keep in sync, do not auto-remediate) are fine; any
    publish/add/create wording must carry conditional gating (only/unless/
    if/when) so a NOT_APPLICABLE site is never told to fabricate.
    """
    action = entry.get("next_action")
    if not action:
        return True
    lowered = action.lower()
    if not any(v in lowered for v in _FABRICATE_VERBS):
        return True
    return any(w in lowered for w in _CONDITIONAL)


def evaluate(cases=None):
    cases = cases if cases is not None else json.loads(CASES_PATH.read_text())["cases"]
    rows = []
    for case in cases:
        first = scan_once(case)
        second = scan_once(case)  # independent retest
        target = case["check"]
        got = first.get(target, {})
        got2 = second.get(target, {})
        status_ok = got.get("status") == case["expected_status"]
        appl_ok = got.get("applicability") == case["expected_applicability"]
        reproducible = (got.get("status") == got2.get("status")
                        and got.get("applicability") == got2.get("applicability"))
        ev = got.get("evidence") or []
        covered = (got.get("status") in STATUS_ENUM
                   and isinstance(got.get("spec_url"), str)
                   and len(ev) == 1 and isinstance(ev[0].get("locator"), str)
                   and isinstance(ev[0].get("observed"), str))
        ok = status_ok and appl_ok
        if ok:
            verdict = "confirmed"
        elif got.get("status") == "FAIL" and case["expected_status"] != "FAIL":
            verdict = "false_positive"
        elif case["expected_status"] == "FAIL" and got.get("status") != "FAIL":
            verdict = "false_negative"
        else:
            verdict = "needs_retest"
        review = ar.with_review(got.get("raw") or {}, verdict, f"eval {case['id']}")
        rows.append({
            "id": case["id"], "check": target,
            "expected": f"{case['expected_applicability']}/{case['expected_status']}",
            "got": f"{got.get('applicability')}/{got.get('status')}",
            "ok": ok, "verdict": verdict, "review": review.get("review"),
            "reproducible": reproducible, "covered": covered,
            "fabrication_free": fabrication_free(got),
        })
    total = len(rows)
    metrics = {
        "cases": total,
        "accuracy": sum(r["ok"] for r in rows) / total if total else 0,
        "precision_fail": _ratio(rows, lambda r: r["got"].endswith("/FAIL"), lambda r: r["ok"]),
        "na_accuracy": _ratio(rows, lambda r: r["expected"].endswith("/NOT_APPLICABLE"),
                              lambda r: r["ok"]),
        "evidence_coverage": sum(r["covered"] for r in rows) / total if total else 0,
        "reproducibility": sum(r["reproducible"] for r in rows) / total if total else 0,
        "false_positives": sum(r["verdict"] == "false_positive" for r in rows),
        "false_negatives": sum(r["verdict"] == "false_negative" for r in rows),
        "fabrication_free": all(r["fabrication_free"] for r in rows),
    }
    passed = (metrics["accuracy"] == 1.0 and metrics["na_accuracy"] == 1.0
              and metrics["evidence_coverage"] == 1.0 and metrics["reproducibility"] == 1.0
              and metrics["fabrication_free"])
    return {"metrics": metrics, "rows": rows, "passed": passed}


def _ratio(rows, select, good):
    sel = [r for r in rows if select(r)]
    return sum(good(r) for r in sel) / len(sel) if sel else 1.0


def main():
    parser = argparse.ArgumentParser(description="Sprint B AgentReady eval (mocked, no network).")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()
    report = evaluate()
    m = report["metrics"]
    print(f"cases={m['cases']} accuracy={m['accuracy']:.3f} "
          f"precision_fail={m['precision_fail']:.3f} na_accuracy={m['na_accuracy']:.3f} "
          f"coverage={m['evidence_coverage']:.3f} reproducibility={m['reproducibility']:.3f} "
          f"fp={m['false_positives']} fn={m['false_negatives']} "
          f"fabrication_free={m['fabrication_free']} passed={report['passed']}")
    for row in report["rows"]:
        if not row["ok"]:
            print(f"  MISMATCH {row['id']} ({row['check']}): expected {row['expected']}, "
                  f"got {row['got']} -> {row['verdict']}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
