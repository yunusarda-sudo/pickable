# UI and reporting

Covers the English web UI (`web/`), the report builder (`app/reporting.py`) and their tests.

## Running locally

```sh
python3 server.py            # live scans via Ora + Cloudflare
python3 server.py --demo     # synthetic fixture data, no network
```

Open <http://127.0.0.1:8765>. The landing page has a domain field; starting a scan shows the
loading view, then the report. As soon as the first source returns, a **partial report** is shown
and it updates automatically when the remaining source finishes.

Demo mode never scans the entered domain: it shows a DEMO MODE badge, keeps history in memory
(lost on restart) and is limited to 30 reports. It does not prove the live integrations work.

## Report layout

Domain and scan time first, then the large preliminary score, coverage, overall assessment and
check counts. Below the layer scores: **next steps** for checks reported missing, and the checks
grouped by layer with expandable details. Source scores, method, the check table, raw evidence and
original report downloads live under **Sources & method**.

Next steps are generic Pickable suggestions derived from missing checks, labelled with the source
they are based on. They are not the provider's own recommendations or priority order and carry no
effort or score-impact estimate. Provider recommendations keep their original order in the technical
details.

Visual references: [Ora's report](https://ora.ai/score/ora.ai) and
[Cloudflare's isitagentready report](https://isitagentready.com/cloudflare.com).

## What the score means

Provider scores are shown separately; the product score is not their average. Each layer scores
`PASS / (PASS + FAIL) × weight` over the seven canonical checks. Weights: discoverability 20,
readability 25, usability 35, security & compliance 10, payments 10.

UNKNOWN, warning and N/A results are excluded from the denominator. Unmeasured layers stay `null`;
coverage is shown separately. Security and payments are not scored yet, so a perfect run is 80/100 at
80% coverage, which is why the UI calls it a **preliminary score**.

Each check uses Ora's definitive result; when Ora has none, Cloudflare's definitive result is used as
a fallback. Every row shows which source it was scored from. No independent verification is claimed.

## Evidence preservation

Native check ids, statuses, evidence, provider scores and timestamps are kept in the report. Unmapped
findings are listed separately; Ora's recommendation order is unchanged. Provider text is never
interpreted as HTML.

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
