# Architectural Decision Records (ADRs)

## 1. Python Standard Library Only (No External Dependencies)
- **Decision**: No third-party networking libraries (`requests`, `httpx`, `aiohttp`, `urllib3`) or web frameworks (`flask`, `fastapi`). Use only standard library modules (`http.client`, `socket`, `ssl`, `ipaddress`, `sqlite3`, `asyncio`, `subprocess`, `unittest`).
- **Rationale**: Eliminates dependency supply-chain risks, avoids installation overhead, ensures reproducibility across standard Python 3.12+ environments, and gives exact, granular control over socket connections for SSRF and DNS pinning protections.

## 2. SSRF Protection & DNS Pinning (`safe_fetch.py`)
- **Decision**: Perform single DNS resolution via `socket.getaddrinfo`, inspect every resolved IP against private, loopback, link-local, multicast, and cloud metadata (169.254.169.254) addresses, and strictly pin the connection to the verified IP while enforcing proper SNI and certificate validation.
- **Rationale**: Standard HTTP libraries automatically re-resolve DNS or follow redirects blindly, making them vulnerable to DNS rebinding (TOCTOU) and internal intranet egress attacks. Explicit socket pinning guarantees that the network packet goes only to the vetted public IP.

## 3. SQLite Storage with Startup Recovery (`storage.py`)
- **Decision**: Use Python's built-in `sqlite3` without ORMs. Store raw reports verbatim (`raw_report TEXT`) alongside normalized checks. On startup, run:
  `UPDATE jobs SET status='interrupted' WHERE status IN ('pending', 'running')`.
- **Rationale**: Zero setup database that is fully file-based and portable. The startup update prevents orphaned zombie states if the application crashes or restarts mid-scan. Verbatim raw report retention ensures evidence is never lost or mutated.

## 4. Fault-Tolerant Parallel Orchestration (`orchestrator.py`)
- **Decision**: Execute adapters concurrently using `asyncio.gather(..., return_exceptions=True)`. Wrap each source run in isolated try-except blocks.
- **Rationale**: Different scanning providers have different network latencies and failure rates (e.g. rate limits, remote downtime). A failure or timeout in Ora must never fail the OSS scanner run, and vice-versa. Partial scan data is always preserved.

## 5. Non-Destructive Status Mapping (Evidence Preservation)
- **Decision**: Never convert `UNKNOWN`, `NA`, `NOT_APPLICABLE`, or `WARNING` statuses to `FAIL`.
- **Rationale**: Honesty in reporting. An unscanned check or an inconclusive result must never penalize a site as a confirmed failure.
