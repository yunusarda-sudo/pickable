"""Versioned, conservative mappings; native evidence is never rewritten."""
from datetime import datetime, timezone
import json
import math

SOURCES = ("ora", "cloudflare")  # tie-break priority; newer comparable observations win
SOURCE_NAMES = {"ora": "Ora", "cloudflare": "Cloudflare"}
WEIGHTS = {"discovery": 20, "content": 25, "usability": 35}  # totals are rescaled to /100
LAYER_NAMES = {"discovery": "Discoverability", "content": "Readability", "usability": "Usability"}
# This is a product rubric, not a provider-score average or calibrated confidence.
# (canonical id, title, category, Ora id, Cloudflare id, definition)
DEFINITIONS = [
    ("discovery.sitemap", "XML sitemap", "discovery", "sitemap", "sitemap", "Sitemap discoverability."),
    ("discovery.link_headers", "HTTP Link header", "discovery", "link-headers-discovery", "linkHeaders", "HTTP Link header signals for machine discovery."),
    ("content.llms_txt", "llms.txt", "content", "llms-txt-exists", None, "Presence of an llms.txt file (root or alternate path)."),
    ("content.markdown", "Markdown delivery", "content", "markdown-negotiation-vary", "markdownNegotiation", "Markdown content negotiation and Vary behavior on the home page."),
    ("auth.oauth_metadata", "OAuth metadata", "usability", "mcp-oauth-metadata", "oauthDiscovery", "OAuth discovery metadata."),
    ("auth.protected_resource", "Protected Resource", "usability", "oauth-protected-resource", "oauthProtectedResource", "Protected Resource metadata; the sign-in flow itself is not tested."),
    ("integration.mcp_card", "MCP server card", "usability", "mcp-server-card", "mcpServerCard", "MCP server card discovery."),
]
# Marketing playbook (Pickable's own "growth" checker). (canonical id, title, category, growth id, definition)
MARKETING_WEIGHTS = {"social": 40, "content": 60}
MARKETING_LAYERS = {"social": "Social presence", "content": "Content & blog"}
MARKETING_DEFINITIONS = [
    ("social.profiles", "Social profiles linked", "social", "socialProfiles", "Links from the home page to the brand's social profiles."),
    ("content.blog", "Has a blog", "content", "blogExists", "A blog index or RSS/Atom feed linked from the home page."),
    ("content.blog_recent", "Blog updated recently", "content", "blogRecent", "Latest dated post is recent (feed or sitemap dates)."),
    ("content.blog_cadence", "Posts regularly", "content", "blogCadence", "Enough posts in the last 90 days to show a steady cadence."),
]
NORMALIZED = {"pass": "PASS", "fail": "FAIL", "warn": "WARNING", "warning": "WARNING", "na": "NOT_APPLICABLE", "skip": "NOT_APPLICABLE", "not_applicable": "NOT_APPLICABLE",
             "unknown": "UNKNOWN", "neutral": "UNKNOWN", "pending": "UNKNOWN",
             "error": "ERROR", "timeout": "ERROR", "failed": "ERROR", "unavailable": "ERROR"}
NO_EVIDENCE = "No definitive PASS/FAIL evidence. Warnings, N/A and access errors are excluded from the score."
# Shadow informational checks (Sprint A): never scored, never override DEFINITIONS.
SHADOW_SOURCES = ("agentready",)
AGENTREADY_CHECK_IDS = ("contentSignals", "oauthDiscovery", "oauthProtectedResource", "authMd", "webMcp", "dnsAid")
AGENTREADY_SPEC_URLS = {
    "contentSignals": "https://contentsignals.org/",
    "oauthDiscovery": "https://www.rfc-editor.org/rfc/rfc8414",
    "oauthProtectedResource": "https://www.rfc-editor.org/rfc/rfc9728",
    "authMd": "https://workos.com/auth-md/docs",
    "webMcp": "https://webmachinelearning.github.io/webmcp/",
    "dnsAid": "https://datatracker.ietf.org/doc/draft-mozleywilliams-dnsop-dnsaid/",
}
# Pickable-authored fallback guidance for FAIL checks the provider gave no
# recommendation for. Never attributed to a provider; items carry source "pickable".
PICKABLE_GUIDANCE = {
    "discovery.sitemap": "Publish a valid XML sitemap and reference it in robots.txt.",
    "discovery.link_headers": "Advertise your machine entry points with the right HTTP Link relations.",
    "content.llms_txt": "Add a root /llms.txt with an H1 title and links to useful content.",
    "content.markdown": "Check Markdown responses, content type and Accept/Vary behavior together.",
    "auth.oauth_metadata": "If you offer a protected API, publish root OAuth/OIDC discovery metadata. Not needed for public-only content.",
    "auth.protected_resource": "If you have protected resources, declare the resource id and authorization servers in metadata.",
    "integration.mcp_card": "If you run an MCP server, publish its discovery card with the advertised endpoints.",
}


