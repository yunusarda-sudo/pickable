"""Fixture-only local UI preview. Never imports or calls live adapters."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.reporting import report_from_job
from app.security import normalize_domain
from ui_fixture import fixture_runs


def make_server(port=8765):
    jobs = {}
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
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host")
            if host not in allowed_hosts:
                self.send({"error": "Host engellendi."}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                self.send({"error": "Origin engellendi."}, 403)
                return False
            return True

        def do_GET(self):
            if not self.allowed(): return
            path = urlsplit(self.path).path
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            if path in static:
                filename, mime = static[path]; self.send((ROOT / "web" / filename).read_bytes(), mime=mime); return
            if path == "/api/config": self.send({"csrf_token": token, "mode": "demo"}); return
            if path == "/api/scans": self.send({"scans": [{k: v for k, v in job.items() if k not in ("report", "sources")} for job in reversed(list(jobs.values()))]}); return
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "scans"] and parts[2] in jobs:
                job = jobs[parts[2]]
                if len(parts) == 3: self.send(job); return
                if parts[3:] == ["report"]: self.send(job["report"], filename="pickable-test-report.json"); return
                if len(parts) == 5 and parts[3] == "raw":
                    raw = next((r["raw_report"] for r in fixture_runs() if r["source_id"] == parts[4]), None)
                    if raw: self.send(raw, filename=parts[4] + "-test.json"); return
            self.send({"error": "Kayıt bulunamadı."}, 404)

        def do_POST(self):
            if not self.allowed(): return
            if self.path != "/api/scans": self.send({"error": "Endpoint bulunamadı."}, 404); return
            if self.headers.get("X-CSRF-Token") != token:
                self.send({"error": "Tarama tokenı geçersiz."}, 403); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.close_connection = True
                    self.send({"error": "JSON gövdesi geçersiz veya çok büyük."}, 400); return
                data = json.loads(self.rfile.read(length))
                domain = normalize_domain(data.get("domain"))
            except (ValueError, TypeError, AttributeError) as error:
                self.send({"error": str(error) or "Geçersiz domain."}, 400); return
            if len(jobs) >= 30:
                self.send({"error": "Test oturumu 30 raporla sınırlı; preview sunucusunu yeniden başlatın."}, 429); return
            job_id = secrets.token_hex(12)
            report = report_from_job({"domain": domain}, fixture_runs())
            jobs[job_id] = {"id": job_id, "domain": domain, "created_at": datetime.now(timezone.utc).isoformat(), "status": "completed", "sources": report["sources"], "report": report}
            self.send({"job_id": job_id}, 202)

        def log_message(self, *_): pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pickable UI sunucusu")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true", help="Canlı tarama modu (Ora ve AgentReady OSS)")
    args = parser.parse_args()
    if args.live:
        from app.server import make_live_server
        server = make_live_server(args.port)
        print(f"CANLI MOD — http://127.0.0.1:{server.server_port} — Ora ve AgentReady OSS paralel çalışıyor", flush=True)
    else:
        server = make_server(args.port)
        print(f"TEST MODU — http://127.0.0.1:{server.server_port} — gerçek tarama yapılmaz", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
