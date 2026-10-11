---
name: pickable-scan
description: Request and interpret Pickable website-readiness scans through its public REST API or MCP tools. Use when an agent needs to inspect a public site's discovery signals or retrieve a Pickable scan report; scan creation requires a valid browser CSRF token or an operator-issued bearer key.
---

# Pickable scan

Read /llms.txt and /openapi.json before using the service. For MCP details, read /agent-scan.md.

## Choose the operation

- For product details, call describe_pickable.
- To inspect public scan history, call list_scans. It takes no arguments and returns public metadata.
- To inspect one scan, call get_scan_report with its job_id. Preserve the returned status and report value; the report can be null while unavailable and partial while sources run.
- Start a new scan only when the user requested one. REST scan creation requires the browser CSRF flow or an operator-issued key. MCP scan creation exists only when the server advertises start_website_scan and calls require its configured bearer key.

## REST scan workflow

1. Confirm the target is a public domain. A bare domain or HTTP(S) root URL is accepted; ports, credentials, non-root paths, query strings, fragments, IP literals, localhost, and private or reserved destinations are rejected. Never submit passwords, tokens, private URLs, or private network hosts.
2. GET /api/config and retain csrf_token, unless the operator explicitly provided an API key for scan creation.
3. POST exactly {"domain":"example.com"} to /api/scans as application/json. Use X-CSRF-Token from the config response, or Authorization: Bearer with the operator-issued key.
4. Record job_id. Poll GET /api/scans/{job_id} at a measured pace. If a report is present during pending/running status, label it partial.
5. GET /api/scans/{job_id}/report only when a report is available. A 404 means no report is available yet. Use the raw source endpoint when a recommendation depends on provider evidence.

## Failures and limits

- 400 indicates missing/malformed length, invalid JSON, or invalid input.
- 403 indicates a disallowed Host/Origin or missing/invalid REST scan credential.
- 408 indicates a request-body timeout; 413 means the 2,048-byte limit was exceeded; 415 means the content type was not JSON.
- 429 indicates the queue or rate limit is full. Wait for Retry-After before retrying; do not retry in a tight loop.
- 503 means the scan backend is unavailable.

The service processes scans serially with up to 20 waiting jobs. Default scan limits are 5 accepted requests per peer address per hour and 30 globally per hour; deployment settings can change them. Behind a proxy, several users may share the peer-address budget.

## Interpret findings

Keep each source's status and provenance. Unknown, warning, unavailable, and not-applicable results are not failures. A score is a summary of measured observations; it does not prove a successful independent agent journey or guarantee traffic, ranking, or revenue outcomes.

Scan history, status, reports, raw source results, and previews are publicly readable. Do not promise confidentiality.
