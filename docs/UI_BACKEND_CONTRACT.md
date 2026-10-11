# UI ↔ backend contract

## Endpoints

- `GET /api/config` → `{ "csrf_token": "server session token", "mode": "live" | "demo" }`
- `POST /api/scans`, JSON `{ "domain": "randevuo.com.tr" }`, `X-CSRF-Token` header
  → HTTP 202 `{ "job_id": "..." }`. Errors are JSON `{ "error": "description" }`; 429 when the queue is full.
- `GET /api/scans` → `{ "scans": [{"id":"...","domain":"...","status":"completed","created_at":"ISO"}] }`
- `GET /api/scans/:id` → `{ "id", "domain", "created_at", "status": "pending|running|completed|interrupted|failed", "sources": [], "report": null | UnifiedReport }`
  - `report` is present as soon as **any** source has finished, even while `status` is still `running`
    (partial report). The UI renders it and keeps polling until the job is terminal.
- `GET /api/scans/:id/report` → report JSON (download).
- `GET /api/scans/:id/raw/:source` → unmodified provider JSON (download).
- Source ids: `ora`, `cloudflare`, `growth`. Source statuses:
  `pending|running|completed|partial|unavailable|failed|timeout|interrupted`.
- Host/Origin validation and the scan token are backend responsibilities. CORS stays closed.

## Building reports

`report_from_job(job, source_runs)` turns stored job and source-run rows into the UI report.
Source-run fields: `source_id`, `status`, `raw_report` (JSON string or dict), `error`, `finished_at`.
Raw reports are never modified.

```python
from app.reporting import build_report
report = build_report("randevuo.com.tr", {
  "ora": {"status": "completed", "raw": ora_json, "observed_at": "ISO"},
  "cloudflare": {"status": "completed", "raw": cloudflare_json, "observed_at": "ISO"},
})
```

## Scoring

- The mapping has 7 canonical checks. Ora and Cloudflare (isitagentready.com) run in parallel.
- `mapping_version`: `2026-10-11.1`. `scoring_status` / `scoring_source` select the newest comparable
  PASS/FAIL from completed, error-free runs. Ora breaks ties or missing timestamps; cached results with
  unresolved times cannot override dated evidence. With no eligible definitive result the check is
  unscored. llms.txt is Ora-only.
- Observation time uses provider `scannedAt`; cached reports without it derive the time from receipt
  time minus a finite, nonnegative `resultAgeSeconds`. If that is unavailable, cache time is unknown;
  uncached reports can use receipt time. Report `scanned_at` is report creation time.
- `content.markdown` requires content negotiation and `Vary: Accept`. Cloudflare's narrower check
  cannot score this full requirement (`scope_difference`). A newer dated Cloudflare PASS against a
  dated cached Ora FAIL yields UNKNOWN with no scoring source, pending a full recheck.
- `source_disagreement` flags conflicting eligible comparable results. Source summaries expose
  `observed_at`, `served_from_cache` and `result_age_seconds`; the UI discloses disagreements and cache
  use. Source observations preserve native evidence even when excluded from scoring. Error-bearing or
  non-completed runs never score. Cloudflare provider errors or responses without valid checks fail the run.
- `combined_status`: the scoring status when definitive, else UNKNOWN.
- Only PASS+FAIL count in the denominator; warnings, UNKNOWN and N/A are excluded. Unmeasured layers are null.
- Layers: discoverability 20, readability 25, usability 35 (relative weights). The total and coverage
  are rescaled to /100, so all defined checks passing gives 100/100; unmeasured layers never earn free points.
- `score.coverage`: weighted coverage of definitive measurements. `score.rubric_version`: pickable-0.2.
- Raw provider statuses, scores and recommendation order are preserved. `source_specific` keeps all
  unmapped findings. The UI renders provider text with `textContent` only.
- Findings are source-reported; functional sign-in, bookings and payments are not verified.

## Marketing playbook

`report.marketing` = `{checks, score, source}`, built from the `growth` source only, scored with its own
weights (social presence 40, content & blog 60). Checks: social profiles linked, has a blog, blog
updated within 60 days, 3+ posts in the last 90 days. Post dates come from the RSS/Atom feed, else
sitemap `lastmod`; when neither has dates the recency checks are UNKNOWN, not FAIL. While `growth`
is running every check is PENDING and `score.final` is false.

## Running

`python3 server.py` runs live scans (`mode: "live"`). `python3 server.py --demo` uses fixture data
only and shows a DEMO MODE badge.
