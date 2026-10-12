# Pickable

Pickable analyzes public websites using Ora, Cloudflare and marketing checks.
Its public demo is [solveny.dev](https://solveny.dev); the demo domain is not the
product name. Reports preserve source evidence and uncertainty.

Multi-source Agent Readiness Reporting backend core in pure Python standard library (3.12+).

Probes domain readiness for AI agents via the Ora API, with Cloudflare's isitagentready.com as a per-check fallback, stores verbatim raw reports and normalized checks in SQLite, and provides resilient SSRF protection and parallel execution.

---

## Features

- **Standard Library Only**: No external dependencies (`requests`, `httpx`, `flask`, ORMs are not used).
- **Hardened SSRF Protection & IP Pinning (`app/safe_fetch.py`)**:
  - Blocks RFC 1918 private IPv4 & IPv6 ranges, loopback, link-local, and cloud metadata (`169.254.169.254`).
  - Protects against DNS rebinding by resolving once and pinning the socket to the verified public IP.
  - Validates HTTPS SNI and TLS certificates against the pinned destination.
  - Follows redirects securely up to a maximum limit (default 5) and stops redirect-to-private attacks.
  - Enforces response size ceilings (10MB) and strict timeouts.
- **SQLite Storage & Recovery (`app/storage.py`)**:
  - Automatically recovers from process crashes on startup by marking `pending`/`running` jobs as `interrupted`.
  - Persists provider JSON internally alongside normalized checks; customer APIs redact evidence and do not expose raw reports.
- **SaaS product intelligence (`app/intelligence.py`)**:
  - After a readiness scan, fetches the submitted URL plus a few discovered same-origin pages, then runs the `saas-site-intelligence` skill through local Ollama when no hosted key is configured.
  - The report shows source links and explicit unknowns. Local deep analysis expands the page analysis but does not search the web; hosted OpenAI mode can search the web, with competitor findings requiring source review.
- **Fault-Tolerant Parallel Orchestration (`app/orchestrator.py`)**:
  - Marketing playbook (`app/adapters/growth.py`): social profiles linked from the home page and blog presence, recency and cadence (RSS/Atom feed, falling back to sitemap dates). The UI has a Normal (marketing) / Agent (readiness) toggle.
  - Runs Ora and Cloudflare in parallel with per-source fault isolation; the UI shows a partial report as soon as the first source lands.
- **Non-Destructive Status Normalization**:
  - Preserves `UNKNOWN`, `NA`, and `WARNING` states without coercing them to `FAIL`.
- **Early-access waitlist (`app/waitlist.py`)**:
  - Accessible CTA under the scan form and inside scan reports opens a native dialog to capture an email (plus optional website and source).
  - Server-side validation, email lowercasing and deduplication, a honeypot field that silently drops automated submissions, and per-peer/global rate limits. A repeat signup returns a friendly "already on the list" confirmation rather than an error.
  - First-party funnel events (`/api/events`) are recorded without any third-party tracker; signup outcomes are counted server-side so a retry never double-counts.

---

## Directory Structure

```
app/
  __init__.py
  config.py              # Environment variables and defaults
  safe_fetch.py          # SSRF-protected HTTP client with DNS pinning
  intelligence.py       # skill-guided page fetch and local Ollama / hosted analysis
  skills/saas-site-intelligence.md
  storage.py             # SQLite schema and operations
  adapters/
    __init__.py
    base.py              # Adapter ABC & CheckRecord dataclass
    ora.py               # Ora API adapter (with rate limiter & polling)
    cloudflare.py        # isitagentready.com fallback adapter
    growth.py            # marketing playbook: socials + blog checks
  orchestrator.py        # Concurrent adapter orchestration & fault isolation
tests/
  test_safe_fetch.py
  test_storage.py
  test_orchestrator.py
  test_adapters.py
  test_core.py
  fixtures/
    ora_sample.json
examples/
  randevuo-ora.json      # Sample live Ora output
docs/
  DECISIONS.md           # Architecture decision records
run.py                   # CLI entry point
```

---

## Requirements & Installation

- **Python**: 3.12 or newer.
- No third-party packages required!
- Optional: install `coverage` if you want to inspect test coverage:
  ```bash
  pip install -r requirements.txt
  ```

---

## Running Tests

All tests run via the Python standard library `unittest` runner:

```bash
python3 -m unittest discover tests/ -v
```

---

## Web UI

```bash
python3 server.py            # live Ora scans → http://127.0.0.1:8765
python3 server.py --demo     # synthetic fixture data, no network
```

Env: `PORT`, `BIND` (use `0.0.0.0` on a host), `ALLOWED_HOSTS` (comma-separated public host names, e.g. `pickable.example.com`), `DB_PATH`, `ORA_API_KEY`, `OPENAI_API_KEY` (optional hosted SaaS analysis), `OPENAI_QUICK_MODEL` (default `gpt-6-luna`), `OPENAI_DEEP_MODEL` (default `gpt-6.1-sol`), legacy `OPENAI_MODEL` (quick-model fallback unless `OPENAI_QUICK_MODEL` is set), `INTELLIGENCE_PROVIDER` (`ollama` or `openai`), `OLLAMA_MODEL` (default `qwen3:8b`), and `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434` for local Python; Docker Compose defaults to `http://host.docker.internal:11434` and adds the host-gateway mapping). Short analysis uses the quick model with no reasoning and no web search, and fetches at most one additional same-site page; deep analysis uses the deep model with low reasoning, at most three additional pages, and caps web search at two tool calls. With no OpenAI key, Pickable uses Ollama without a hosted API account or key. For a deployed Pickable server, Ollama must run on that host or be reachable over a private network; set `OLLAMA_BASE_URL` to its HTTP(S) origin, such as `http://ollama:11434` on a shared container network. Do not expose an unauthenticated Ollama endpoint to the public internet. Keys never return to the browser.

