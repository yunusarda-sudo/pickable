# UI ↔ backend contract

## Endpoints

- `GET /api/config` → `{ "csrf_token": "server session token", "mode": "live" | "demo" }`
- `POST /api/scans`, JSON `{ "url": "https://randevuo.com.tr/product" }` (legacy `domain` is accepted), `X-CSRF-Token` header
  → HTTP 202 `{ "job_id": "..." }`. Errors are JSON `{ "error": "description" }`; 429 when the queue is full.
- `GET /api/scans` → `{ "scans": [{"id":"...","domain":"...","status":"completed","created_at":"ISO"}] }`
- `GET /api/scans/:id` → `{ "id", "domain", "created_at", "status": "pending|running|completed|interrupted|failed", "sources": [], "report": null | CustomerReport }`
  - `report` is present as soon as **any** source has finished, even while `status` is still `running`
    (partial report). The UI renders it and keeps polling until the job is terminal.
- `GET /api/scans/:id/report` → customer-facing report projection (download).
- `GET /api/scans/:id/intelligence` → automatic short SaaS product analysis.
- `POST /api/scans/:id/intelligence`, `X-CSRF-Token` → queue optional deep research; returns HTTP 202.
- `GET /api/scans/:id/intelligence/deep` → deep research status and report.
- Analysis jobs persist in SQLite, are unique by scan and depth, and recover queued/running jobs as interrupted after restart. Readiness scoring remains independent.
- Short analysis fetches the submitted public URL with `safe_fetch`, then up to three same-origin pages discovered from real links (pricing, about, docs, and similar). The local Ollama agent can request a relevant, already-linked same-origin page through a bounded `fetch_page` tool; server code validates the link and fetches it with `safe_fetch`. Bounded visible text and SHA-256 records are passed with the `saas-site-intelligence` skill. Every product conclusion must cite a fetched page ref; insufficient facts go into `unknowns`.
- With Ollama, deep analysis reviews the fetched pages in more detail and explicitly does not claim competitor or web research. With hosted OpenAI configured, deep analysis enables web search; citation links are shown for review and competitor claims still require source verification. No mode provides verified market sizing or exhaustive competitor coverage.
- Configure no key for local Ollama (`qwen3:8b` at `127.0.0.1:11434`); it must run on the same machine as the Pickable server. Optional `OPENAI_API_KEY`, `OPENAI_MODEL`, `OLLAMA_MODEL`, and `INTELLIGENCE_PROVIDER` select a hosted provider or local model. Keys and provider traces never enter browser responses.
- The normalized intelligence report is retained in server-side storage; the provider response envelope and raw prompt are discarded, and no public raw-download endpoint is exposed.
- Source ids: `ora`, `cloudflare`, `growth`. Source statuses:
  `pending|running|completed|partial|unavailable|failed|timeout|interrupted`.
- Host/Origin validation and the scan token are backend responsibilities. CORS stays closed.

## Building reports

`report_from_job(job, source_runs)` turns stored job and source-run rows into an internal report.
Source-run fields: `source_id`, `status`, `raw_report` (JSON string or dict), `error`, `finished_at`.
Raw reports are never modified. `server.public_report()` projects them into a customer response:
provider payloads, unmapped findings and source evidence blobs are removed; recommendation text and
safe profile links are retained. The raw endpoint is intentionally absent.

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
- `source_disagreement` flags conflicting eligible comparable results. Internal source summaries expose
  `observed_at`, `served_from_cache` and `result_age_seconds`; source observations preserve native evidence even when excluded from scoring. Error-bearing or
  non-completed runs never score. Cloudflare provider errors or responses without valid checks fail the run.
- `combined_status`: the scoring status when definitive, else UNKNOWN.
- Only PASS+FAIL count in the denominator; warnings, UNKNOWN and N/A are excluded. Unmeasured layers are null.
- Layers: discoverability 20, readability 25, usability 35 (relative weights). The total and coverage
  are rescaled to /100, so all defined checks passing gives 100/100; unmeasured layers never earn free points.
- `score.coverage`: weighted coverage of definitive measurements. `score.rubric_version`: pickable-0.2.
- Raw provider statuses, scores, recommendation order and unmapped findings are preserved internally.
  The customer API returns normalized statuses and recommendation text only; provider text is rendered
  with `textContent`.
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
