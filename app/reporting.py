"""Versioned, conservative mappings; native evidence is never rewritten."""
from datetime import datetime, timezone
import json

SOURCES = ("ora", "cloudflare")  # scoring preference order: Cloudflare fills checks Ora leaves undecided
SOURCE_NAMES = {"ora": "Ora", "cloudflare": "Cloudflare"}
WEIGHTS = {"discovery": 20, "content": 25, "usability": 35, "security": 10, "payments": 10}
LAYER_NAMES = {"discovery": "Discoverability", "content": "Readability", "usability": "Usability", "security": "Security & compliance", "payments": "Payments"}
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
NORMALIZED = {"pass": "PASS", "fail": "FAIL", "warn": "WARNING", "warning": "WARNING", "na": "NOT_APPLICABLE", "skip": "NOT_APPLICABLE", "not_applicable": "NOT_APPLICABLE"}
NO_EVIDENCE = "No definitive PASS/FAIL evidence. Warnings, N/A and access errors are excluded from the score."


def text_value(value):
    return value if isinstance(value, str) else None


def number_value(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def now():
    return datetime.now(timezone.utc).isoformat()


def report_from_job(job, source_runs, pending=()):
    sources = {}
    for run in source_runs:
        source = run.get("source_id")
        if source not in SOURCES:
            continue
        raw, error = run.get("raw_report"), run.get("error")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw, error = None, "The stored provider report could not be read as JSON."
        if raw is not None and not isinstance(raw, dict):
            raw, error = None, "The provider report is not a JSON object."
        observed_at = None
        if isinstance(raw, dict) and raw.get("scannedAt"):
            observed_at = raw.get("scannedAt")
        else:
            observed_at = run.get("finished_at")
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
            "raw": raw, "observed_at": envelope.get("observed_at"), "attribution": "source_reported"}


def score_checks(checks):
    breakdown, earned, coverage, any_measured = {}, 0, 0, False
    for category, weight in WEIGHTS.items():
        rows = [c for c in checks if c["category"] == category]
        valid = [c for c in rows if c["scoring_status"] in ("PASS", "FAIL")]
        points = round(weight * sum(c["scoring_status"] == "PASS" for c in valid) / len(valid), 1) if valid else None
        layer_coverage = len(valid) / len(rows) if rows else 0
        coverage += weight * layer_coverage
        if points is not None:
            earned += points
            any_measured = True
        breakdown[category] = {"label": LAYER_NAMES[category], "points": points, "max_points": weight, "measured": len(valid), "defined": len(rows), "coverage": round(layer_coverage * 100, 1)}
    total = round(earned, 1) if any_measured else None
    return {"total": total, "grade": None if total is None else next(g for threshold, g in [(90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")] if total >= threshold),
            "coverage": round(coverage, 1), "breakdown": breakdown, "rubric_version": "pickable-0.1",
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
        basis = next((s for s in SOURCES if s in observations and observations[s]["status"] in ("PASS", "FAIL")), None)
        status = observations[basis]["status"] if basis else observations["ora"]["status"]
        reason = f"{SOURCE_NAMES[basis]} reported a definitive result." if basis else NO_EVIDENCE
        if basis == "cloudflare":
            reason += " Ora had no definitive result, so Cloudflare was used as fallback."
        # Undecided until every source that outranks the basis (or every source, if none decided) has finished.
        if any(s in pending and s in observations for s in (SOURCES[:SOURCES.index(basis)] if basis else SOURCES)):
            status, basis, reason = "PENDING", None, "Waiting for a higher-priority source before deciding this check."
        checks.append({"check_id": cid, "title": title, "category": category,
                       "measurement_definition": definition, "sources": observations, "combined_status": status if basis else "UNKNOWN",
                       "scoring_status": status, "scoring_source": basis, "reason": reason})
    specific = [{"source": s, "source_check_id": cid, "raw": raw} for s, rows in flat.items() for cid, raw in rows.items() if cid not in mapped[s]]
    summaries = []
    for source, envelope in envelopes.items():
        raw = envelope.get("raw") or {}
        summaries.append({"id": source, "status": envelope["status"], "error": text_value(envelope.get("error")),
                          "score": number_value(raw.get("score")), "grade": text_value(raw.get("grade")), "level": number_value(raw.get("level")), "level_name": text_value(raw.get("levelName")),
                          "observed_at": envelope.get("observed_at"),
                          "contract_version": text_value(raw.get("contractVersion")), "check_count": len(flat[source]),
                          "attribution": "source_reported", "analysis_status": text_value(raw.get("analysisStatus"))})
    recommendations = (envelopes["ora"].get("raw") or {}).get("topFixes")
    if not isinstance(recommendations, list):
        recommendations = []
    return {"schema_version": "0.1", "mapping_version": "2026-10-10.2", "domain": domain, "scanned_at": now(),
            "sources": summaries, "checks": checks, "score": {**score_checks(checks), "final": not any(c["scoring_status"] == "PENDING" for c in checks)},
            "source_specific": specific, "recommendations": recommendations}
