# Pickable documentation

## Run a scan

1. Enter a public domain on https://solveny.dev/.
2. Pickable queues an asynchronous scan and polls its sources.
3. Inspect the normalized findings before applying changes to the audited site. Provider evidence is redacted; raw reports are unavailable.

The status response can include a partial report while sources are running. A report download returns 404 when no report is available yet. Unknown, warning, and not-applicable checks are not equivalent to failure.

## Queue and rate limits

One worker processes scans at a time and accepts up to 20 waiting scans. The default scan rate is 5 accepted scans per peer address per hour and 30 across the server per hour. Operators can tune these limits using PICKABLE_SCANS_PER_HOUR and PICKABLE_GLOBAL_SCANS_PER_HOUR. A 429 response includes Retry-After; wait at least that long before retrying.

The limiter is in memory and resets on restart. Behind a proxy, the per-peer limit may be shared by multiple visitors because forwarded-IP headers are not trusted.

## REST API

- Read csrf_token and mode: GET https://solveny.dev/api/config
- POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, and private/reserved destinations are rejected. Authentication query parameters are rejected; other query parameters are preserved and fragments are dropped. Domain input also rejects paths and ports. Send Content-Type: application/json and X-CSRF-Token from GET /api/config, or the configured bearer key. Poll GET /api/scans/{job_id}.
- Poll status: GET https://solveny.dev/api/scans/{job_id}
- Read a report: GET https://solveny.dev/api/scans/{job_id}/report
- OpenAPI 3.1: https://solveny.dev/openapi.json

An operator-issued bearer key configured as PICKABLE_AGENT_API_KEY can replace the CSRF header for scan creation. This is not user identity and does not bypass the peer-IP scope. Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404.

## MCP

Use JSON-RPC 2.0 with POST /mcp. See https://solveny.dev/docs/agent-scan for protocol-version negotiation, request headers, and tool names. By default, MCP exposes describe_pickable, list_scans, and get_scan_report. The optional start_website_scan tool requires operator configuration and a bearer key.

## Limitations

Scores normalize available source observations. They are not independent verification of a real-agent journey and do not guarantee traffic, rankings, purchases, or growth.

## Local product intelligence

The local intelligence feature remains available: GET /api/scans/{job_id}/intelligence and GET /api/scans/{job_id}/intelligence/deep return scoped analysis. POST /api/scans/{job_id}/intelligence starts deep research and requires X-CSRF-Token; bearer credentials cannot replace that token. Intelligence is not an MCP tool or evidence of hosted availability.
