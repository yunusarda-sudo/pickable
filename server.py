"""Pickable web server. Live mode scans via Ora; --demo serves synthetic fixtures."""
import argparse
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.agent_policy import AgentPolicy, RequestError, ScanLimiter, ScanRateLimitError
from app.backends import MAX_QUEUED, demo_backend, live_backend
from app.edge import (
    DEFAULT_TRUSTED_PROXY_CIDRS,
    _came_via_cloudflare,
    _parse_trusted_proxies,
    _peer_is_trusted,
    _real_client_ip,
)
from app.pickable_agent_routes import serve_agent_get, serve_agent_post
from app.presenters import public_intelligence, public_job, public_report
from app.security import normalize_domain, normalize_site_url
from app.waitlist import RateLimiter, RateLimitError, honeypot_filled, parse_event, parse_signup

# Re-exported for backward compatibility (tests import these from server).
__all__ = [
    "BoundedHTTPServer", "make_server", "serve",
    "demo_backend", "live_backend",
    "public_report", "public_job", "public_intelligence",
    "MAX_QUEUED",
    "DEFAULT_TRUSTED_PROXY_CIDRS",
    "_parse_trusted_proxies", "_peer_is_trusted",
    "_real_client_ip", "_came_via_cloudflare",
]

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


def make_server(port, backend, bind="127.0.0.1", allowed_hosts=(), policy=None):
    token = secrets.token_urlsafe(32)
    policy = policy or AgentPolicy.from_env()
    limiter = ScanLimiter(policy)
    deep_limiter = ScanLimiter(AgentPolicy(scans_per_hour=10, global_scans_per_hour=30))
    waitlist_limiter = RateLimiter(per_peer=10, per_hour=60, message="Too many waitlist signups from this address. Please try again later.")
    event_limiter = RateLimiter(per_peer=120, per_hour=1000, message="Too many requests from this address. Please try again later.")
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

        def _record_event(self, name, source):
            # Signup outcome logging is best-effort: a storage hiccup must never
            # turn an otherwise successful signup into an error response.
            recorder = backend.get("record_event")
            if recorder is None:
                return
            try:
                recorder(name, source)
            except Exception:
                pass

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
            methods = "POST" if path in ("/mcp", "/api/waitlist", "/api/events") else "GET, HEAD, POST" if path == "/api/scans" else "GET, HEAD"
            self.send(b"", 204, "text/plain; charset=utf-8", headers={"Allow": methods})

        def do_POST(self):
            if not self.allowed(): return
            path = urlsplit(self.path).path
            if path == "/api/waitlist":
                if self.headers.get("X-CSRF-Token") != token:
                    self.send({"error": "Invalid waitlist token.", "code": "invalid_csrf_token"}, 403); return
                signup = backend.get("waitlist")
                if signup is None:
                    self.send({"error": "The waitlist is not available right now.", "code": "waitlist_unavailable"}, 503); return
                try:
                    data = self.read_json(2048)
                except RequestError as error:
                    self.close_connection = True
                    self.send({"error": error.message, "code": error.code}, error.status); return
                if not isinstance(data, dict):
                    self.send({"error": "Send a JSON object with an email address.", "code": "invalid_signup"}, 400); return
                if honeypot_filled(data):
                    # Automated submissions get the normal success shape and are
                    # never stored; a human never fills the hidden field.
                    self.send({"status": "created"}, 201); return
                try:
                    email, website, source = parse_signup(data)
                except ValueError as error:
                    # The parse failed, so there is no reliable source to attribute
                    # this to; fall back to the landing CTA for the failure row.
                    self._record_event("waitlist_signup_failed", "landing_page")
                    self.send({"error": str(error), "code": "invalid_signup"}, 400); return
                try:
                    waitlist_limiter.admit(self.client_ip)
                except RateLimitError as error:
                    self._record_event("waitlist_signup_failed", source)
                    self.send({"error": error.message, "code": "waitlist_rate_limited", "retry_after": error.retry_after}, 429,
                              headers={"Retry-After": error.retry_after}); return
                try:
                    created = signup(email, website, source)
                except Exception:
                    self._record_event("waitlist_signup_failed", source)
                    self.send({"error": "The waitlist is unavailable right now. Please try again shortly.", "code": "waitlist_unavailable"}, 503); return
                if created:
                    self._record_event("waitlist_signup_succeeded", source)
                    self.send({"status": "created", "message": "You are on the list. We will be in touch when early access opens."}, 201)
                else:
                    # The submitter already controls this address, so confirming
                    # it is already listed reveals nothing they did not know.
                    self.send({"status": "already_registered", "message": "You are already on the waitlist. We will be in touch."}, 200)
                return
            if path == "/api/events":
                if self.headers.get("X-CSRF-Token") != token:
                    self.send({"error": "Invalid event token.", "code": "invalid_csrf_token"}, 403); return
                recorder = backend.get("record_event")
                if recorder is None:
                    self.send({"error": "Analytics are not available right now.", "code": "analytics_unavailable"}, 503); return
                try:
                    data = self.read_json(512)
                except RequestError as error:
                    self.close_connection = True
                    self.send({"error": error.message, "code": error.code}, error.status); return
                try:
                    event_limiter.admit(self.client_ip)
                except RateLimitError as error:
                    self.send({"error": error.message, "code": "event_rate_limited", "retry_after": error.retry_after}, 429,
                              headers={"Retry-After": error.retry_after}); return
                try:
                    name, source = parse_event(data)
                except ValueError as error:
                    self.send({"error": str(error), "code": "invalid_event"}, 400); return
                try:
                    recorder(name, source)
                except Exception:
                    self.send({"error": "Analytics are unavailable right now.", "code": "analytics_unavailable"}, 503); return
                self.send({"ok": True}); return
            if serve_agent_post(self, scoped_backend(self.client_ip), self.path, policy, create_scan): return
            parts = path.strip("/").split("/")
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
