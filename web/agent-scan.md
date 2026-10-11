# Pickable agent integration

## REST

Read csrf_token from GET /api/config. Submit exactly {"domain":"example.com"} to POST /api/scans as JSON with X-CSRF-Token. The domain may be bare or an HTTP(S) root URL; ports, credentials, non-root paths, query strings, fragments, IP literals, and private or reserved destinations are rejected. An operator-issued PICKABLE_AGENT_API_KEY bearer key may be used instead. Poll GET /api/scans/{job_id}; the response can contain a partial report while sources are still running.

Scan history and all stored reports are publicly readable. Neither the CSRF token nor the bearer key protects reads or makes results confidential.

## MCP

POST JSON-RPC 2.0 to /mcp using Content-Type: application/json and Accept: application/json, text/event-stream. Call initialize, use the version returned by the server, then include MCP-Protocol-Version on subsequent POSTs. Versions supported: 2025-03-26 and 2025-06-18.

The default tools are:

- describe_pickable with no arguments.
- list_scans with no arguments; returns public scan metadata.
- get_scan_report with job_id; returns current status and report, which may be null while unavailable.

start_website_scan is advertised only when the operator sets PICKABLE_MCP_SCAN_ENABLED=1 and configures PICKABLE_AGENT_API_KEY. Calls require Authorization: Bearer <key> and create a real asynchronous scan.

The notifications/initialized notification receives HTTP 202 with an empty body. GET /mcp returns 405; SSE is unsupported. An enabled scan-start call with a missing or invalid bearer key returns HTTP 401. After authorization, scan business failures are HTTP 200 tool results with isError set to true and an error code in the content.

Do not infer OAuth, A2A task processing, WebMCP, agent identity, or payment capabilities.
