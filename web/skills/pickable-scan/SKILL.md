---
name: pickable-scan
description: Request and interpret Pickable website-readiness scans through its public REST API or MCP tools. Use when an agent needs to inspect a public site's discovery signals or retrieve a Pickable scan report; scan creation requires a valid browser CSRF token or an operator-issued bearer key.
---

# Pickable scan

Read /llms.txt and /openapi.json before using the service. For MCP details, read /agent-scan.md.

## Choose the operation

- For product details, call describe_pickable.
- To inspect scan history for your direct peer IP, call list_scans. It takes no arguments.
- To inspect one scan, call get_scan_report with its job_id. Preserve the returned status and report value; the report can be null while unavailable and partial while sources run.
- Start a new scan only when the user requested one. REST scan creation requires the browser CSRF flow or an operator-issued key. MCP scan creation exists only when the server advertises start_website_scan and calls require its configured bearer key.

## REST scan workflow

1. Confirm the target is public. Use domain for a bare domain/root URL or url for an exact URL path; do not combine them. Ports, credentials, authentication query parameters, IP literals, localhost, and private/reserved destinations are rejected. Other query parameters are preserved and fragments are dropped. Never submit passwords, tokens, private URLs, or private network hosts.
2. GET /api/config and retain csrf_token, unless the operator explicitly provided an API key for scan creation.
3. POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, and private/reserved destinations are rejected. Authentication query parameters are rejected; other query parameters are preserved and fragments are dropped. Domain input also rejects paths and ports. Send Content-Type: application/json and X-CSRF-Token from GET /api/config, or the configured bearer key. Poll GET /api/scans/{job_id}.
4. Record job_id. Poll GET /api/scans/{job_id} at a measured pace. If a report is present during pending/running status, label it partial.
5. GET /api/scans/{job_id}/report only when a report is available. A 404 means the scan is outside your peer-IP scope or no report is available yet. Provider evidence is redacted; raw-source routes return 404.

## Failures and limits

- 400 indicates missing/malformed length, invalid JSON, or invalid input.
- 403 indicates a disallowed Host/Origin or missing/invalid REST scan credential.
- 408 indicates a request-body timeout; 413 means the 2,048-byte limit was exceeded; 415 means the content type was not JSON.
- 429 indicates the queue or rate limit is full. Wait for Retry-After before retrying; do not retry in a tight loop.
- 503 means the scan backend is unavailable.

The service processes scans serially with up to 20 waiting jobs. Default scan limits are 5 accepted requests per peer address per hour and 30 globally per hour; deployment settings can change them. Behind a proxy, several users may share the peer-address budget.

## Interpret findings

Keep each source's status and provenance. Unknown, warning, unavailable, and not-applicable results are not failures. A score is a summary of measured observations; it does not prove a successful independent agent journey or guarantee traffic, ranking, or revenue outcomes.

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404.

MCP start_website_scan accepts domain only (bare public domain or HTTP(S) root URL). It does not accept the REST url field. MCP list_scans and get_scan_report use the same direct peer-IP scope; a bearer key does not bypass it.
