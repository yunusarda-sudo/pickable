"""Pickable web server. Live mode scans via Ora; --demo serves synthetic fixtures."""
import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.reporting import report_from_job
from app.security import normalize_domain

MAX_QUEUED = 20


def demo_backend():
    sys.path.insert(0, str(ROOT / "tests"))
    from ui_fixture import fixture_runs
    jobs = {}

    def create(domain):
        if len(jobs) >= 30:
            raise OverflowError("Demo session is limited to 30 reports; restart the server.")
        job_id = secrets.token_hex(12)
        report = report_from_job({"domain": domain}, fixture_runs())
        jobs[job_id] = {"id": job_id, "domain": domain, "created_at": datetime.now(timezone.utc).isoformat(), "status": "completed", "sources": report["sources"], "report": report}
        return job_id

    def raw(job_id, source):
        return next((r["raw_report"] for r in fixture_runs() if r["source_id"] == source), None) if job_id in jobs else None

    return {"mode": "demo", "create": create, "get": jobs.get, "raw": raw,
            "list": lambda: [{k: v for k, v in job.items() if k not in ("report", "sources")} for job in reversed(list(jobs.values()))]}


def live_backend():
    from app.adapters.cloudflare import CloudflareAdapter
    from app.adapters.growth import GrowthAdapter
    from app.adapters.ora import OraAdapter
    from app.config import Config
    from app.orchestrator import Orchestrator
    from app.storage import Storage
    storage = Storage(Config.DB_PATH)
    orchestrator = Orchestrator(adapters=[OraAdapter(), CloudflareAdapter(), GrowthAdapter()], storage=storage)
    pending = queue.Queue()

    def worker():
        # ponytail: one scan at a time on one event loop (keeps Ora's rate limiter shared); add workers if queue waits get long
        loop = asyncio.new_event_loop()
        while True:
            job_id, domain = pending.get()
            try:
                loop.run_until_complete(orchestrator.run_scan(domain, job_id))
            except Exception as error:
                storage.update_job_status(job_id, "failed", error=f"{type(error).__name__}: {error}")

    threading.Thread(target=worker, daemon=True).start()

    def create(domain):
        if pending.qsize() >= MAX_QUEUED:
            raise OverflowError("Too many scans are queued right now. Please try again shortly.")
        job_id = storage.create_job(domain)
        pending.put((job_id, domain))
        return job_id

    def get(job_id):
        job = storage.get_job(job_id)
        if not job:
            return None
        runs = storage.get_source_runs(job_id)
        running = job["status"] in ("pending", "running")
        pending = [a.source_id for a in orchestrator.adapters if running and a.source_id not in {r["source_id"] for r in runs}]
        return {"id": job["id"], "domain": job["domain"], "created_at": job["started_at"], "status": job["status"],
                "sources": [{"id": r["source_id"], "status": r["status"], "error": r["error"]} for r in runs],
                "report": report_from_job(job, runs, pending) if runs else None}  # partial: render as soon as the first source lands

    def raw(job_id, source):
        run = next((r for r in storage.get_source_runs(job_id) if r["source_id"] == source), None)
        try:
            return json.loads(run["raw_report"]) if run and run["raw_report"] else None
        except ValueError:
            return None

    def list_jobs():
        return [{"id": r["id"], "domain": r["domain"], "status": r["status"], "created_at": r["started_at"]} for r in storage.list_jobs()]

    return {"mode": "live", "create": create, "get": get, "raw": raw, "list": list_jobs}


def make_server(port, backend, bind="127.0.0.1", allowed_hosts=()):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send(self, data, status=200, mime="application/json; charset=utf-8", filename=None):
            content = json.dumps(data, ensure_ascii=False).encode() if mime.startswith("application/json") else data
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def allowed(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}", *allowed_hosts}
            host = self.headers.get("Host")
            if host not in hosts:
                self.send({"error": "Host not allowed."}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + host, "https://" + host):
                self.send({"error": "Origin not allowed."}, 403)
                return False
            return True

        def do_GET(self):
            if not self.allowed(): return
            path = urlsplit(self.path).path
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            if path in static:
                filename, mime = static[path]; self.send((ROOT / "web" / filename).read_bytes(), mime=mime); return
            if path == "/api/config": self.send({"csrf_token": token, "mode": backend["mode"]}); return
            if path == "/api/scans": self.send({"scans": backend["list"]()}); return
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "scans"]:
                job = backend["get"](parts[2])
                if job:
                    if len(parts) == 3: self.send(job); return
                    if parts[3:] == ["report"] and job["report"]: self.send(job["report"], filename="pickable-report.json"); return
                    if len(parts) == 5 and parts[3] == "raw":
                        raw = backend["raw"](parts[2], parts[4])
                        if raw: self.send(raw, filename=parts[4] + ".json"); return
            self.send({"error": "Not found."}, 404)

        def do_POST(self):
            if not self.allowed(): return
            if self.path != "/api/scans": self.send({"error": "Endpoint not found."}, 404); return
            if self.headers.get("X-CSRF-Token") != token:
                self.send({"error": "Invalid scan token."}, 403); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.close_connection = True
                    self.send({"error": "JSON body is invalid or too large."}, 400); return
                data = json.loads(self.rfile.read(length))
                domain = normalize_domain(data.get("domain"))
            except (ValueError, TypeError, AttributeError) as error:
                self.send({"error": str(error) or "Invalid domain."}, 400); return
            try:
                self.send({"job_id": backend["create"](domain)}, 202)
            except OverflowError as error:
                self.send({"error": str(error)}, 429)

        def log_message(self, *_): pass

    return ThreadingHTTPServer((bind, port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pickable web server.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--bind", default=os.environ.get("BIND", "127.0.0.1"))
    parser.add_argument("--demo", action="store_true", help="Synthetic test data; no real scans.")
    args = parser.parse_args()
    allowed = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    server = make_server(args.port, demo_backend() if args.demo else live_backend(), args.bind, allowed)
    print(f"{'DEMO MODE' if args.demo else 'LIVE'} — http://127.0.0.1:{server.server_port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
