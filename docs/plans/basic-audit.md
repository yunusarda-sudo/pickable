# Multi-source reporting — first version

> Historical plan (2026-10-10). Since superseded: AgentReady was removed and Cloudflare
> (isitagentready.com) added as a fallback source. See [UI and reporting](../UI_REPORTING.md).

Scope: a simple web UI and a working backend; Ora + Python AgentReady, storing source reports,
comparison and a separate 100-point product score.

## Division of work

Two chat sessions worked in this folder. One finished the UI and reporting; the other owned adapters,
storage, the orchestrator and the live API server. The list below is the overall product plan, not a
claim of completion.

## Implementation order

1. Write security and scoring tests first and confirm they fail.
2. Public target validation and a DNS-pinned HTTP transport.
3. Ora scan/polling adapter and a commit-pinned MIT scanner runner.
4. SQLite storage for raw reports/jobs; run both sources in parallel with separate error states.
5. Versioned check mapping, comparison and score/coverage calculation.
6. Domain form, source progress, evidence table, raw JSON download, history.
7. Automated tests, a real randevuo.com.tr scan and a browser check.
8. Code/security review and usage docs.

## Constraints and decisions

- Python standard library + SQLite + HTML/CSS/JS; no frameworks or dependencies.
- Public domains on default ports only; public egress enforced for every discovered URL and redirect.
- Vendor network access goes through the safe fetch wrapper; subprocesses have time/resource limits.
- Provider scores are preserved; the product score is not their average.
- Non-PASS/FAIL data is excluded from the denominator. Fully unmeasured layers are null and earn no
  points. Missing coverage is reported as a percentage and the score is labelled preliminary.
- Partial/related mappings are not treated as verified agreement. Missing conditions are explained.
- Raw reports, native statuses, provider version and scan time are preserved.
- Out of scope: remediation, overlays, real agent task tests, Cloudflare and publishing.

## Acceptance criteria

- One source failing never loses another source's data.
- UNKNOWN/N/A never turns into FAIL or free points.
- Users can open the source and raw evidence of every finding.
- Private IPs, mixed DNS answers and private redirects are blocked before any network request.
- Reports survive a server restart; unfinished jobs are marked interrupted.
- Tests and the local runtime flow pass; real provider problems are shown clearly.
