# Pickable remaining agent-readiness actions

Prioritized on 2026-10-11. Pickable is the product; solveny.dev is its public demo. The measured Ora baseline is 24/100. The local implementation is complete and tested; its deployed score has not been measured.

| Priority | Action | Expected benefit | Acceptance evidence |
|---|---|---|---|
| P0 | Deliver this checkout through the existing approved Docker/Render workflow and deploy it. No commit/push was performed in this session. | Makes implemented discovery, Markdown, API and MCP resources publicly available. | Public verifier exits 0; route statuses/types, tools and canonical origin match local evidence. |
| P0 | Rescan Ora after deployment and compare its dated report with the saved baseline. | Measures actual improvement and identifies remaining gaps. | Observed score, scan time, layer scores and failed-check list saved; no inferred score. |
| P1 | Test a supported real ChatGPT/Claude MCP client through initialization, scan creation when enabled, polling and report interpretation. | Verifies client compatibility and useful task completion beyond raw HTTP checks. | Client/version, tool calls, final result and failures recorded. |
| P1 | Configure trusted reverse-proxy admission controls and persistent shared limits before increasing traffic or replica count. | Prevents process restarts and shared proxy peers from undermining reliable quota control. | Limits tested across replicas/restarts; rejected calls do not reach providers. |
| P1 | Choose public-demo retention policy and implement actual per-user authorization if private reports become a product requirement. | Establishes confidentiality where genuinely needed. | Ownership tests, migration/retention behavior and accurate auth/privacy docs. |
| P2 | Publish the reviewed repository/skill through explicitly approved channels and monitor organic name-based discovery. | Addresses source/skill and brand-search gaps that deployment alone cannot resolve. | Verified public source and skill listings, relevant search samples and useful referrals. |
| P2 | Improve operational measurement: source latency, failures, queue depth, completed jobs and provider quota visibility. | Supports reliability under actual demand. | Observable metrics and actionable alerts without exposing credentials/report contents. |
| Conditional | Complete a valid legacy ai-plugin manifest only if a real consumer needs it and verified contact/legal resources exist. | Enables that specific legacy discovery consumer. | Manifest schema and every declared resource verified. |

Score-related features that do not fit the current product are not implementation targets by themselves. Add OAuth, A2A, WebMCP, agent UI integrations, paid tiers or SDK/package publication only when there is a concrete user workflow and a functioning implementation. Do not fabricate business address/contact details, external listings, Wikipedia notability, payment behavior or privacy guarantees for scanner credit.

A score of 100 cannot be promised: Ora includes external search/registry presence and integrations the current public scanner does not provide. Prioritize deployed discovery and successful agent scans, then use the actual rescan to select the next legitimate improvement.

Follow-up commands on the configured deployment host:

```bash
docker compose config --quiet
docker compose up -d --build
python3 scripts/verify_live.py --url https://solveny.dev
```

Then inspect https://ora.ai/score/solveny.dev after a fresh Ora scan. See AGENT_READINESS_REPORT.md for reproduction, measured evidence and the complete file list.
