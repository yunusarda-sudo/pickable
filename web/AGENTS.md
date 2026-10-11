# Pickable public agent guide

Pickable is a public website-analysis demo. It reports external agent-readiness observations and basic marketing checks; its score does not prove task completion or guarantee growth.

## Start here

- Read /llms.txt for the concise map.
- Read /openapi.json for HTTP request/response schemas.
- Read /docs/agent-scan for REST and MCP usage.
- Read /auth.md for scan-creation credential behavior.
- Read /skills/pickable-scan/SKILL.md for the scan workflow.

## Data and capabilities

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. Do not submit credentials or secrets. A robots rule is crawl guidance only.

MCP defaults to describe_pickable, list_scans, and get_scan_report. start_website_scan is available only if the operator enables it and configures a bearer key. Check tools/list at runtime.

The MCP server supports the 2025-03-26 and 2025-06-18 protocol versions using POST Streamable HTTP with JSON replies; GET/SSE is unsupported. Do not infer OAuth, A2A task processing, WebMCP, payments, or agent identity capabilities.
