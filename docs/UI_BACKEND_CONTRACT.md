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
- `scoring_status` / `scoring_source`: Ora's result when it is a definitive PASS/FAIL; otherwise
  Cloudflare's, if definitive (fallback). Otherwise the check is unscored. llms.txt is Ora-only.
- `combined_status`: the scoring status when definitive, else UNKNOWN.
- Only PASS+FAIL count in the denominator; warnings, UNKNOWN and N/A are excluded. Unmeasured layers are null.
- Weights 20/25/35/10/10. Security and payments are not scored yet, so all defined checks passing
  gives 80/100 at 80% coverage; unmeasured areas never earn free points.
- `score.coverage`: weighted coverage of definitive measurements. `score.rubric_version`: pickable-0.1.
- Raw provider statuses, scores and recommendation order are preserved. `source_specific` keeps all
  unmapped findings. The UI renders provider text with `textContent` only.

## Marketing playbook

`report.marketing` = `{checks, score, source}`, built from the `growth` source only, scored with its own
weights (social presence 40, content & blog 60). Checks: social profiles linked, has a blog, blog
updated within 60 days, 3+ posts in the last 90 days. Post dates come from the RSS/Atom feed, else
sitemap `lastmod`; when neither has dates the recency checks are UNKNOWN, not FAIL. While `growth`
is running every check is PENDING and `score.final` is false.

## Running

`python3 server.py` runs live scans (`mode: "live"`). `python3 server.py --demo` uses fixture data
only and shows a DEMO MODE badge.
