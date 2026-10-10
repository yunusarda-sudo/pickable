#!/usr/bin/env python3
"""
CLI entry point for Agent Readiness scanning and reporting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.adapters.cloudflare import CloudflareAdapter
from app.adapters.growth import GrowthAdapter
from app.adapters.ora import OraAdapter
from app.config import Config
from app.orchestrator import Orchestrator
from app.storage import Storage


def cmd_scan(args: argparse.Namespace) -> int:
    domain = args.domain.strip().lower()
    # Remove protocol prefix if user entered full URL
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.split("/")[0]

    storage = Storage(Config.DB_PATH)
    ora = OraAdapter()
    orchestrator = Orchestrator(adapters=[ora, CloudflareAdapter(), GrowthAdapter()], storage=storage)

    job_id = asyncio.run(orchestrator.run_scan(domain))
    job = storage.get_job(job_id)

    output = {
        "job_id": job_id,
        "domain": domain,
        "status": job["status"] if job else "unknown",
    }
    print(json.dumps(output, indent=2))
    storage.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    storage = Storage(Config.DB_PATH)
    job = storage.get_job(args.job_id)
    if not job:
        print(json.dumps({"error": f"Job {args.job_id} not found"}, indent=2))
        storage.close()
        return 1

    runs = storage.get_source_runs(args.job_id)
    checks = storage.get_checks(args.job_id)

    formatted_runs = {}
    for r in runs:
        raw_parsed = None
        if r.get("raw_report"):
            try:
                raw_parsed = json.loads(r["raw_report"])
            except Exception:
                raw_parsed = r["raw_report"]
        formatted_runs[r["source_id"]] = {
            "status": r["status"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "error": r["error"],
            "raw_report": raw_parsed,
        }

    output = {
        "job": job,
        "sources": formatted_runs,
        "checks_count": len(checks),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    storage.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Multi-source Agent Readiness CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan sub-command
    scan_parser = subparsers.add_parser("scan", help="Scan a domain using all adapters")
    scan_parser.add_argument("domain", help="Target domain (e.g., randevuo.com.tr)")
    scan_parser.set_defaults(func=cmd_scan)

    # show sub-command
    show_parser = subparsers.add_parser("show", help="Show saved raw reports for a job_id")
    show_parser.add_argument("job_id", help="Job ID")
    show_parser.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
