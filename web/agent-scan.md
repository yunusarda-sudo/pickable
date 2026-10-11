# Pickable agent integration

## REST

POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, and private/reserved destinations are rejected. Authentication query parameters are rejected; other query parameters are preserved and fragments are dropped. Domain input also rejects paths and ports. Send Content-Type: application/json and X-CSRF-Token from GET /api/config, or the configured bearer key. Poll GET /api/scans/{job_id}.

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. The bearer key does not bypass this read scope.

## MCP

POST JSON-RPC 2.0 to /mcp using Content-Type: application/json and Accept: application/json, text/event-stream. Call initialize, use the version returned by the server, then include MCP-Protocol-Version on subsequent POSTs. Versions supported: 2025-03-26 and 2025-06-18.

The default tools are:

- describe_pickable with no arguments.
- list_scans with no arguments; returns scan metadata scoped to the direct peer IP.
- get_scan_report with job_id; returns current status and report, which may be null while unavailable.

start_website_scan is advertised only when the operator sets PICKABLE_MCP_SCAN_ENABLED=1 and configures PICKABLE_AGENT_API_KEY. Calls require Authorization: Bearer <key> and create a real asynchronous scan.

The notifications/initialized notification receives HTTP 202 with an empty body. GET /mcp returns 405; SSE is unsupported. An enabled scan-start call with a missing or invalid bearer key returns HTTP 401. After authorization, scan business failures are HTTP 200 tool results with isError set to true and an error code in the content.

Do not infer OAuth, A2A task processing, WebMCP, agent identity, or payment capabilities.

## Local product intelligence

The local intelligence feature remains available: GET /api/scans/{job_id}/intelligence and GET /api/scans/{job_id}/intelligence/deep return scoped analysis. POST /api/scans/{job_id}/intelligence starts deep research and requires X-CSRF-Token; bearer credentials cannot replace that token. Intelligence is not an MCP tool or evidence of hosted availability.

MCP start_website_scan accepts domain only (bare public domain or HTTP(S) root URL). It does not accept the REST url field. MCP list_scans and get_scan_report use the same direct peer-IP scope; a bearer key does not bypass it.
