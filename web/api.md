# Pickable REST and MCP API

OpenAPI document: https://solveny.dev/openapi.json

## Create a REST scan

1. GET /api/config and read its csrf_token.
2. POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, and private/reserved destinations are rejected. Authentication query parameters are rejected; other query parameters are preserved and fragments are dropped. Domain input also rejects paths and ports. Send Content-Type: application/json and X-CSRF-Token from GET /api/config, or the configured bearer key. Poll GET /api/scans/{job_id}.
3. Read the returned job_id and poll GET /api/scans/{job_id}.

An operator-issued bearer key set in PICKABLE_AGENT_API_KEY is an alternative for scan creation. The CSRF token is not identity; the bearer key does not bypass the peer-IP read scope.

## Scoped reads

- GET /api/scans lists scan history for the direct peer IP.
- GET /api/scans/{job_id} reads state and may include a partial report while sources run.
- GET /api/scans/{job_id}/report returns the report when available; otherwise it returns 404.
- Raw-source routes are unavailable and return 404.
- GET /api/scans/{job_id}/preview returns preview metadata.
- GET /api/config returns the process CSRF token and mode.
- GET /api/health returns a health indication.

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404.

## Errors and request limits

Scan requests contain exactly one domain or url string and are limited to 2,048 bytes. Missing or malformed Content-Length and invalid JSON or input return 400. Disallowed Host or Origin and invalid CSRF/bearer credentials return 403. A body read timeout returns 408, an oversized body 413, and a non-JSON content type 415. Queue/rate limits return 429 with Retry-After; backend unavailability returns 503.

One worker handles scans serially with at most 20 waiting jobs. Default rate limits are 5 accepted scans per peer per hour and 30 across the server per hour. The operator can change them with PICKABLE_SCANS_PER_HOUR and PICKABLE_GLOBAL_SCANS_PER_HOUR.

## MCP

POST JSON-RPC 2.0 to /mcp with Accept: application/json, text/event-stream and Content-Type: application/json. Pickable returns single-message JSON and does not support GET/SSE. Supported versions are 2025-03-26 and 2025-06-18. See https://solveny.dev/docs/agent-scan for initialization and tool details.

An unauthorized enabled scan-start call returns HTTP 401. After authorization, scan business failures are HTTP 200 tool results with isError set to true and an error code in content; successful 2025-06-18 calls also include structuredContent.


## Local product intelligence

The local intelligence feature remains available: GET /api/scans/{job_id}/intelligence and GET /api/scans/{job_id}/intelligence/deep return scoped analysis. POST /api/scans/{job_id}/intelligence starts deep research and requires X-CSRF-Token; bearer credentials cannot replace that token. Intelligence is not an MCP tool or evidence of hosted availability.
