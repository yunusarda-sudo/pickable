"""Pickable web server. Live mode scans via Ora; --demo serves synthetic fixtures."""
import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.reporting import report_from_job
from app.security import normalize_domain, normalize_site_url
from app.agent_policy import AgentPolicy, RequestError, ScanLimiter, ScanRateLimitError
from app.pickable_agent_routes import serve_agent_get, serve_agent_post

MAX_QUEUED = 20
REQUEST_TIMEOUT = 10


class BoundedHTTPServer(ThreadingHTTPServer):
    """Limit simultaneous request handlers, including clients with slow bodies."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def public_report(report):
    """Return the customer report without provider payloads or source evidence blobs."""
    if not isinstance(report, dict):
        return None
    checks = []
    for check in report.get("checks", []) if isinstance(report.get("checks"), list) else []:
        if not isinstance(check, dict):
            continue
        checks.append({key: check.get(key) for key in
                       ("check_id", "title", "category", "measurement_definition", "scoring_status", "scoring_source", "combined_status")})
    recommendations = []
    for item in report.get("recommendations", []) if isinstance(report.get("recommendations"), list) else []:
        if isinstance(item, str):
            recommendations.append({"id": "", "recommendation": item[:3000], "check_id": None})
        elif isinstance(item, dict) and isinstance(item.get("recommendation"), str):
            source_id = item.get("id") if isinstance(item.get("id"), str) else ""
            check = next((row for row in report.get("checks", []) if isinstance(row, dict) and any(
                isinstance(obs, dict) and obs.get("source_check_id") == source_id
                for obs in (row.get("sources") or {}).values())), None)
            entry = {"id": source_id, "recommendation": item["recommendation"][:3000],
                     "check_id": check.get("check_id") if check else item.get("check_id")}
            if item.get("source") == "pickable":
                entry["source"] = "pickable"
            recommendations.append(entry)
    marketing = report.get("marketing")
    if isinstance(marketing, dict):
        source = marketing.get("source")
        marketing_score = marketing.get("score")
        if isinstance(marketing_score, dict):
            marketing_score = {key: marketing_score.get(key) for key in ("total", "grade", "coverage", "breakdown", "final", "label")}
        marketing_checks = []
        for check in marketing.get("checks", []) if isinstance(marketing.get("checks"), list) else []:
            if not isinstance(check, dict):
                continue
            raw_details = check.get("details")
            profiles = raw_details.get("profiles") if isinstance(raw_details, dict) else None
            safe_profiles = {str(name): url for name, url in profiles.items()
                             if isinstance(name, str) and isinstance(url, str)} if isinstance(profiles, dict) else {}
            marketing_checks.append({key: check.get(key) for key in
                                     ("check_id", "title", "category", "scoring_status", "scoring_source", "message", "measurement_definition")}
                                    | {"details": {"profiles": safe_profiles} if safe_profiles else None})
        marketing = {"score": marketing_score,
                     "source": {key: source.get(key) for key in ("id", "status", "observed_at")}
                     if isinstance(source, dict) else None,
                     "checks": marketing_checks}
    score = report.get("score")
    if isinstance(score, dict):
        score = {key: score.get(key) for key in ("total", "grade", "coverage", "breakdown", "final", "label")}
    return {"schema_version": report.get("schema_version"),
            "domain": report.get("domain"), "scanned_at": report.get("scanned_at"),
            "checks": checks, "score": score if isinstance(score, dict) else None, "recommendations": recommendations,
            "marketing": marketing}


def public_job(job):
    if not isinstance(job, dict):
        return None
    return {"id": job.get("id"), "domain": job.get("domain"), "created_at": job.get("created_at"),
            "status": job.get("status"), "sources": [{key: source.get(key) for key in ("id", "status")}
                                                       for source in job.get("sources", []) if isinstance(source, dict)],
            "report": public_report(job.get("report")),
            "intelligence": public_intelligence(job.get("intelligence"))}


def public_intelligence(job):
    """Strict allowlist for customer-facing product analysis.

    Only the lifecycle status and the report content leave the server.
    Model names, token usage, finish reasons and raw error text stay in
    backend logs and internal diagnostics, never in user payloads.
    """
    if not isinstance(job, dict): return None
    report = job.get("report")
    safe_report = None
    if isinstance(report, dict):
        keys = ("analysis_stage", "product_summary", "target_customers", "problem", "value_proposition", "competitive_landscape", "workflows", "evidence_refs", "unknowns", "sources", "web_sources", "research_note", "research_trace")
        safe_report = {key: report[key] for key in keys if key in report}
        safe_report["sources"] = [{"label": row.get("label"), "url": row.get("url"), "retrieved_at": row.get("retrieved_at"), "sha256": row.get("sha256")}
                                   for row in safe_report.get("sources", []) if isinstance(row, dict) and isinstance(row.get("url"), str)]
        safe_report["web_sources"] = [{"label": row.get("label"), "url": row.get("url")}
                                       for row in safe_report.get("web_sources", []) if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"].startswith("https://")][:12]
        trace = safe_report.get("research_trace")
        if isinstance(trace, dict):
            safe_report["research_trace"] = {
                key: trace.get(key) for key in
                ("search_calls", "raw_results", "parsed_sources", "report_citations", "fallback_reason", "latency_ms")
                if isinstance(trace.get(key), int) or (key == "fallback_reason" and isinstance(trace.get(key), str))} or None
        else:
            safe_report.pop("research_trace", None)
    status = job.get("status")
    return {"id": job.get("id"), "depth": job.get("depth"), "status": status,
            "created_at": job.get("created_at"), "finished_at": job.get("finished_at"), "report": safe_report}


def demo_backend():
    sys.path.insert(0, str(ROOT / "tests"))
    from ui_fixture import fixture_runs
    jobs = {}

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
            "intelligence": lambda job_id, depth="short": None, "deep": lambda job_id, owner_ip=None: None,
            "list": lambda owner_ip=None: [{k: v for k, v in job.items() if k not in ("report", "sources", "owner_ip")}
                for job in reversed(list(jobs.values())) if owner_ip is None or job.get("owner_ip") == owner_ip]}


def live_backend(policy=None):
    from app.adapters.cloudflare import CloudflareAdapter
    from app.adapters.growth import GrowthAdapter
    from app.adapters.ora import OraAdapter
    from app.agent_policy import AgentPolicy
    from app.config import Config
    from app.orchestrator import Orchestrator
    from app.storage import Storage
    policy = policy or AgentPolicy.from_env()
    storage = Storage(Config.DB_PATH)
    orchestrator = Orchestrator(adapters=[OraAdapter(), CloudflareAdapter(), GrowthAdapter()], storage=storage)
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
            "shutdown": shutdown,
            "intelligence": get_intelligence, "deep": start_deep}


DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _parse_trusted_proxies(raw):
    """Parse TRUSTED_PROXIES env (comma/space separated IPs or CIDRs)."""
    networks = []
    for part in (raw or "").replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    if not networks:
        networks = [ipaddress.ip_network(c) for c in DEFAULT_TRUSTED_PROXY_CIDRS]
    return tuple(networks)


def _peer_is_trusted(peer_ip, trusted):
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in trusted)


def _real_client_ip(handler, trusted):
    """Resolve the client IP behind Traefik/Cloudflare.

    Forwarding headers are honored ONLY when the direct TCP peer is a trusted
    proxy (docker/loopback by default). Direct internet clients cannot spoof.
    Prefers CF-Connecting-IP (Cloudflare-canonical), then X-Forwarded-For.
    """
    peer = handler.client_address[0]
    if not _peer_is_trusted(peer, trusted):
        return peer
    cf_ip = (handler.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass
    xff = handler.headers.get("X-Forwarded-For") or ""
    first = xff.split(",")[0].strip()
    if first:
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            pass
    return peer


def _came_via_cloudflare(handler):
    """True when Cloudflare edge headers are present (Ray/Connecting-IP/Visitor)."""
    return bool(handler.headers.get("CF-Ray") or handler.headers.get("CF-Connecting-IP")
                or handler.headers.get("CF-Visitor") or handler.headers.get("CF-IPCountry"))


def make_server(port, backend, bind="127.0.0.1", allowed_hosts=(), policy=None):
    token = secrets.token_urlsafe(32)
    policy = policy or AgentPolicy.from_env()
    limiter = ScanLimiter(policy)
    deep_limiter = ScanLimiter(AgentPolicy(scans_per_hour=10, global_scans_per_hour=30))
    trusted_proxies = _parse_trusted_proxies(os.environ.get("TRUSTED_PROXIES", ""))
    # When "1": drop direct-to-origin requests that bypassed the CDN edge.
    # Health checks stay open so the orchestrator/loadbalancer can probe.
    # Loopback is always allowed (local dev, VPS-side curl).
    require_cf = os.environ.get("PICKABLE_REQUIRE_CF", "0") == "1"

    def create_scan(domain, remote_addr, target_url=None):
        domain = normalize_domain(domain)
        limiter.admit(remote_addr)
        return backend["create"](domain, target_url or "https://" + domain + "/", remote_addr)

    def scoped_backend(remote_addr):
        # MCP routes use one-argument reads, so bind every read to the transport peer.
        return {**backend,
                "get": lambda job_id: backend["get"](job_id, remote_addr),
                "list": lambda: backend["list"](remote_addr)}

    class Handler(BaseHTTPRequestHandler):
        # Generic banner: do not advertise the stack to scanners.
        server_version = "nginx"
        sys_version = ""

        @property
        def client_ip(self):
            return _real_client_ip(self, trusted_proxies)

        def _edge_allowed(self, path):
            """Origin lockdown: when REQUIRE_CF=1, only CDN-routed traffic passes.

            Exempts /api/health (orchestrator probes) and loopback callers.
            Everything else without Cloudflare edge headers gets 403, so a
            leaked origin IP alone is not enough to reach the app.
            """
            if not require_cf:
                return True
            if path == "/api/health":
                return True
            if _peer_is_trusted(self.client_address[0], trusted_proxies) and _came_via_cloudflare(self):
                return True
            try:
                if ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return True
            except ValueError:
                pass
            return False

        def setup(self):
            self.request.settimeout(REQUEST_TIMEOUT)
            super().setup()

        def send(self, data, status=200, mime="application/json; charset=utf-8", filename=None, headers=None):
            is_json = mime.split(";")[0] == "application/json" or mime.split(";")[0].endswith("+json")
            content = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() if is_json else data
            if isinstance(content, str):
                content = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept")
            self.send_header("Link", '</sitemap.xml>; rel="sitemap", </openapi.json>; rel="service-desc"; type="application/json", </.well-known/api-catalog>; rel="api-catalog", </llms.txt>; rel="alternate"; type="text/plain", </docs>; rel="help"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            # Honored only on HTTPS; harmless on plain-HTTP behind the edge TLS terminator.
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
            self.send_header("X-Frame-Options", "DENY")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            for name, value in (headers or {}).items():
                self.send_header(name, str(value))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def read_json(self, max_bytes):
            if self.headers.get("Transfer-Encoding"):
                raise RequestError(400, "Chunked request bodies are not supported. Send Content-Length.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise RequestError(400, "A single positive Content-Length is required.")
            if len(lengths[0]) > 10:
                raise RequestError(400, "Content-Length is too long.")
            length = int(lengths[0])
            if length <= 0:
                raise RequestError(400, "A nonempty JSON request body is required.")
            if length > max_bytes:
                raise RequestError(413, f"Request body must not exceed {max_bytes} bytes.", "body_too_large")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise RequestError(415, "Content-Type must be application/json.", "unsupported_media_type")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise RequestError(400, "Request body is shorter than Content-Length.")
                def reject_constant(value):
                    raise ValueError("Non-finite JSON numbers are not accepted.")
                return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            except (socket.timeout, TimeoutError):
                raise RequestError(408, "Request body timed out.", "request_timeout") from None
            except (ValueError, UnicodeError) as error:
                if isinstance(error, RequestError):
                    raise
                raise RequestError(400, "Request body must contain valid UTF-8 JSON.", "invalid_json") from None

        def allowed(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}", *allowed_hosts}
            host = self.headers.get("Host")
            if host not in hosts:
                self.send({"error": "Host not allowed.", "code": "host_not_allowed"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + host, "https://" + host):
                self.send({"error": "Origin not allowed.", "code": "origin_not_allowed"}, 403)
                return False
            if not self._edge_allowed(urlsplit(self.path).path):
                self.send({"error": "Direct origin access is not allowed.", "code": "direct_origin_blocked"}, 403)
                return False
            return True

        def do_GET(self):
            if not self.allowed(): return
            path = urlsplit(self.path).path
            if serve_agent_get(self, path, backend, ROOT, policy): return
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            if path in static:
                filename, mime = static[path]
                content = (ROOT / "web" / filename).read_bytes()
                if path == "/":
                    content = content.replace(b"https://solveny.dev", policy.public_url.encode())
                self.send(content, mime=mime); return
            if path == "/api/config": self.send({"csrf_token": token, "mode": backend["mode"]}); return
            if path == "/api/scans": self.send({"scans": backend["list"](self.client_ip)}); return
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "scans"]:
                job = backend["get"](parts[2], self.client_ip)
                if job:
                    if len(parts) == 3: self.send(job); return
                    if parts[3:] == ["preview"]: self.send({"preview": backend["preview"](parts[2]) or None}); return
                    if parts[3:] in (["intelligence"], ["intelligence", "deep"]):
                        depth = "deep" if parts[3:] == ["intelligence", "deep"] else "short"
                        self.send({"intelligence": backend["intelligence"](parts[2], depth)}); return
                    if parts[3:] == ["report"] and job["report"]: self.send(job["report"], filename="pickable-report.json"); return
            if not path.startswith("/api/") and "text/markdown" in self.headers.get("Accept", ""):
                self.send(b"# Page not found\n\nThis resource does not exist. See [Pickable documentation](/docs), [llms.txt](/llms.txt), or [the sitemap](/sitemap.xml).\n", 404, "text/markdown; charset=utf-8")
            else:
                self.send({"error": "Not found.", "code": "not_found", "documentation_url": policy.public_url + "/docs"}, 404)

        def do_HEAD(self):
            self.do_GET()

        def do_DELETE(self):
            if not self.allowed(): return
            self.send({"error": "Method not allowed. MCP is stateless and supports POST requests.", "code": "method_not_allowed"}, 405,
                      headers={"Allow": "POST" if urlsplit(self.path).path == "/mcp" else "GET, HEAD"})

        def do_OPTIONS(self):
            if not self.allowed(): return
            path = urlsplit(self.path).path
            methods = "POST" if path == "/mcp" else "GET, HEAD, POST" if path == "/api/scans" else "GET, HEAD"
            self.send(b"", 204, "text/plain; charset=utf-8", headers={"Allow": methods})

        def do_POST(self):
            if not self.allowed(): return
            if serve_agent_post(self, scoped_backend(self.client_ip), self.path, policy, create_scan): return
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "scans"] and parts[3] == "intelligence":
                if self.headers.get("X-CSRF-Token") != token:
                    self.send({"error": "Invalid scan token."}, 403); return
                try:
                    if not backend["get"](parts[2], self.client_ip):
                        self.send({"error": "Not found."}, 404); return
                    deep_limiter.admit(self.client_ip)
                    job_id = backend["deep"](parts[2], self.client_ip)
                    if not job_id: self.send({"error": "Analysis is unavailable in demo mode."}, 409); return
                    self.send({"intelligence_job_id": job_id}, 202); return
                except ValueError as error:
                    self.send({"error": str(error)}, 409); return
                except OverflowError as error:
                    self.send({"error": str(error)}, 429); return
            if urlsplit(self.path).path != "/api/scans": self.send({"error": "Endpoint not found."}, 404); return
            if self.headers.get("X-CSRF-Token") != token and not policy.agent_authorized(self.headers.get("Authorization")):
                self.send({"error": "Invalid scan token.", "code": "invalid_scan_token"}, 403); return
            try:
                data = self.read_json(2048)
                if not isinstance(data, dict) or set(data) not in ({"url"}, {"domain"}):
                    raise ValueError("Provide exactly one url or domain field in a JSON object.")
                if "url" in data:
                    domain, target_url = normalize_site_url(data["url"])
                else:
                    domain = normalize_domain(data["domain"])
                    target_url = "https://" + domain + "/"
            except RequestError as error:
                self.close_connection = True
                self.send({"error": error.message, "code": error.code}, error.status); return
            except (ValueError, TypeError, AttributeError) as error:
                self.send({"error": str(error) or "Invalid domain.", "code": "invalid_domain"}, 400); return
            try:
                self.send({"job_id": create_scan(domain, self.client_ip, target_url)}, 202)
            except ScanRateLimitError as error:
                self.send({"error": str(error), "code": "scan_rate_limited", "retry_after": error.retry_after}, 429, headers={"Retry-After": error.retry_after})
            except OverflowError as error:
                self.send({"error": str(error), "code": "scan_queue_full", "retry_after": 60}, 429, headers={"Retry-After": "60"})
            except Exception:
                self.send({"error": "Scan service unavailable. Please retry shortly.", "code": "scan_unavailable"}, 503)

        def log_message(self, *_): pass

    server = BoundedHTTPServer((bind, port), Handler)
    server.scan_limiter = limiter
    server.agent_policy = policy
    return server


def serve(port, bind, demo=False):
    # RENDER_EXTERNAL_HOSTNAME is set by Render, so the public URL works without extra config.
    allowed = [h.strip() for h in (os.environ.get("ALLOWED_HOSTS", "") + "," + os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")).split(",") if h.strip()]
    backend = demo_backend() if demo else live_backend()
    server = make_server(port, backend, bind, allowed)
    print(f"{'DEMO MODE' if demo else 'LIVE'} — http://{bind}:{server.server_port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        if backend.get("shutdown"):
            backend["shutdown"]()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pickable web server.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--bind", default=os.environ.get("BIND", "127.0.0.1"))
    parser.add_argument("--demo", action="store_true", help="Synthetic test data; no real scans.")
    args = parser.parse_args()
    serve(args.port, args.bind, args.demo)
