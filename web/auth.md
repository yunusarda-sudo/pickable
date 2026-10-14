# Pickable auth.md — request protection and credentials

This auth.md is for AI agents calling Pickable's public demo. There is no self-service agent registration endpoint: browser clients use a CSRF token, and operator-authorized scan creation uses a pre-shared bearer key. No account signup, OAuth authorization flow, or token endpoint exists on this service.

## Agent provisioning (scan creation)

The provisioning endpoint for agents is POST /api/scans. To register a scan, send Content-Type: application/json with exactly one domain or url string (for example {"domain":"example.com"}). Two credential methods are supported: (1) browser CSRF token from GET /api/config, sent as X-CSRF-Token; (2) operator-issued bearer key, sent as Authorization: Bearer <key>, required for MCP scan creation when the operator enables it. The bearer key authorizes only scan creation and is revoked by replacing or removing PICKABLE_AGENT_API_KEY in the service environment. Never send credentials or secrets for read operations; reads are scoped to the caller's direct peer IP address.

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller's direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. Scan-creation bearer credentials do not bypass read scope. OAuth/OIDC discovery and OAuth protected-resource metadata are intentionally not published because this service offers no OAuth authorization server.

## Browser CSRF token

GET /api/config returns a process-scoped csrf_token. Send that value as X-CSRF-Token with POST /api/scans, along with Content-Type: application/json and exactly one domain or url string (for example {"domain":"example.com"} or {"url":"https://example.com/pricing"}); never combine them. The token is an anti-CSRF request check; it is not user identity, an account login, or an API key for reads.

The server also checks the Host and, when supplied, Origin headers. A disallowed Host or Origin returns 403. Missing or invalid CSRF credentials return 403 unless a valid configured bearer key is supplied.

## Operator-issued bearer key

An operator may configure PICKABLE_AGENT_API_KEY as a shared secret to authorize scan creation. The key must be at least 32 ASCII characters and contain no whitespace. Send it as Authorization: Bearer <key> to POST /api/scans. For MCP scan creation, the operator must configure the same key and set PICKABLE_MCP_SCAN_ENABLED=1; the optional start_website_scan tool then requires the bearer header.

This key authorizes only scan creation; it does not bypass the direct peer-IP read scope or provide account privacy. A missing or invalid key cannot start an MCP scan. The HTTP API returns 403 when neither valid CSRF nor valid bearer credentials are present. When the operator has enabled start_website_scan, a call without a valid bearer key returns HTTP 401 with a Bearer WWW-Authenticate challenge.

## Configure and revoke

Generate a high-entropy secret with at least 32 ASCII characters, configure it through the hosting environment's secret settings, and do not put it in browser code, a public repository, or a URL. To revoke it, replace or remove PICKABLE_AGENT_API_KEY in the service environment and restart/redeploy the process. The previous value stops working when the new configuration is active. Disabling MCP scan creation also requires setting PICKABLE_MCP_SCAN_ENABLED to a value other than 1.

There is no token-issuing endpoint, per-user identity, refresh flow, OAuth, or public authorization metadata.

POST /api/scans/{job_id}/intelligence requires the CSRF token even when a bearer key is configured. Its GET routes follow the same direct peer-IP scope.
