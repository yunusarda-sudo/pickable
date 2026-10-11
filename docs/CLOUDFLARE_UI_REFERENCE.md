# Cloudflare Agent Readiness reference

Observed on 2026-10-11. This is a design reference and integration investigation,
not an implemented redesign or a change to Pickable scoring.

## Verified access

- The existing Chrome session can open the Cloudflare account and the
  randevuo.com.tr zone. Its Agent Readiness menu includes Overview, Diagnostics,
  AI Playground and WebMCP. The latter three are labelled Beta.
- The zone is on Free. Markdown for Agents is disabled and labelled Pro plan.
- Plugin Management search returned no Cloudflare plugin in this session.
- Cloudflare offers an official OAuth-capable MCP at
  `https://mcp.cloudflare.com/mcp`; this investigation did not connect it.
- Pickable's existing `CloudflareAdapter.scan()` successfully called the public
  scanner for randevuo.com.tr. Result: completed, no error, level 1.
- Response keys: url, targetUrl, scannedAt, level, levelName, checks, nextLevel,
  isCommerce, commerceSignals. `checks` contains five category objects.
- The adapter returns raw JSON and an empty normalized checks list. Reporting
  separately reads provider JSON; a successful request alone does not verify
  every normalization path or UI mapping.

## Public scanner

Reference: https://isitagentready.com/randevuo.com.tr

- URL input, Scan button, Customize scan and last-scanned timestamp.
- Overall score and level, followed by five category score rings and counts.
- An Improve the score action with the number of suggestions.
- Category headings followed by expandable checks with status icons.
- Missing-check details: Goal, Issue, How to implement, Resources, Copy prompt.
- Audit details reveal actual requests, responses, headers, conclusion and time.
- Share score card and scan-another-site actions.
- Observed dark appearance: warm dark background, off-white text, orange accent,
  thin borders, green pass and red fail icons. Action button: 8px radius,
  12px 20px padding, 14px type. Category group gap: 32px.
- Live scan returned 20/100. Web Bot Auth is explicitly Not applicable in the
  browser accessibility state, explaining the bot category's 1/2 count despite
  showing three checks. Plain pasted text had lost that status distinction.

## Account Diagnostics

- Light background, persistent left navigation and zone/account context.
- Main heading, description, Rescan button and timestamp.
- Expandable stages: Quick Wins, Technical Groundwork, Advanced Integration,
  Commerce. Each has an explanation and completed/total counts.
- Compact check rows lead with a plain-language purpose and a protocol badge.
- Quick Wins showed 3/5 complete; further groups 0/3, 0/8 and optional 0/5.

## Adaptation for Pickable

1. Keep Pickable branding and its existing preliminary score/coverage semantics.
2. Use the public scanner's score overview and category navigation as the visual
   reference. Show unknown and unmeasured categories explicitly.
3. Use Diagnostics' compact, expandable rows to reduce report length.
4. Lead expanded rows with a plain-language finding, source attribution and next
   step; keep technical evidence behind a secondary disclosure.
5. Offer copyable suggestions only from an explicit, attributed provider field
   or clearly labelled Pickable guidance. Do not execute suggestions.
6. Preserve source scores separately. Do not import Cloudflare's levels, weights,
   High impact labels or completed counts into Pickable's own score.
7. Keep Normal/Agent modes and partial-report/error handling intact.
8. Use existing local fonts; do not depend on Cloudflare's proprietary font.

Implementation should first verify provider recommendation fields and current
report mappings. Acceptance must cover responsive layout, keyboard disclosures,
source failures, unknown/N/A states, partial results and safe provider text.

## Sources

- https://blog.cloudflare.com/agent-readiness/
- https://github.com/cloudflare/mcp/blob/main/README.md
- https://developers.cloudflare.com/fundamentals/reference/markdown-for-agents/
