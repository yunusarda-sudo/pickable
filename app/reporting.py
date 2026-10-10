"""Versioned, conservative mappings; native evidence is never rewritten."""
from datetime import datetime, timezone
import json

WEIGHTS = {"discovery": 20, "content": 25, "usability": 35, "security": 10, "payments": 10}
LAYER_NAMES = {"discovery": "Keşfedilebilirlik", "content": "Anlaşılabilirlik", "usability": "Kullanılabilirlik", "security": "Güvenlik & uyum", "payments": "Ödeme"}
# This is a product rubric, not a provider-score average or calibrated confidence.
DEFINITIONS = [
    ("discovery.sitemap", "XML sitemap", "discovery", "sitemap", "Sitemap bulunabilirliği."),
    ("discovery.link_headers", "HTTP Link header", "discovery", "link-headers-discovery", "Makine keşfine yönelik HTTP Link header sinyali."),
    ("content.llms_txt", "llms.txt", "content", "llms-txt-exists", "llms.txt dosyasının varlığı (kök veya alternatif yol)."),
    ("content.markdown", "Markdown sunumu", "content", "markdown-negotiation-vary", "Ana sayfada Markdown negotiation ve Vary davranışı."),
    ("auth.oauth_metadata", "OAuth metadata", "usability", "mcp-oauth-metadata", "MCP endpoint'inin OAuth metadata'sı."),
    ("auth.protected_resource", "Protected Resource", "usability", "oauth-protected-resource", "Protected Resource metadata'sı; giriş akışı test edilmez."),
    ("integration.mcp_card", "MCP server card", "usability", "mcp-server-card", "MCP server card keşfi."),
]
NORMALIZED = {"pass": "PASS", "fail": "FAIL", "warn": "WARNING", "warning": "WARNING", "na": "NOT_APPLICABLE", "skip": "NOT_APPLICABLE", "not_applicable": "NOT_APPLICABLE"}


def text_value(value):
    return value if isinstance(value, str) else None


def number_value(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def now():
    return datetime.now(timezone.utc).isoformat()


def report_from_job(job, source_runs):
    sources = {}
    for run in source_runs:
        source = run.get("source_id")
        if source != "ora":
            continue
        raw, error = run.get("raw_report"), run.get("error")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw, error = None, "Sağlayıcının saklanan ham raporu JSON olarak okunamadı."
        if raw is not None and not isinstance(raw, dict):
            raw, error = None, "Sağlayıcı raporu JSON nesnesi değil."
        observed_at = None
        if isinstance(raw, dict) and raw.get("scannedAt"):
            observed_at = raw.get("scannedAt")
        else:
            observed_at = run.get("finished_at")
        sources[source] = {"status": run.get("status", "unavailable"), "raw": raw,
                           "error": error, "observed_at": observed_at}
    return build_report(job["domain"], sources)


def flatten(raw):
    layers = raw.get("layers") if isinstance(raw, dict) else None
    if not isinstance(layers, list):
        return {}
    result = {}
    for layer in layers:
        checks = layer.get("checks") if isinstance(layer, dict) else None
        if not isinstance(checks, list):
            continue
        for c in checks:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                result[c["id"]] = c
    return result


def observation(cid, rows, envelope):
    raw = rows.get(cid)
    native = raw.get("status") if raw else None
    return {"source_check_id": cid, "status": NORMALIZED.get(native, "UNKNOWN") if isinstance(native, str) else "UNKNOWN", "native_status": native,
            "raw": raw, "observed_at": envelope.get("observed_at"), "attribution": "source_reported"}



REASONS = {"PASS": "Ora kesin sonuç bildirdi.", "FAIL": "Ora kesin sonuç bildirdi."}
NO_EVIDENCE = "Kesin PASS/FAIL kanıtı yok. Uyarı, N/A ve erişim hataları puana dahil edilmedi."

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
            "label": "Ön skor", "method": "Katman puanı = PASS / (PASS + FAIL) × katman ağırlığı. Ölçülmeyen katman null; ücretsiz puan yok. Uyarı/UNKNOWN/N/A dışlanır; kapsam ayrıca gösterilir."}


def build_report(domain, sources):
    envelopes = {}
    for source in ("ora",):
        envelope = dict(sources.get(source) or {"status": "unavailable", "raw": None})
        if not isinstance(envelope.get("raw"), dict):
            envelope["raw"] = None
        envelope["status"] = text_value(envelope.get("status")) or "unavailable"
        envelope["observed_at"] = text_value((envelope.get("raw") or {}).get("scannedAt")) or text_value(envelope.get("observed_at"))
        envelopes[source] = envelope
    flat = {s: flatten(e.get("raw")) for s, e in envelopes.items()}
    checks, mapped = [], {s: set() for s in envelopes}
    for cid, title, category, ora_id, definition in DEFINITIONS:
        obs = observation(ora_id, flat["ora"], envelopes["ora"])
        mapped["ora"].add(ora_id)
        status = obs["status"]
        scored = status in ("PASS", "FAIL")
        checks.append({"check_id": cid, "title": title, "category": category,
                       "measurement_definition": definition, "sources": {"ora": obs}, "combined_status": status if scored else "UNKNOWN",
                       "scoring_status": status, "scoring_source": "ora" if scored else None, "reason": REASONS.get(status, NO_EVIDENCE)})
    specific = [{"source": s, "source_check_id": cid, "raw": raw} for s, rows in flat.items() for cid, raw in rows.items() if cid not in mapped[s]]
    summaries = []
    for source, envelope in envelopes.items():
        raw = envelope.get("raw") or {}
        summaries.append({"id": source, "status": envelope["status"], "error": text_value(envelope.get("error")),
                          "score": number_value(raw.get("score")), "grade": text_value(raw.get("grade")), "level": number_value(raw.get("level")), "level_name": text_value(raw.get("levelName")),
                          "observed_at": envelope.get("observed_at"),
                          "contract_version": text_value(raw.get("contractVersion")) or text_value(raw.get("scanner")), "check_count": len(flat[source]),
                          "attribution": "source_reported", "analysis_status": text_value(raw.get("analysisStatus"))})
    recommendations = (envelopes["ora"].get("raw") or {}).get("topFixes")
    if not isinstance(recommendations, list):
        recommendations = []
    return {"schema_version": "0.1", "mapping_version": "2026-10-10.1", "domain": domain, "scanned_at": now(),
            "sources": summaries, "checks": checks, "score": score_checks(checks),
            "source_specific": specific, "recommendations": recommendations}
