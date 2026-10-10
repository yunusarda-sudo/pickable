"""
Subprocess runner for Python AgentReady scanner.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from app.adapters.base import Adapter, AdapterResult, CheckRecord
from app.config import Config


class AgentReadyOSSAdapter(Adapter):
    source_id = "agentready_oss"

    def __init__(
        self,
        script_path: Optional[str] = None,
        timeout: Optional[int] = None,
        max_stdout: int = 10_000_000,
    ):
        if script_path:
            self.script_path = script_path
        else:
            # Check standard expected locations
            candidates = [
                os.path.join(Config.VENDOR_AGENTREADY, "scripts", "agent_ready_scan.py"),
                os.path.join(Config.VENDOR_AGENTREADY, "scanner.py"),
            ]
            self.script_path = next((c for c in candidates if os.path.exists(c)), candidates[0])

        self.timeout = timeout or Config.SUBPROCESS_TIMEOUT
        self.max_stdout = max_stdout

    async def health(self) -> str:
        if os.path.exists(self.script_path):
            return "ok"
        return "unavailable"

    def _map_status(self, raw_status: str) -> str:
        status_norm = (raw_status or "").strip().lower()
        mapping = {
            "pass": "PASS",
            "passed": "PASS",
            "fail": "FAIL",
            "failed": "FAIL",
            "warning": "WARNING",
            "warn": "WARNING",
            "neutral": "UNKNOWN",
            "na": "NA",
            "n/a": "NA",
            "skip": "NA",
            "skipped": "NA",
            "error": "ERROR",
            "unknown": "UNKNOWN",
        }
        return mapping.get(status_norm, "UNKNOWN")

    def _extract_checks(
        self, data: Dict[str, Any], job_id: str, observed_at: str
    ) -> List[CheckRecord]:
        checks: List[CheckRecord] = []
        raw_checks = data.get("checks", {})

        if isinstance(raw_checks, dict):
            for cat_or_id, content in raw_checks.items():
                if isinstance(content, dict):
                    # Check if nested by category: {"discoverability": {"robots_txt": {...}}}
                    # or flat: {"robots_txt": {"result": "pass"}}
                    if any(
                        isinstance(v, dict) and ("result" in v or "status" in v)
                        for v in content.values()
                    ):
                        for cid, item in content.items():
                            if isinstance(item, dict):
                                raw_status = item.get("status") or item.get("result", "unknown")
                                evidence_str = json.dumps(item)
                                checks.append(
                                    CheckRecord(
                                        job_id=job_id,
                                        source_id=self.source_id,
                                        native_check_id=str(cid),
                                        canonical_check_id=None,
                                        status=self._map_status(raw_status),
                                        evidence=evidence_str,
                                        observed_at=observed_at,
                                    )
                                )
                    else:
                        raw_status = content.get("status") or content.get("result", "unknown")
                        evidence_str = json.dumps(content)
                        checks.append(
                            CheckRecord(
                                job_id=job_id,
                                source_id=self.source_id,
                                native_check_id=str(cat_or_id),
                                canonical_check_id=None,
                                status=self._map_status(raw_status),
                                evidence=evidence_str,
                                observed_at=observed_at,
                            )
                        )
        return checks

    def _run_subprocess(self, target_url: str, output_path: str, temp_dir: str) -> None:
        cmd = [
            sys.executable,
            os.path.abspath(self.script_path),
            target_url,
            "--json",
            output_path,
        ]
        # Strict isolated environment passing only PATH
        proc_env = {"PATH": os.environ.get("PATH", "")}

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=temp_dir,
            env=proc_env,
        )

        try:
            stdout_data, stderr_data = proc.communicate(timeout=self.timeout)
            if len(stdout_data) > self.max_stdout or len(stderr_data) > self.max_stdout:
                proc.kill()
                raise ValueError("Subprocess output exceeded max_stdout (10MB)")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise TimeoutError(f"Subprocess timed out after {self.timeout}s")

        # scanner.py returns 0 if level >= 1, 1 if level 0. Both produce valid JSON report!
        # Only raise if output_path is missing or empty
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            err_msg = stderr_data.decode("utf-8", errors="replace")
            raise RuntimeError(f"Subprocess failed without generating output. Stderr: {err_msg}")

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        target_url = f"https://{domain}"

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "report.json")
            loop = asyncio.get_running_loop()

            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._run_subprocess(target_url, output_path, temp_dir),
                )
            except TimeoutError as e:
                raise
            except Exception as e:
                return AdapterResult(
                    source_id=self.source_id,
                    status="failed",
                    raw_report=None,
                    checks=[],
                    error=str(e),
                )

            if not os.path.exists(output_path):
                return AdapterResult(
                    source_id=self.source_id,
                    status="failed",
                    raw_report=None,
                    checks=[],
                    error="Report file not found",
                )

            with open(output_path, "r", encoding="utf-8") as f:
                raw_report = f.read()

            try:
                data = json.loads(raw_report)
            except Exception as e:
                return AdapterResult(
                    source_id=self.source_id,
                    status="failed",
                    raw_report=raw_report,
                    checks=[],
                    error=f"JSON parsing error: {e}",
                )

            checks = self._extract_checks(data, job_id, observed_at)

            return AdapterResult(
                source_id=self.source_id,
                status="completed",
                raw_report=raw_report,
                checks=checks,
                error=None,
            )
