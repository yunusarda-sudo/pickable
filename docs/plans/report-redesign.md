# Report screen redesign

> Historical plan (2026-10-10). References to AgentReady predate its removal.

## Goal and boundaries

Keep the landing page. The report should lead with the result and actionable work instead of a
technical comparison table. Use Ora's score + layers + fixes flow and Cloudflare's per-category
check/guidance approach as references, without copying their scores or verification claims. The
backend and scoring formula do not change.

## Product hierarchy

1. **Report header:** domain, scan time, test-data label; JSON download as a secondary action.
2. **Summary:** large Pickable preliminary score with measurement coverage beside it, and a "review
   gaps" action. PASS/FAIL/warning-unclear counts come from existing `scoring_status` values. No
   success percentage or fake severity.
3. **Category navigation:** All + five layers, each with measured/defined checks and points;
   security/payments show "Not measured". Selection filters the visible checks; it never changes the score.
4. **Check cards:** instead of a technical table — title, plain-language status, short explanation of the
   measure, source labels and "View evidence". Missing checks first; differing source results get a
   separate amber label. `combined_status` and `scoring_status` are never mixed up. A partial match's
   single-source scoring input is readable on the card.
5. **Suggested steps:** only recommendations the source actually provides, as readable cards. Ora's order
   is preserved; no arbitrary priority, score gain or effort estimate. Without recommendations, show the
   measure and evidence link; never invent a fix.
6. **Sources and method:** Ora and AgentReady with their own score/level, status, time and raw JSON link.
   Method notes and unmapped source findings in a collapsible section. Sources never dominate the first screen.

## Evidence and interaction

- A source's native result, the comparison label and the scored result are kept separate.
- The evidence dialog shows a readable explanation and source id first; raw JSON is kept as detail.
- API error, single source, no measurements, all PASS and unmeasured category each get their own empty/state text.
- Provider text uses `textContent` only; no HTML/URL is executed.
- Category buttons show a selected state; keyboard focus works; categories scroll horizontally on mobile.

## Acceptance and verification

- Landing page design and flow are unchanged.
- The first report screen shows domain, preliminary score, coverage and the next action; no raw JSON.
- The demo fixture's 20/100 at 34.2% coverage stays the same; the source's own 61/D and 1/5 stay separate.
- Every canonical check and unmapped source finding is reachable; filtered counts use real data.
- Mobile/desktop, category + status filter, evidence open/close, JSON download and back-to-new-scan are
  verified in the browser.
- Safe display of a missing source, null score and malicious provider text stays covered by regression tests.
