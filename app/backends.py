"""Scan backends: demo fixtures vs live orchestrator + workers."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.presenters import public_intelligence, public_job, public_report
from app.reporting import report_from_job
from app.waitlist import MemoryWaitlist

ROOT = Path(__file__).resolve().parent.parent

MAX_QUEUED = 20


def demo_backend():
    sys.path.insert(0, str(ROOT / "tests"))
    from ui_fixture import fixture_runs
    jobs = {}
    waitlist = MemoryWaitlist()

    def create(domain, target_url=None, owner_ip=None):
        if len(jobs) >= 30:
            raise OverflowError("Demo session is limited to 30 reports; restart the server.")
        job_id = secrets.token_hex(12)
        report = report_from_job({"domain": domain}, fixture_runs())
        jobs[job_id] = {"id": job_id, "domain": domain, "owner_ip": owner_ip, "created_at": datetime.now(timezone.utc).isoformat(), "status": "completed", "sources": report["sources"], "report": report}
        return job_id

    def raw(job_id, source):
        return next((r["raw_report"] for r in fixture_runs() if r["source_id"] == source), None) if job_id in jobs else None

    preview = lambda job_id: job_id in jobs and {"url": f"https://{jobs[job_id]['domain']}/", "title": "Example Domain (test data)", "description": "Demo mode does not fetch the site.", "image": None, "icon": None}
    def get(job_id, owner_ip=None):
        item = jobs.get(job_id)
        return public_job(item) if item and (owner_ip is None or item.get("owner_ip") == owner_ip) else None
    return {"mode": "demo", "create": create, "get": get, "raw": raw, "preview": preview,
            "waitlist": waitlist.signup, "record_event": waitlist.record_event,
            "intelligence": lambda job_id, depth="short": None, "deep": lambda job_id, owner_ip=None: None,
            "list": lambda owner_ip=None: [{k: v for k, v in job.items() if k not in ("report", "sources", "owner_ip")}
                for job in reversed(list(jobs.values())) if owner_ip is None or job.get("owner_ip") == owner_ip]}


def _live_adapters():
    from app.adapters.cloudflare import CloudflareAdapter
    from app.adapters.growth import GrowthAdapter
    from app.adapters.ora import OraAdapter
    adapters = [OraAdapter(), CloudflareAdapter(), GrowthAdapter()]
    # Sprint A shadow-mode: read-only AgentReady probes run alongside the
    # scored sources but never affect the score. Opt-in via env so default
    # deployments are unchanged until Sprint B validates precision.
    if os.environ.get("PICKABLE_AGENTREADY_SHADOW", "0") == "1":
        try:
            from app.adapters.agentready import AgentReadyAdapter
            adapters.append(AgentReadyAdapter())
        except Exception:
            pass
    return adapters


def live_backend(policy=None):
    from app.agent_policy import AgentPolicy
    from app.config import Config
    from app.orchestrator import Orchestrator
    from app.storage import Storage
    policy = policy or AgentPolicy.from_env()
    storage = Storage(Config.DB_PATH)
    try:
        # One line so a wiped/moved database is visible in the service logs:
        # a live restart that drops to 0 rows means the DB file did not survive.
        waiting = storage.conn.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()["n"]
        print(f"waitlist: {waiting} rows in {Config.DB_PATH}", flush=True)
    except Exception:
        pass
    orchestrator = Orchestrator(adapters=_live_adapters(), storage=storage)
    pending = queue.Queue(maxsize=MAX_QUEUED)
    admission_lock = threading.Lock()
    intelligence_pending = queue.Queue(maxsize=MAX_QUEUED)
    stop_workers = threading.Event()
    shutdown_lock = threading.Lock()
    storage_closed = False
    # Spend guard: in-flight (domain, depth) analyses. A repeat visitor for a
    # site that is already being analyzed never triggers a second model call:
    # readers simply retry on the next poll and then hit the reuse path above.
    # Best-effort by design; a missed join only ever costs one extra call,
    # never correctness. Nothing here blocks an HTTP handler: read paths and
    # the scan worker return immediately and let the UI poll again.
    flight_lock = threading.Lock()
    flight = set()
    from app.intelligence import IntelligenceProvider, evidence_pages, fetch_evidence

    provider = IntelligenceProvider()

    def intelligence_model(depth):
        model_for = getattr(provider, "model_for", None)
        return model_for(depth) if callable(model_for) else provider.model

    def _reuse_cutoff():
        if not policy.intel_reuse_hours:
            return None
        return (datetime.now(timezone.utc) - timedelta(hours=policy.intel_reuse_hours)).isoformat()

    def _try_reuse(scan_id, domain, depth):
        """Copy a recent completed same-domain analysis; no model call. Returns job_id or None."""
        cutoff = _reuse_cutoff()
        if cutoff is None:
            return None
        hit = storage.find_completed_intelligence(domain, depth, cutoff)
        if not isinstance(hit, dict):
            return None
        job_id, _ = storage.create_intelligence_job(scan_id, depth, intelligence_model(depth))
        storage.update_intelligence_job(job_id, "completed", report=hit)
        return job_id

    def queue_intelligence(scan_id, depth="short", request_ip=None):
        scan = storage.get_job(scan_id)
        if not scan: return None
        if request_ip and scan.get("owner_ip") != request_ip: return None
        # Short analysis only needs the domain, so it is queued while the
        # readiness scan is still running (parallel). Terminally failed
        # scans never start new analyses; an already-running one is simply
        # ignored by the client.
        if scan["status"] not in ("completed", "pending", "running"): raise ValueError("The site scan must finish before product analysis starts.")
        domain = scan["domain"]
        existing = storage.get_intelligence_job(scan_id, depth)
        if existing and existing["status"] in ("queued", "running"):
            return existing["id"]
        reused = _try_reuse(scan_id, domain, depth)
        if reused: return reused
        key = (domain, depth)
        with flight_lock:
            in_flight = key in flight
        if not in_flight:
            # Cross-process guard: another worker process may hold the
            # in-flight analysis in the shared SQLite file.
            try:
                in_flight = storage.has_inflight_intelligence(domain, depth)
            except Exception:
                pass
        if in_flight:
            if depth == "deep":
                raise OverflowError("Deep analysis for this site is already running; please try again shortly.")
            # Short analyses are retried by the next UI poll, which then hits reuse.
            return None
        if depth == "deep":
            today = datetime.now(timezone.utc).date().isoformat()
            claimed = storage.try_claim_deep_budget(scan_id, intelligence_model(depth), today, policy.deep_per_day)
            if claimed is None:
                raise ValueError("Today's deep-research budget is used up. The short analysis above remains available; try deep again tomorrow.")
            with flight_lock:
                flight.add(key)
            try: intelligence_pending.put_nowait((claimed, scan_id, depth))
            except queue.Full:
                with flight_lock: flight.discard(key)
                storage.update_intelligence_job(claimed, "unavailable", error="Intelligence queue is full.")
                raise OverflowError("Deep analysis queue is full; try again shortly.")
            return claimed
        if not getattr(provider, "available", True):
            # No analysis backend configured (e.g. no Ollama/OpenAI): record that
            # plainly instead of queueing work that can never succeed. Cached
            # reuse above still applies, so previously completed analyses stay visible.
            job_id, _ = storage.create_intelligence_job(scan_id, depth, intelligence_model(depth))
            storage.update_intelligence_job(job_id, "unavailable",
                                            error=getattr(provider, "unavailable_reason", None)
                                            or "Product analysis is not configured on this server.")
            if depth == "deep": raise RuntimeError("Product analysis is not configured on this server.")
            return job_id
        job_id, created = storage.create_intelligence_job(scan_id, depth, intelligence_model(depth))
        if created:
            with flight_lock:
                flight.add(key)
            try: intelligence_pending.put_nowait((job_id, scan_id, depth))
            except queue.Full:
                with flight_lock: flight.discard(key)
                storage.update_intelligence_job(job_id, "unavailable", error="Intelligence queue is full.")
                if depth == "deep": raise OverflowError("Deep analysis queue is full; try again shortly.")
        return job_id

    def intelligence_worker():
        while not stop_workers.is_set():
            try:
                analysis_id, scan_id, depth = intelligence_pending.get(timeout=0.1)
            except queue.Empty:
                continue
            evidence = None
            preliminary = None
            flight_key = None
            try:
                scan = storage.get_job(scan_id)
                if scan:
                    flight_key = (scan.get("domain"), depth)
                storage.update_intelligence_job(analysis_id, "running")
                from app.intelligence import quick_analysis
                def publish_homepage_summary(homepage):
                    nonlocal preliminary
                    preliminary = quick_analysis(homepage)
                    storage.update_intelligence_job(analysis_id, "running", report=preliminary)

                evidence = fetch_evidence(scan.get("target_url") or scan["domain"],
                                          extra_pages=3 if depth == "deep" else 1,
                                          on_homepage=publish_homepage_summary)
                if preliminary is None:
                    preliminary = quick_analysis(evidence)
                    storage.update_intelligence_job(analysis_id, "running", report=preliminary)
                report = provider.analyze(evidence, depth, {"domain": scan["domain"], "status": scan["status"]})
                report["analysis_stage"] = "complete"
                report["sources"] = [{"label": (page.get("title") or page.get("ref") or "Public page")[:180],
                                      "url": page["url"], "retrieved_at": page.get("retrieved_at"), "sha256": page.get("sha256")}
                                     for page in evidence_pages(evidence) if isinstance(page.get("url"), str)]
                if depth == "deep" and len(report["unknowns"]) < 12:
                    report["unknowns"].append("Competitor findings need explicit source-level review before they can be treated as verified.")
                storage.update_intelligence_job(analysis_id, "completed", report=report,
                                                usage=getattr(provider, "last_usage", None),
                                                finish_reason=getattr(provider, "last_finish", None))
            except Exception as error:
                # API-side work must never leave the client empty-handed:
                # keep the published preliminary summary and record the cause.
                finish = getattr(provider, "last_finish", None)
                usage = getattr(provider, "last_usage", None)
                detail = f"{type(error).__name__}: {error}"
                if isinstance(finish, str) and finish:
                    detail += f" (finish={finish})"
                status = "partial" if preliminary is not None else "failed"
                storage.update_intelligence_job(analysis_id, status, report=preliminary,
                                                error=detail[:1000], usage=usage,
                                                finish_reason=finish if isinstance(finish, str) else None)
            finally:
                intelligence_pending.task_done()
                if flight_key is not None:
                    with flight_lock:
                        flight.discard(flight_key)

    intelligence_thread = threading.Thread(target=intelligence_worker, daemon=True, name="pickable-intelligence-worker")
    intelligence_thread.start()

    def worker():
        # ponytail: one scan at a time on one event loop (keeps Ora's rate limiter shared); add workers if queue waits get long
        loop = asyncio.new_event_loop()
        while not stop_workers.is_set():
            try:
                job_id, domain = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if job_id is None:
                    continue
                loop.run_until_complete(orchestrator.run_scan(domain, job_id))
                try:
                    queue_intelligence(job_id, "short")
                except Exception:
                    # The readiness scan itself is complete; a full intelligence
                    # queue must not retroactively fail it. The client can retry
                    # short analysis via GET (lazy ensure) or deep via POST.
                    pass
            except Exception as error:
                storage.update_job_status(job_id, "failed", error=f"{type(error).__name__}: {error}")
            finally:
                pending.task_done()
        loop.close()

    scan_thread = threading.Thread(target=worker, daemon=True, name="pickable-scan-worker")
    scan_thread.start()

    def shutdown():
        """Stop backend workers and close their SQLite connection after in-flight work finishes."""
        nonlocal storage_closed
        with shutdown_lock:
            if storage_closed:
                return
        stop_workers.set()
        with flight_lock:
            flight.clear()
        # Workers poll with timeout (scan) or 0.1s (intelligence), so they
        # always observe stop_workers even if the sentinel is lost because the
        # queue was full. The sentinel only wakes an idle worker faster.
        # No worker may touch storage after this point: callers must stop
        # issuing create/get/deep calls before shutdown, and the DB file must
        # not be deleted until shutdown returns.
        for _ in range(2):
            try:
                pending.put_nowait((None, None))
            except queue.Full:
                break
        scan_thread.join(timeout=10)
        intelligence_thread.join(timeout=10)
        alive = [thread.name for thread in (scan_thread, intelligence_thread) if thread.is_alive()]
        if alive:
            raise TimeoutError("Backend workers did not stop: " + ", ".join(alive))
        with shutdown_lock:
            if not storage_closed:
                with storage._lock:
                    storage.conn.close()
                    storage._closed = True
                storage_closed = True

    def create(domain, target_url=None, owner_ip=None):
        with admission_lock:
            if pending.full():
                raise OverflowError("Too many scans are queued right now. Please try again shortly.")
            job_id = storage.create_job(domain, target_url or ("https://" + domain + "/"), owner_ip)
            pending.put_nowait((job_id, domain))
            try:
                # Product analysis needs only the domain, so it starts in
                # parallel with the readiness scan instead of after it.
                queue_intelligence(job_id, "short")
            except Exception:
                # Readiness must never fail because analysis queueing did;
                # the client lazily re-queues short analysis on later polls.
                pass
            return job_id

    def get(job_id, owner_ip=None):
        job = storage.get_job(job_id)
        if not job:
            return None
        if owner_ip and job.get("owner_ip") != owner_ip: return None
        runs = storage.get_source_runs(job_id)
        running = job["status"] in ("pending", "running")
        pending = [a.source_id for a in orchestrator.adapters if running and a.source_id not in {r["source_id"] for r in runs}]
        report = report_from_job(job, runs, pending) if runs else None
        intel = storage.get_intelligence_job(job_id, "short")
        if intel is None and job["status"] == "completed":
            try:
                queue_intelligence(job_id, "short")
                intel = storage.get_intelligence_job(job_id, "short")
            except Exception:
                pass
        return {"id": job["id"], "domain": job["domain"], "created_at": job["started_at"], "status": job["status"],
                "sources": [{"id": r["source_id"], "status": r["status"]} for r in runs],
                "report": public_report(report) if report else None,
                "intelligence": public_intelligence(intel)}  # partial: render as soon as the first source lands

    def get_intelligence(job_id, depth="short"):
        if not storage.get_job(job_id): return None
        job = storage.get_intelligence_job(job_id, depth)
        if job is None and depth == "short":
            # Server restart (or a full queue) may have skipped the auto-queued
            # short analysis. Lazily resume it via the API so the UI never
            # stays empty just because the browser was closed mid-run.
            try:
                queue_intelligence(job_id, "short")
                job = storage.get_intelligence_job(job_id, depth)
            except Exception:
                pass
        return public_intelligence(job)

    def start_deep(job_id, owner_ip=None):
        return queue_intelligence(job_id, "deep", owner_ip)

    def raw(job_id, source):
        run = next((r for r in storage.get_source_runs(job_id) if r["source_id"] == source), None)
        try:
            return json.loads(run["raw_report"]) if run and run["raw_report"] else None
        except ValueError:
            return None

    def list_jobs(owner_ip=None):
        rows = storage.list_jobs()
        return [{"id": r["id"], "domain": r["domain"], "status": r["status"], "created_at": r["started_at"]}
                for r in rows if owner_ip is None or r.get("owner_ip") == owner_ip]

    from app.adapters.growth import fetch_preview
    previews = OrderedDict()
    preview_lock = threading.Lock()

    def preview(job_id):
        # Cache misses/failures too, so repeated agent reads do not repeat outbound fetches.
        with preview_lock:
            cached = previews.get(job_id)
            if cached and time.monotonic() - cached[0] < 300:
                previews.move_to_end(job_id)
                return cached[1]
            job = storage.get_job(job_id)
            value = fetch_preview(job["domain"]) if job else None
            previews[job_id] = (time.monotonic(), value)
            previews.move_to_end(job_id)
            while len(previews) > 128:
                previews.popitem(last=False)
            return value

    return {"mode": "live", "create": create, "get": get, "raw": raw, "list": list_jobs, "preview": preview,
            "shutdown": shutdown, "waitlist": storage.add_waitlist_signup, "record_event": storage.record_event,
            "intelligence": get_intelligence, "deep": start_deep}
