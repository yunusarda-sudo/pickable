# UI and reporting

Covers the English web UI (`web/`), the report builder (`app/reporting.py`) and their tests.

## Running locally

```sh
python3 server.py            # live scans via Ora + Cloudflare
python3 server.py --demo     # synthetic fixture data, no network
```

Open <http://127.0.0.1:8765>. The landing page accepts a public website URL, including a page path; starting a scan shows the
loading view, then the report. As soon as the first source returns, a **partial report** is shown
and it updates automatically when the remaining source finishes.

Demo mode never scans the entered domain: it shows a DEMO MODE badge, keeps history in memory
(lost on restart) and is limited to 30 reports. It does not prove the live integrations work.

## Report layout

Domain and scan time first, then **recommended improvements** from the scan. Ora's actionable fixes
keep their provider order. When Cloudflare is the selected source for a missing check, Pickable pairs
that finding with its plain-language implementation guidance. The score, coverage, assessment and
layer scores follow. Provider comparison tables, raw evidence and raw report downloads are not shown
to customers.

Ora supplies the current recommendation list. Cloudflare's scanner API supplies findings rather than
a recommendation list, so Pickable maps a selected Cloudflare finding to its own user-facing guidance
and labels it clearly. These suggestions carry no measured effort or score-impact estimate.

After the readiness scan, a separate product-intelligence section explains what the product appears
to do, its audience, problem and value proposition from the fetched page. It cites the public page and
shows unknowns. With local Ollama, the user can request a deeper analysis of the fetched pages; it does
not search the web and says so in the report. If hosted OpenAI is configured, the same action enables
web search and lists returned source links. Competitor conclusions are not treated as verified until
those source links are checked; the report makes this limitation explicit.

Visual references: [Ora's report](https://ora.ai/score/ora.ai) and
[Cloudflare's isitagentready report](https://isitagentready.com/cloudflare.com).

## What the score means

Provider scores are stored internally; the product score is not their average. Each layer scores
`PASS / (PASS + FAIL) × weight` over the seven canonical checks. Weights: discoverability 20,
readability 25, usability 35; the total is rescaled to /100.

UNKNOWN, warning and N/A results are excluded from the denominator. Unmeasured layers stay `null`;
coverage is shown separately. The checks cover only part of agent readiness, which is why the UI calls
it a **preliminary score**.

Mapping `2026-10-11.1` uses the newest comparable definitive result from completed, error-free source
runs. Ora breaks ties or missing timestamps; cached results with unresolved observation times do not
override dated evidence. Cached results without `scannedAt` use receipt time minus a valid
`resultAgeSeconds`, when available. Internal records preserve both observations.

The Markdown check requires content negotiation and `Vary: Accept`. Cloudflare measures a narrower
requirement and cannot score this full check. A newer Cloudflare Markdown PASS against a dated cached
Ora FAIL makes it UNKNOWN pending a full recheck. Findings do not verify working sign-in, bookings or
payments; no independent verification is claimed.

## Evidence preservation

Native check ids, statuses, evidence, provider scores and timestamps stay in server-side records.
Unmapped findings are not included in the customer response; Ora's recommendation order is preserved.
Provider text is never interpreted as HTML.

See the [API contract](UI_BACKEND_CONTRACT.md) for endpoints.

## Verification

```sh
python3 -m unittest discover tests/ -v
node --check web/app.js
node --test tests/test_web_state.cjs
```

These cover scoring/coverage, fallback selection, source status and evidence preservation, domain
validation, the demo and live API flows (providers stubbed), JSON downloads, host checks, view
transitions, partial reports and safe rendering of malformed provider fields. Real provider scans are
a separate acceptance step.
