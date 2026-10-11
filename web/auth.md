# Pickable request protection and credentials

Pickable has no user accounts or OAuth authorization flow. Public scan history, statuses, reports, raw source results, and previews can be read without a credential. A key on scan creation does not add confidentiality to those public GET routes.

## Browser CSRF token

GET /api/config returns a process-scoped csrf_token. Send that value as X-CSRF-Token with POST /api/scans, along with Content-Type: application/json and the exact body {"domain":"example.com"}. The token is an anti-CSRF request check; it is not user identity, an account login, or an API key for reads.

The server also checks the Host and, when supplied, Origin headers. A disallowed Host or Origin returns 403. Missing or invalid CSRF credentials return 403 unless a valid configured bearer key is supplied.

## Operator-issued bearer key

An operator may configure PICKABLE_AGENT_API_KEY as a shared secret to authorize scan creation. The key must be at least 32 ASCII characters and contain no whitespace. Send it as Authorization: Bearer <key> to POST /api/scans. For MCP scan creation, the operator must configure the same key and set PICKABLE_MCP_SCAN_ENABLED=1; the optional start_website_scan tool then requires the bearer header.

This key authorizes only the configured scan-creation operation. It does not authenticate callers to public read endpoints or make scan data private. A missing or invalid key cannot start an MCP scan. The HTTP API returns 403 when neither valid CSRF nor valid bearer credentials are present. When the operator has enabled start_website_scan, a call without a valid bearer key returns HTTP 401 with a Bearer WWW-Authenticate challenge.

## Configure and revoke

Generate a high-entropy secret with at least 32 ASCII characters, configure it through the hosting environment's secret settings, and do not put it in browser code, a public repository, or a URL. To revoke it, replace or remove PICKABLE_AGENT_API_KEY in the service environment and restart/redeploy the process. The previous value stops working when the new configuration is active. Disabling MCP scan creation also requires setting PICKABLE_MCP_SCAN_ENABLED to a value other than 1.

There is no token-issuing endpoint, per-user identity, refresh flow, OAuth, or public authorization metadata.