def text_value(value):
    return value if isinstance(value, str) else None


def number_value(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def observation_time(raw, received_at):
    scanned_at = text_value(raw.get("scannedAt"))
    if scanned_at:
        return scanned_at
    if raw.get("servedFromCache") is True:
        age = number_value(raw.get("resultAgeSeconds"))
        received = timestamp(received_at)
        if age is not None and math.isfinite(age) and age >= 0 and received is not None:
            try:
                return datetime.fromtimestamp(received - age, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        return None
    return text_value(received_at)


def choose_source(candidates, observations):
    if not candidates:
        return None
    times = {s: timestamp(observations[s]["observed_at"]) for s in candidates}
    if any(t is not None for t in times.values()):
        candidates = [s for s in candidates if times[s] is not None or not observations[s].get("cached")]
    if all(t is not None for t in times.values()):
        return max(candidates, key=lambda s: times[s])
    return candidates[0]


def report_from_job(job, source_runs, pending=()):
    sources = {}
    for run in source_runs:
        source = run.get("source_id")
        if source not in SOURCES + ("growth",) + SHADOW_SOURCES:
            continue
        raw, error = run.get("raw_report"), run.get("error")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw, error = None, "The stored provider report could not be read as JSON."
        if raw is not None and not isinstance(raw, dict):
            raw, error = None, "The provider report is not a JSON object."
        observed_at = observation_time(raw or {}, run.get("finished_at"))
        sources[source] = {"status": run.get("status", "unavailable"), "raw": raw,
                           "error": error, "observed_at": observed_at}
    return build_report(job["domain"], sources, pending)


def flatten(source, raw):
    if not isinstance(raw, dict):
        return {}
    result = {}
    if source == "ora":
        layers = raw.get("layers")
        for layer in layers if isinstance(layers, list) else []:
            checks = layer.get("checks") if isinstance(layer, dict) else None
            for c in checks if isinstance(checks, list) else []:
                if isinstance(c, dict) and isinstance(c.get("id"), str):
                    result[c["id"]] = c
        return result
    groups = raw.get("checks")
    for group in groups.values() if isinstance(groups, dict) else []:
        for cid, c in group.items() if isinstance(group, dict) else []:
            if isinstance(c, dict):
                result[cid] = c
    return result


def observation(cid, rows, envelope):
    raw = rows.get(cid)
    native = raw.get("status") if raw else None
    return {"source_check_id": cid, "status": NORMALIZED.get(native, "UNKNOWN") if isinstance(native, str) else "UNKNOWN", "native_status": native,
            "raw": raw, "observed_at": envelope.get("observed_at"), "attribution": "source_reported",
            "eligible": envelope.get("status") == "completed" and not envelope.get("error"),
            "cached": (envelope.get("raw") or {}).get("servedFromCache") is True}


def score_checks(checks, weights=WEIGHTS, layer_names=LAYER_NAMES):
    breakdown, earned, coverage, any_measured = {}, 0, 0, False
    for category, weight in weights.items():
        rows = [c for c in checks if c["category"] == category]
        valid = [c for c in rows if c["scoring_status"] in ("PASS", "FAIL")]
        points = round(weight * sum(c["scoring_status"] == "PASS" for c in valid) / len(valid), 1) if valid else None
        layer_coverage = len(valid) / len(rows) if rows else 0
        coverage += weight * layer_coverage
        if points is not None:
            earned += points
            any_measured = True
        breakdown[category] = {"label": layer_names[category], "points": points, "max_points": weight, "measured": len(valid), "defined": len(rows), "coverage": round(layer_coverage * 100, 1)}
    scale = 100 / sum(weights.values())  # layer weights are relative; the total is always out of 100
    total = round(earned * scale, 1) if any_measured else None
    return {"total": total, "grade": None if total is None else next(g for threshold, g in [(90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")] if total >= threshold),
            "coverage": round(coverage * scale, 1), "breakdown": breakdown, "rubric_version": "pickable-0.2",
            "label": "Preliminary score", "method": "Layer score = PASS / (PASS + FAIL) × layer weight. Unmeasured layers are null; no free points. Warning/UNKNOWN/N/A are excluded; coverage is shown separately."}


def build_report(domain, sources, pending=()):
    """`pending`: sources still running. A check is only shown once no pending source could override it."""
    envelopes = {}
    for source in SOURCES:
        envelope = dict(sources.get(source) or {"status": "unavailable", "raw": None})
        if not isinstance(envelope.get("raw"), dict):
            envelope["raw"] = None
        envelope["status"] = text_value(envelope.get("status")) or "unavailable"
        envelope["observed_at"] = text_value((envelope.get("raw") or {}).get("scannedAt")) or text_value(envelope.get("observed_at"))
        envelopes[source] = envelope
    flat = {s: flatten(s, e.get("raw")) for s, e in envelopes.items()}
    checks, mapped = [], {s: set() for s in envelopes}
    for cid, title, category, ora_id, cf_id, definition in DEFINITIONS:
        observations = {}
        for source, native_id in (("ora", ora_id), ("cloudflare", cf_id)):
            if native_id:
                observations[source] = observation(native_id, flat[source], envelopes[source])
                mapped[source].add(native_id)
        candidates = [s for s in SOURCES if s in observations and observations[s]["eligible"]
                      and observations[s]["status"] in ("PASS", "FAIL")]
        # Cloudflare checks negotiation only; it cannot prove the stricter Vary requirement.
        scope_difference = cid == "content.markdown" and "cloudflare" in candidates
        if scope_difference:
            candidates.remove("cloudflare")
        disagreement = len({observations[s]["status"] for s in candidates}) > 1
        basis = choose_source(candidates, observations)
        status = observations[basis]["status"] if basis else "UNKNOWN"
        reason = f"{SOURCE_NAMES[basis]} reported a definitive result." if basis else NO_EVIDENCE
        if len(candidates) > 1:
            reason += " The newest dated comparable result is used; Ora breaks ties or missing timestamps."
        elif basis == "cloudflare":
            reason += " Ora had no eligible definitive result, so Cloudflare was used as fallback."
        if disagreement:
            reason += " Sources disagree; review both observation times and evidence."
        if scope_difference:
            reason += " Cloudflare does not verify Vary: Accept and cannot score this stricter check."
            ora_time = timestamp(observations["ora"]["observed_at"])
            cf_time = timestamp(observations["cloudflare"]["observed_at"])
            if (basis == "ora" and status == "FAIL" and observations["cloudflare"]["status"] == "PASS"
                    and (envelopes["ora"].get("raw") or {}).get("servedFromCache") is True
                    and ora_time is not None and cf_time is not None and cf_time > ora_time):
                status, basis = "UNKNOWN", None
                reason += " The cached Ora failure predates newer Markdown evidence; recheck the full requirement."
        # A pending source may return a more recent observation, regardless of tie-break priority.
        if any(s in pending for s in observations):
            status, basis, reason = "PENDING", None, "Waiting for source observations before deciding this check."
        checks.append({"check_id": cid, "title": title, "category": category,
                       "measurement_definition": definition, "sources": observations, "combined_status": status if basis else "UNKNOWN",
                       "scoring_status": status, "scoring_source": basis, "reason": reason,
                       "source_disagreement": disagreement, "scope_difference": scope_difference})
    specific = [{"source": s, "source_check_id": cid, "raw": raw} for s, rows in flat.items() for cid, raw in rows.items() if cid not in mapped[s]]
    summaries = []
    for source, envelope in envelopes.items():
        raw = envelope.get("raw") or {}
        summaries.append({"id": source, "status": envelope["status"], "error": text_value(envelope.get("error")),
                          "score": number_value(raw.get("score")), "grade": text_value(raw.get("grade")), "level": number_value(raw.get("level")), "level_name": text_value(raw.get("levelName")),
                          "observed_at": envelope.get("observed_at"),
                          "contract_version": text_value(raw.get("contractVersion")), "check_count": len(flat[source]),
                          "served_from_cache": raw.get("servedFromCache") is True,
                          "result_age_seconds": number_value(raw.get("resultAgeSeconds")),
                          "attribution": "source_reported", "analysis_status": text_value(raw.get("analysisStatus"))})
    recommendations = (envelopes["ora"].get("raw") or {}).get("topFixes")
    if not isinstance(recommendations, list):
        recommendations = []
    covered = set()
    for item in recommendations:
        if isinstance(item, dict) and isinstance(item.get("recommendation"), str):
            if isinstance(item.get("check_id"), str):
                covered.add(item["check_id"])
            for cid, _, _, ora_id, _, _ in DEFINITIONS:
                if ora_id and item.get("id") == ora_id:
                    covered.add(cid)
    for check in checks:
        if check["scoring_status"] == "FAIL" and check["check_id"] not in covered:
            guidance = PICKABLE_GUIDANCE.get(check["check_id"])
            if guidance:
                recommendations = [*recommendations, {"id": "", "source": "pickable",
                                                      "check_id": check["check_id"],
                                                      "recommendation": guidance}]
                covered.add(check["check_id"])
    return {"schema_version": "0.1", "mapping_version": "2026-10-11.1", "domain": domain, "scanned_at": now(),
            "sources": summaries, "checks": checks, "score": {**score_checks(checks), "final": not any(c["scoring_status"] == "PENDING" for c in checks)},
            "source_specific": specific, "recommendations": recommendations,
            "agentready": build_agentready(sources.get("agentready")),
            "marketing": build_marketing(sources.get("growth"), "growth" in pending)}


def build_agentready(envelope):
    """Shadow informational section (Sprint A): parsed from the agentready
    adapter's raw_report summary. Never scored; N/A is never FAIL."""
    envelope = dict(envelope or {"status": "unavailable", "raw": None})
    raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else None
    items = []
    if isinstance(raw, dict):
        found = raw.get("checks")
        if isinstance(found, list):
            for entry in found:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                cid = entry["id"]
                if cid not in AGENTREADY_CHECK_IDS:
                    continue
                status = entry.get("status") if entry.get("status") in (
                    "PASS", "FAIL", "NOT_APPLICABLE", "UNKNOWN", "ERROR") else "UNKNOWN"
                items.append({"check_id": cid, "status": status,
                              "spec_url": AGENTREADY_SPEC_URLS.get(cid),
                              "applicability_note": "Shadow probe; see Sprint A evidence in source run."})
    return {"mode": "shadow", "scored": False,
            "status": text_value(envelope.get("status")) or "unavailable",
            "error": text_value(envelope.get("error")),
            "observed_at": text_value(envelope.get("observed_at")),
            "checks": items,
            "note": "Informational only. Not scored. N/A is not a failure; UNKNOWN/ERROR need retry, not absence claims."}


def build_marketing(envelope, pending):
    envelope = dict(envelope or {"status": "unavailable", "raw": None})
    raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else None
    rows = flatten("growth", raw)
    checks = []
    for cid, title, category, native_id, definition in MARKETING_DEFINITIONS:
        obs = observation(native_id, rows, envelope)
        status = "PENDING" if pending else obs["status"] if obs["eligible"] else "UNKNOWN"
        scored = status in ("PASS", "FAIL")
        checks.append({"check_id": cid, "title": title, "category": category, "measurement_definition": definition,
                       "sources": {"growth": obs}, "scoring_status": status, "scoring_source": "growth" if scored else None,
                       "message": text_value((obs["raw"] or {}).get("message")), "details": (obs["raw"] or {}).get("details")})
    return {"checks": checks, "score": {**score_checks(checks, MARKETING_WEIGHTS, MARKETING_LAYERS), "final": not pending},
            "source": {"id": "growth", "status": text_value(envelope.get("status")) or "unavailable", "error": text_value(envelope.get("error")),
                       "observed_at": text_value((raw or {}).get("scannedAt")) or text_value(envelope.get("observed_at"))}}
