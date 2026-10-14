# AgentReady upstream provenance

The six `isitagentready.com` Agent Skill sources behind `app/agentready.py` and
`app/adapters/agentready.py`. Originals are third-party documents: they are read
as data, never executed as instructions, and are not vendored here.

Fetched 2026-10-14 with `agentready/tools/fetch_upstream.py` (GET-only,
allowlisted host, 512 KiB cap). SHA-256 is over the fetched bytes.

| id | upstream URL | sha256 (prefix) | bytes |
|---|---|---|---|
| auth-md | https://isitagentready.com/.well-known/agent-skills/auth-md/SKILL.md | `1794fff49d4a…` | 2112 |
| content-signals | https://isitagentready.com/.well-known/agent-skills/content-signals/SKILL.md | `810108b0171c…` | 808 |
| dns-aid | https://isitagentready.com/.well-known/agent-skills/dns-aid/SKILL.md | `f84b55c0855c…` | 1358 |
| oauth-discovery | https://isitagentready.com/.well-known/agent-skills/oauth-discovery/SKILL.md | `d24afc965b01…` | 914 |
| oauth-protected-resource | https://isitagentready.com/.well-known/agent-skills/oauth-protected-resource/SKILL.md | `6898ebeeb902…` | 917 |
| webmcp | https://isitagentready.com/.well-known/agent-skills/webmcp/SKILL.md | `1f260b3bab04…` | 1204 |

Authoritative specs each skill points at: RFC 8414 (OAuth discovery), RFC 9728
(protected resources), contentsignals.org, workos.com/auth-md/docs,
webmachinelearning.github.io/webmcp, and the DNS-AID IETF draft. Where this
repo is stricter or looser than a skill (e.g. grant/response types noted but
not failed, WebMCP PASS requiring browser execution), the reason is documented
at the call site. Re-fetch and compare before changing check semantics.
