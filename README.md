# Agent Readiness Backend (Part 1)

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
  - Persists verbatim raw JSON reports alongside normalized granular checks.
- **Fault-Tolerant Parallel Orchestration (`app/orchestrator.py`)**:
  - Marketing playbook (`app/adapters/growth.py`): social profiles linked from the home page and blog presence, recency and cadence (RSS/Atom feed, falling back to sitemap dates). The UI has a Normal (marketing) / Agent (readiness) toggle.
  - Runs Ora and Cloudflare in parallel with per-source fault isolation; the UI shows a partial report as soon as the first source lands.
- **Non-Destructive Status Normalization**:
  - Preserves `UNKNOWN`, `NA`, and `WARNING` states without coercing them to `FAIL`.

---

## Directory Structure

```
app/
  __init__.py
  config.py              # Environment variables and defaults
  safe_fetch.py          # SSRF-protected HTTP client with DNS pinning
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

Env: `PORT`, `BIND` (use `0.0.0.0` on a host), `ALLOWED_HOSTS` (comma-separated public host names, e.g. `pickable.example.com`), `DB_PATH`, `ORA_API_KEY`.

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
