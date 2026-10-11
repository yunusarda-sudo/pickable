# Pickable REST and MCP API

OpenAPI document: https://solveny.dev/openapi.json

## Create a REST scan

1. GET /api/config and read its csrf_token.
2. POST /api/scans with Content-Type: application/json, X-CSRF-Token, and exactly {"domain":"example.com"}. The domain may be bare or an HTTP(S) root URL; ports, credentials, non-root paths, query strings, fragments, IP literals, and private or reserved destinations are rejected.
3. Read the returned job_id and poll GET /api/scans/{job_id}.

An operator-issued bearer key set in PICKABLE_AGENT_API_KEY is an alternative for scan creation. The CSRF token and bearer key do not restrict the public read endpoints.

## Public reads

- GET /api/scans lists public scan history.
- GET /api/scans/{job_id} reads state and may include a partial report while sources run.
- GET /api/scans/{job_id}/report returns the report when available; otherwise it returns 404.
- GET /api/scans/{job_id}/raw/{source} reads original source JSON when available.
- GET /api/scans/{job_id}/preview returns preview metadata.
- GET /api/config returns the process CSRF token and mode.
- GET /api/health returns a health indication.

Stored scan history, statuses, reports, raw outputs, and previews are public. Do not submit private URLs, credentials, or confidential details.

## Errors and request limits

Scan requests are JSON objects containing only a domain string and are limited to 2,048 bytes. Missing or malformed Content-Length and invalid JSON or input return 400. Disallowed Host or Origin and invalid CSRF/bearer credentials return 403. A body read timeout returns 408, an oversized body 413, and a non-JSON content type 415. Queue/rate limits return 429 with Retry-After; backend unavailability returns 503.

One worker handles scans serially with at most 20 waiting jobs. Default rate limits are 5 accepted scans per peer per hour and 30 across the server per hour. The operator can change them with PICKABLE_SCANS_PER_HOUR and PICKABLE_GLOBAL_SCANS_PER_HOUR.

## MCP

POST JSON-RPC 2.0 to /mcp with Accept: application/json, text/event-stream and Content-Type: application/json. Pickable returns single-message JSON and does not support GET/SSE. Supported versions are 2025-03-26 and 2025-06-18. See https://solveny.dev/docs/agent-scan for initialization and tool details.

An unauthorized enabled scan-start call returns HTTP 401. After authorization, scan business failures are HTTP 200 tool results with isError set to true and an error code in content; successful 2025-06-18 calls also include structuredContent.

Source: https://github.com/ozgureyilmaz/pickable
