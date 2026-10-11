# Pickable agent readiness report

Original PR report dated 2026-10-11 (Europe/Istanbul). The verification counts, provider scan, deployment observations, and paths below are historical evidence from the original PR, not validation of this integration. Current integration checks must be recorded separately. Pickable is the product; **solveny.dev is its public demo**. The repository now contains tested discovery documents and real REST/MCP interactions. No commit, push, or public deployment was performed.

## Measured baseline and public state

The [Ora score page](https://ora.ai/score/solveny.dev) was fetched and its embedded scan report inspected. Its observed baseline is **24/100, F**, with Discovery **4/20**, Access **7/30**, Usability **11/40**, and Payments **N/A**. The scan timestamp is 2026-10-11 10:52:34 Europe/Istanbul. This is Ora's score, distinct from Pickable's preliminary normalized score.

| Measurement | Observed result |
|---|---|
| Ora before implementation | 24/100, F |
| Ora after a deployment of these changes | **Not measured: changes are not deployed** |
| Final local discovery verification | 169 checks passed, 0 failed |
| Existing public deployment verification | 16 checks passed, 54 failed |

At the original verification timestamp, public GET `/`, `/api/config`, and `/api/scans` worked. The newly implemented discovery/API documentation resources returned 404 publicly, and public POST `/mcp` returned 404. That deployment's HEAD behavior returned 501. The failed public verifier is expected evidence of deployment state, not a failed local implementation.

Evidence is saved in:

- `docs/AGENT_READINESS_BASELINE.json`: observed Ora score, scan date, layer scores and reported gaps.
- `docs/AGENT_READINESS_LOCAL_VERIFICATION.json`: individual final localhost results with timestamp.
- `docs/AGENT_READINESS_PUBLIC_VERIFICATION.json`: public results and separate existing REST checks; no tokens or scan contents recorded.
- `docs/AGENT_READINESS_LIVE_SCAN.json`: real scanner execution through protected local MCP.

## Patch evaluation and implemented changes

The prepared `/Users/0x79de/Downloads/agent-readiness-patch` was used as a starting point. Its proposed files, installer, tests and plan were inspected. The installer was not run blindly: changes were adapted to the actual standard-library server, existing scan backend and current scoring gaps. No new application runtime dependency was added.

Discovery now includes robots/sitemap, short/full and scoped LLM documentation, a Pickable manifest, separate ARD and AI Catalog formats, an RFC 9727 API linkset, an Agent Skills index with an exact digest, and a schema-validated MCP Server Card. The catalog contains only concrete URLs; templated scan operations belong in OpenAPI.

Product and developer pages have real Markdown twins, canonical and Open Graph metadata, local logo assets, and valid JSON-LD. HTML links advertise the actual `.md` resource. `Accept: text/markdown` and `/?mode=agent` serve onboarding content. HEAD works, Link/Vary discovery headers are present, and unknown page URLs can return a useful Markdown 404. API errors remain JSON.

OpenAPI 3.1 documents real configuration, health, scan creation, peer-IP-scoped history, status, redacted normalized reports, previews, local intelligence and MCP. Every operation has an ID, description and response definitions. Marketing checks have their own schema because they differ from readiness checks. Root HTTP(S) URLs and IDNA normalization are described accurately. Partial and unavailable reports remain explicit.

## Actual agent capabilities

Stateless Streamable HTTP at POST `/mcp` negotiates `2025-03-26` or `2025-06-18`. Initialization, initialized notification, ping, tools/list, tools/call, protocol errors, schema checks and HTTP header handling were exercised. JSON responses are used; GET streaming and sessions are not implemented.

Public default tools:

- `describe_pickable`: returns product scope, documentation, methods and limitations.
- `list_scans`: reads stored scan summaries for the caller direct peer IP.
- `get_scan_report`: reads actual job status, sources and normalized findings. Large responses are bounded to 256 KiB on the wire with explicit truncation and REST links. The June protocol includes structuredContent; the March protocol provides the JSON in text.

Optional `start_website_scan` calls the existing backend through shared admission controls. It is listed only when both `PICKABLE_MCP_SCAN_ENABLED=1` and a valid `PICKABLE_AGENT_API_KEY` are configured; invocation requires the operator's bearer key. REST retains the existing CSRF flow and accepts that bearer key as an alternative.

One **real** protected MCP scan of solveny.dev was run against a local LIVE backend. Ora, Cloudflare and the marketing checker all completed. MCP returned the stored normalized report with seven readiness checks. That original PR run verified provider-backed behavior in its original local checkout; the local job is not a public deployed report, and this is not a ChatGPT/Claude UI-client compatibility claim or an independent agent journey.

Unsupported resources are intentionally absent: `/.well-known/agent-card.json` is 404 because no A2A interface exists; `/.well-known/ai-plugin.json` is 404 because a valid legacy manifest cannot be completed with verified contact/legal information. The service does not advertise OAuth, payments or registry enrollment.

## Security and reliability

All scan creation shares atomic per-peer/global limits, defaulting to 5 per peer/hour and 30 per process/hour. The peer table is bounded to 1,024 entries and expires old entries. A live queue holds at most 20 waiting jobs, with one job executing at a time and source adapters running in parallel. Notifications never start scans. REST uses HTTP 429 plus Retry-After; MCP tool business failures preserve isError and actionable error codes/retry information.

Host/Origin checks cover the new routes. Forwarding headers are not trusted, and no permissive CORS is added. Bodies are capped at 2,048 bytes for REST creation and 32 KiB for MCP; malformed lengths, media types, JSON and stalled bodies have controlled errors. There are at most 32 concurrent handlers and a 10-second request read timeout. Preview successes and failures are cached for five minutes with at most 128 entries. SQLite access is serialized across request and worker threads.

Existing public-IP validation and pinned fetching remain in use. Ora polling now has a real deadline, its polling URL must remain on its configured provider origin, cross-origin redirects remove credentials, and HTTPS redirects cannot downgrade. Container build context excludes credentials, databases and local caches.

Limits are local to one process and reset on restart. Clients behind a reverse proxy may share its peer budget. Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. Before offering private reports or multiple replicas, implement real access control and shared admission controls.

## Historical PR validation evidence

| Check | Result |
|---|---|
| Baseline Python suite | 54 passed |
| Final `python3 -m unittest discover tests -v` (Python 3.14.6) | 85 passed |
| Same final suite on deployment-compatible Python 3.12.12 | 85 passed |
| `node --test tests/test_web_state.cjs` | 15 passed |
| Final localhost demo verifier | 169 passed, 0 failed |
| `run.py server` LIVE entrypoint + read-only verifier | 169 passed, 0 failed |
| Real local MCP scan using all three external sources | completed, all sources completed |
| Full OpenAPI validation and 13 actual REST/MCP HTTP response schemas | passed |
| Official ARD schema and upstream MCP Server Card schema checks | passed |
| Sitemap, JSON-LD, internal links, skill digest and mapped files | passed |
| `git diff --check`; `docker compose config --quiet` | passed |
| Docker image build / hosted deployment | not performed: Docker daemon unavailable; no configured remote deployment access |

The temporary schema validators were jsonschema 4.26.0 and openapi-spec-validator 0.9.0 in an isolated uv environment, not application dependencies. Targeted tests cover scan auth, shared rate limits, global/table bounds, timeouts, malformed payloads, notification safety, the MCP wire-size bound, provider deadlines/credential isolation, and concurrent SQLite access.

Locally verified resources include `/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/llms-full.txt`, `/openapi.json`, `/docs`, `/about`, `/api`, `/docs/agent-scan`, `/pricing`, `/auth.md`, `/skill.md`, all Markdown twins and scoped indexes, `/.well-known/agent.json`, `/.well-known/ard.json`, `/.well-known/ai-catalog.json`, `/.well-known/api-catalog`, `/.well-known/agent-skills/index.json`, `/mcp/server-card`, its well-known alias, `/api/health`, and the existing REST scan endpoints. The final verifier artifact contains exact methods, response statuses and content types.

## Deployment and follow-up

The existing Docker/Traefik and Render configurations include the new files through the existing source/build workflow. Render uses `/api/health`; allowed demo hosts and the canonical public origin are configured. MCP scan creation stays off by default. There is no Pickable/Solveny SSH target or Render deployment environment configured in this session, and the local Docker daemon is unavailable. Commit/push was explicitly prohibited, so the Git-driven deployment route was not used.

Local reproduction, from `/Users/0x79de/dev/pickable`:

```bash
python3 -m unittest discover tests -v
node --test tests/test_web_state.cjs
PICKABLE_PUBLIC_URL=http://127.0.0.1:8765 python3 server.py --demo
# In another terminal:
python3 scripts/verify_live.py --url http://127.0.0.1:8765
```

After delivering the reviewed changes to the existing deployment checkout, run these commands there (the existing Traefik `root_default` network must be available):

```bash
docker compose config --quiet
docker compose up -d --build
python3 scripts/verify_live.py --url https://solveny.dev
```

Alternatively, publish the reviewed source through the approved Render repository workflow and deploy the updated revision with `render.yaml`. Set the bearer key in the hosting secret environment only if enabling MCP creation. After a successful public verifier, rescan [Ora](https://ora.ai/score/solveny.dev) and record the newly observed score and scan timestamp. No numeric score improvement is promised.

## Files created or modified

- `.dockerignore`
- `README.md`
- `app/adapters/ora.py`
- `app/safe_fetch.py`
- `app/storage.py`
- `docker-compose.yml`
- `render.yaml`
- `server.py`
- `tests/test_ui_api.py`
- `web/index.html`
- `.env.example`
- `AGENT_READINESS_PLAN.md`
- `AGENT_READINESS_REPORT.md`
- `app/agent_policy.py`
- `app/pickable_agent_routes.py`
- `docs/AGENT_READINESS_BASELINE.json`
- `docs/AGENT_READINESS_LIVE_SCAN.json`
- `docs/AGENT_READINESS_LOCAL_VERIFICATION.json`
- `docs/AGENT_READINESS_PUBLIC_VERIFICATION.json`
- `scripts/verify_live.py`
- `tests/test_agent_backend_security.py`
- `tests/test_agent_readiness.py`
- `web/.well-known/agent-skills/index.json`
- `web/.well-known/agent.json`
- `web/.well-known/ai-catalog.json`
- `web/.well-known/api-catalog.json`
- `web/.well-known/ard.json`
- `web/.well-known/mcp/server-card.json`
- `web/AGENTS.md`
- `web/SKILL.md`
- `web/about.html`
- `web/about.md`
- `web/agent-scan.html`
- `web/agent-scan.md`
- `web/api-docs.html`
- `web/api-llms.txt`
- `web/api.md`
- `web/auth.md`
- `web/docs-llms.txt`
- `web/docs.html`
- `web/docs.md`
- `web/index.md`
- `web/llms-full.txt`
- `web/llms.txt`
- `web/openapi.json`
- `web/pickable-mark.svg`
- `web/pickable-og.svg`
- `web/pricing.html`
- `web/pricing.md`
- `web/robots.txt`
- `web/site-docs.css`
- `web/sitemap.xml`
- `web/skills/pickable-scan/SKILL.md`

## Integration contract retained

POST /api/scans accepts exactly one string field: {"domain":"example.com"} for a bare public domain or HTTP(S) root URL, or {"url":"https://example.com/pricing"} for an exact public URL path. Do not send both fields. Credentials, IP literals, private/reserved destinations, query strings, and fragments are rejected; domain input also rejects paths and ports. MCP creation remains domain-only. The bearer key does not bypass scoped reads. GET `/api/scans/{job_id}/intelligence` and `/intelligence/deep` retain local analysis; POST `/api/scans/{job_id}/intelligence` requires CSRF even when a bearer key is configured. The configured public origin is retained through `PICKABLE_PUBLIC_URL`.
