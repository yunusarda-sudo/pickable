# Pickable demo and limits

Pickable currently offers a free public demo. No commercial plans, paid tiers, price list, or service guarantee are published here.

One worker processes scans at a time and accepts up to 20 waiting scans. Default rate limits are 5 accepted scans per peer address per hour and 30 across the server per hour. Operators can tune the per-peer limit with PICKABLE_SCANS_PER_HOUR and the global limit with PICKABLE_GLOBAL_SCANS_PER_HOUR.

Queue and rate-limit responses use HTTP 429 with Retry-After. Limits are held in memory and reset when the process restarts. Behind a proxy, multiple visitors may share a peer-address limit.

Scan history, statuses, reports, previews, and intelligence reads are scoped to the caller’s direct peer IP address. Forwarded-IP headers are not trusted; clients behind a reverse proxy may share the same scope. This is not account authentication or a confidentiality guarantee. Provider evidence is redacted and raw-source endpoints return 404. The demo accepts public targets only.

Scores describe available observations. Pickable does not guarantee agent task completion, referrals, search ranking, or revenue growth.

- Docs: https://solveny.dev/docs
- API: https://solveny.dev/api