## CLI Usage

### 1. Scan a Domain

```bash
python3 run.py scan randevuo.com.tr
```

Example Output:
```json
{
  "job_id": "b9a4e46a-9f61-4a74-80b4-d1bd58485fe4",
  "domain": "randevuo.com.tr",
  "status": "completed"
}
```

### 2. View Saved Reports for a Job

```bash
python3 run.py show b9a4e46a-9f61-4a74-80b4-d1bd58485fe4
```

### 3. Inspect Database Directly

```bash
sqlite3 data/scan.db "SELECT id, domain, status, started_at, finished_at FROM jobs;"
sqlite3 data/scan.db "SELECT id, source_id, status, error FROM source_runs;"
sqlite3 data/scan.db "SELECT count(*) FROM checks;"
```

## Agent interfaces

The server serves `/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/llms-full.txt`,
`/openapi.json`, `/docs`, `/api`, `/auth.md`, `/skill.md`, Markdown page twins,
and discovery catalogs under `/.well-known/`. HTML pages support
`Accept: text/markdown`; `/?mode=agent` provides the agent onboarding view.
See [the implementation report](AGENT_READINESS_REPORT.md) and
[remaining actions](AGENT_READINESS_PLAN.md) for validation evidence.

`POST /mcp` provides stateless Streamable HTTP with JSON replies. Tools
describe Pickable, list scans for the caller’s direct peer IP and retrieve a scoped, redacted report. Initialize
first with protocol `2025-06-18` or `2025-03-26`, send the initialized notification,
then discover tools. Clients send `Accept: application/json, text/event-stream`
and the negotiated `MCP-Protocol-Version` header on subsequent requests.
GET streaming and sessions are not implemented.

The existing UI/REST scan flow obtains `X-CSRF-Token` from `/api/config` before
`POST /api/scans`. An operator-issued bearer key is an optional alternative.
This token protects against cross-site requests. Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. Never submit secrets or private targets.

POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, and private/reserved destinations are rejected. Authentication query parameters are rejected; other query parameters are preserved and fragments are dropped. Domain input also rejects paths and ports.

Local intelligence remains available through GET `/api/scans/{job_id}/intelligence`
and GET `/api/scans/{job_id}/intelligence/deep`. POST `/api/scans/{job_id}/intelligence`
starts deep research and requires CSRF; bearer credentials cannot replace it.
MCP scan creation remains domain-only. The bearer key does not bypass read scope.

MCP scan creation is disabled by default. To enable it, set both
`PICKABLE_MCP_SCAN_ENABLED=1` and `PICKABLE_AGENT_API_KEY` to a secret of at least
32 ASCII characters. Callers must supply `Authorization: Bearer <key>` for the
scan tool. Configure secrets through the deployment environment; server.py does
not load `.env` files. The key can be rotated or removed and the service restarted
to revoke access. No OAuth identity or per-user privacy is provided.

All REST/MCP creation requests share limits: five scans per peer IP per hour and
30 per process per hour by default. Configure `PICKABLE_SCANS_PER_HOUR` and
`PICKABLE_GLOBAL_SCANS_PER_HOUR`. Proxy headers are not trusted, so clients behind
one proxy may share the peer budget. Limits are in memory and reset on restart;
multiple replicas require an external shared limiter. The scan queue holds at
most 20 waiting jobs. The server limits request bodies, read timeouts and concurrent
handlers; preview results are cached with bounded storage.

Run a local check without consuming provider quota:

```bash
PICKABLE_PUBLIC_URL=http://127.0.0.1:8765 python3 server.py --demo
# In another terminal:
python3 scripts/verify_live.py --url http://127.0.0.1:8765
```

Docker deployment disables product analysis by default (`INTELLIGENCE_PROVIDER=unavailable`) and does not pass OpenAI credentials into the container. Marketing and agent-readiness scans remain available. To use a local model later, explicitly configure `INTELLIGENCE_PROVIDER=ollama` and a reachable Ollama service.

For Docker deployment on the configured host, `docker compose up -d --build`
uses the existing Traefik network and domain routing. `render.yaml` serves the
same application with `/api/health` as its health check. Set
`PICKABLE_PUBLIC_URL` to the public HTTPS origin and explicitly allow its Host
header using `ALLOWED_HOSTS`. Serve all updated `app/`, `web/`, and server files
together. Check the deployed endpoints with:

```bash
python3 scripts/verify_live.py --url https://solveny.dev
```

The verifier starts no scans and does not trigger Ora rescans. Observe the Ora
score separately after deployment; a passing local suite does not establish a
public score change.

If sites on the same VPS time out only from the Pickable container, check whether
the origin firewall permits only Cloudflare traffic. A verified fix is an
interface-bound rule allowing only the container's source IP (`/32`) to the
VPS's public IP on TCP 443. Do not allow the shared Docker subnet or unrestricted
public ingress; keep SSRF protections enabled. Verify the container IP and bridge
interface after rebuilding, and update that narrow rule if either changes.
Required homepage fetch errors are preserved so transient marketing-analysis
timeouts can retry, and saved reports show terminal intelligence results immediately.
