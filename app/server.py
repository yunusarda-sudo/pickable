"""
Production-ready live HTTP server for Pickable Web UI.
Uses pure standard library ThreadingHTTPServer and connects directly to SQLite and Orchestrator.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.agentready_oss import AgentReadyOSSAdapter
from app.adapters.ora import OraAdapter
from app.config import Config
from app.orchestrator import Orchestrator
from app.reporting import report_from_job
from app.security import normalize_domain
from app.storage import Storage


def make_live_server(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    storage = Storage(Config.DB_PATH)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send(self, data, status=200, mime="application/json; charset=utf-8", filename=None):
            if mime.startswith("application/json") and not isinstance(data, (bytes, bytearray)):
                content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                content = data.encode("utf-8")
            else:
                content = data

            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def allowed(self) -> bool:
            # Allow any host for Render deployment
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path

            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            if path in static_files:
                filename, mime = static_files[path]
                file_path = ROOT / "web" / filename
                if file_path.exists():
                    self.send(file_path.read_bytes(), mime=mime)
                    return

            if path == "/api/config":
                self.send({"csrf_token": token, "mode": "live"})
                return

            if path == "/api/scans":
                jobs = storage.list_jobs(50)
                scans = [
                    {
                        "id": j["id"],
                        "job_id": j["id"],
                        "domain": j["domain"],
                        "status": j["status"],
                        "created_at": j["started_at"],
                    }
                    for j in jobs
                ]
                self.send({"scans": scans})
                return

            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "scans"]:
                job_id = parts[2]
                job = storage.get_job(job_id)
                if not job:
                    self.send({"error": "Kayıt bulunamadı."}, 404)
                    return

                runs = storage.get_source_runs(job_id)

                if len(parts) == 3:
                    if job["status"] in ("pending", "running"):
                        sources = [
                            {
                                "id": r["source_id"],
                                "source_id": r["source_id"],
                                "status": r["status"],
                                "error": r.get("error"),
                            }
                            for r in runs
                        ]
                        self.send({
                            "id": job["id"],
                            "job_id": job["id"],
                            "domain": job["domain"],
                            "status": job["status"],
                            "sources": sources,
                            "report": None,
                        })
                        return

                    try:
                        report = report_from_job(job, runs)
                        sources = report.get("sources", [])
                    except Exception as err:
                        report = None
                        sources = [
                            {
                                "id": r["source_id"],
                                "source_id": r["source_id"],
                                "status": r["status"],
                                "error": r.get("error"),
                            }
                            for r in runs
                        ]

                    self.send({
                        "id": job["id"],
                        "job_id": job["id"],
                        "domain": job["domain"],
                        "status": job["status"],
                        "sources": sources,
                        "report": report,
                    })
                    return

                if parts[3:] == ["report"]:
                    try:
                        report = report_from_job(job, runs)
                        self.send(report, filename=f"pickable-{job['domain']}-report.json")
                    except Exception as err:
                        self.send({"error": f"Rapor oluşturulamadı: {err}"}, 500)
                    return

                if len(parts) == 5 and parts[3] == "raw":
                    source_id = parts[4]
                    run = next((r for r in runs if r["source_id"] == source_id), None)
                    if run and run.get("raw_report"):
                        raw_data = run["raw_report"]
                        try:
                            parsed = json.loads(raw_data)
                            self.send(parsed, filename=f"{source_id}-{job['domain']}.json")
                        except Exception:
                            self.send(raw_data.encode("utf-8"), filename=f"{source_id}-{job['domain']}.json", mime="application/json")
                    else:
                        self.send({"error": f"{source_id} için ham rapor bulunamadı."}, 404)
                    return

            self.send({"error": "Kayıt bulunamadı."}, 404)

        def do_POST(self):
            if not self.allowed():
                return
            if self.path != "/api/scans":
                self.send({"error": "Endpoint bulunamadı."}, 404)
                return

            if self.headers.get("X-CSRF-Token") != token:
                self.send({"error": "Tarama tokenı geçersiz."}, 403)
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.close_connection = True
                    self.send({"error": "JSON gövdesi geçersiz veya çok büyük."}, 400)
                    return
                data = json.loads(self.rfile.read(length))
                domain = normalize_domain(data.get("domain"))
            except (ValueError, TypeError, AttributeError) as error:
                self.send({"error": str(error) or "Geçersiz domain."}, 400)
                return

            # Create job in database
            job_id = storage.create_job(domain)

            # Start real live scan in background thread
            def run_scan_worker(target_domain: str, j_id: str):
                thread_storage = Storage(Config.DB_PATH, recover_on_startup=False)
                try:
                    ora = OraAdapter()
                    oss = AgentReadyOSSAdapter()
                    orchestrator = Orchestrator(adapters=[ora, oss], storage=thread_storage)
                    asyncio.run(orchestrator.run_scan(target_domain, j_id))
                except Exception as ex:
                    thread_storage.update_job_status(j_id, "failed", error=str(ex))
                finally:
                    thread_storage.close()

            worker = threading.Thread(
                target=run_scan_worker,
                args=(domain, job_id),
                daemon=True,
                name=f"scan-{job_id[:8]}",
            )
            worker.start()

            self.send({"job_id": job_id}, 202)

        def log_message(self, format, *args):
            # Clean logging
            sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}\n")

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def main():
    parser = argparse.ArgumentParser(description="Pickable Canlı Web Sunucusu")
    parser.add_argument("--port", type=int, default=8765, help="Port numarası (varsayılan: 8765)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (varsayılan: 127.0.0.1)")
    args = parser.parse_args()

    server = make_live_server(port=args.port, host=args.host)
    print(f"CANLI MOD — http://{args.host}:{server.server_port} — Ora ve AgentReady OSS paralel çalışıyor", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu kapatılıyor...")
        server.server_close()


if __name__ == "__main__":
    main()
