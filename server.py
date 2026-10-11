"""Pickable web server. Live mode scans via Ora; --demo serves synthetic fixtures."""
import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from app.security import normalize_domain
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

    preview = lambda job_id: job_id in jobs and {"url": f"https://{jobs[job_id]['domain']}/", "title": "Example Domain (test data)", "description": "Demo mode does not fetch the site.", "image": None, "icon": None}
    return {"mode": "demo", "create": create, "get": jobs.get, "raw": raw, "preview": preview,
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
    pending = queue.Queue(maxsize=MAX_QUEUED)
    admission_lock = threading.Lock()

    def worker():
        # ponytail: one scan at a time on one event loop (keeps Ora's rate limiter shared); add workers if queue waits get long
        loop = asyncio.new_event_loop()
        while True:
            job_id, domain = pending.get()
            try:
                loop.run_until_complete(orchestrator.run_scan(domain, job_id))
            except Exception as error:
                storage.update_job_status(job_id, "failed", error=f"{type(error).__name__}: {error}")
            finally:
                pending.task_done()

    threading.Thread(target=worker, daemon=True).start()

    def create(domain):
        with admission_lock:
            if pending.full():
                raise OverflowError("Too many scans are queued right now. Please try again shortly.")
            job_id = storage.create_job(domain)
            pending.put_nowait((job_id, domain))
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

    return {"mode": "live", "create": create, "get": get, "raw": raw, "list": list_jobs, "preview": preview}


def make_server(port, backend, bind="127.0.0.1", allowed_hosts=(), policy=None):
    token = secrets.token_urlsafe(32)
    policy = policy or AgentPolicy.from_env()
    limiter = ScanLimiter(policy)

    def create_scan(domain, remote_addr):
        domain = normalize_domain(domain)
        limiter.admit(remote_addr)
        return backend["create"](domain)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Pickable/1.0"
        sys_version = ""

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
            if path == "/api/scans": self.send({"scans": backend["list"]()}); return
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "scans"]:
                job = backend["get"](parts[2])
                if job:
                    if len(parts) == 3: self.send(job); return
                    if parts[3:] == ["preview"]: self.send({"preview": backend["preview"](parts[2]) or None}); return
                    if parts[3:] == ["report"] and job["report"]: self.send(job["report"], filename="pickable-report.json"); return
                    if len(parts) == 5 and parts[3] == "raw":
                        raw = backend["raw"](parts[2], parts[4])
                        if raw is not None: self.send(raw, filename=parts[4] + ".json"); return
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
            if serve_agent_post(self, backend, self.path, policy, create_scan): return
            if urlsplit(self.path).path != "/api/scans": self.send({"error": "Endpoint not found.", "code": "not_found"}, 404); return
            if self.headers.get("X-CSRF-Token") != token and not policy.agent_authorized(self.headers.get("Authorization")):
                self.send({"error": "Invalid scan token. Obtain X-CSRF-Token from /api/config or use an operator-issued bearer key.", "code": "invalid_scan_token"}, 403); return
            try:
                data = self.read_json(2048)
                if not isinstance(data, dict) or set(data) != {"domain"}:
                    raise ValueError("Provide exactly one domain field in a JSON object.")
                domain = normalize_domain(data["domain"])
            except RequestError as error:
                self.close_connection = True
                self.send({"error": error.message, "code": error.code}, error.status); return
            except (ValueError, TypeError, AttributeError) as error:
                self.send({"error": str(error) or "Invalid domain.", "code": "invalid_domain"}, 400); return
            try:
                self.send({"job_id": create_scan(domain, self.client_address[0])}, 202)
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
    server = make_server(port, demo_backend() if demo else live_backend(), bind, allowed)
    print(f"{'DEMO MODE' if demo else 'LIVE'} — http://{bind}:{server.server_port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pickable web server.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--bind", default=os.environ.get("BIND", "127.0.0.1"))
    parser.add_argument("--demo", action="store_true", help="Synthetic test data; no real scans.")
    args = parser.parse_args()
    serve(args.port, args.bind, args.demo)
