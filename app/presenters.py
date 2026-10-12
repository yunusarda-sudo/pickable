"""Customer-facing projections. No provider payloads or source blobs leave the server."""

from __future__ import annotations


def public_report(report):
    """Return the customer report without provider payloads or source evidence blobs."""
    if not isinstance(report, dict):
        return None
    checks = []
    for check in report.get("checks", []) if isinstance(report.get("checks"), list) else []:
        if not isinstance(check, dict):
            continue
        checks.append({key: check.get(key) for key in
                       ("check_id", "title", "category", "measurement_definition", "scoring_status", "scoring_source", "combined_status")})
    recommendations = []
    for item in report.get("recommendations", []) if isinstance(report.get("recommendations"), list) else []:
        if isinstance(item, str):
            recommendations.append({"id": "", "recommendation": item[:3000], "check_id": None})
        elif isinstance(item, dict) and isinstance(item.get("recommendation"), str):
            source_id = item.get("id") if isinstance(item.get("id"), str) else ""
            check = next((row for row in report.get("checks", []) if isinstance(row, dict) and any(
                isinstance(obs, dict) and obs.get("source_check_id") == source_id
                for obs in (row.get("sources") or {}).values())), None)
            entry = {"id": source_id, "recommendation": item["recommendation"][:3000],
                     "check_id": check.get("check_id") if check else item.get("check_id")}
            if item.get("source") == "pickable":
                entry["source"] = "pickable"
            recommendations.append(entry)
    marketing = report.get("marketing")
    if isinstance(marketing, dict):
        source = marketing.get("source")
        marketing_score = marketing.get("score")
        if isinstance(marketing_score, dict):
            marketing_score = {key: marketing_score.get(key) for key in ("total", "grade", "coverage", "breakdown", "final", "label")}
        marketing_checks = []
        for check in marketing.get("checks", []) if isinstance(marketing.get("checks"), list) else []:
            if not isinstance(check, dict):
                continue
            raw_details = check.get("details")
            profiles = raw_details.get("profiles") if isinstance(raw_details, dict) else None
            safe_profiles = {str(name): url for name, url in profiles.items()
                             if isinstance(name, str) and isinstance(url, str)} if isinstance(profiles, dict) else {}
            marketing_checks.append({key: check.get(key) for key in
                                     ("check_id", "title", "category", "scoring_status", "scoring_source", "message", "measurement_definition")}
                                    | {"details": {"profiles": safe_profiles} if safe_profiles else None})
        marketing = {"score": marketing_score,
                     "source": {key: source.get(key) for key in ("id", "status", "observed_at")}
                     if isinstance(source, dict) else None,
                     "checks": marketing_checks}
    score = report.get("score")
    if isinstance(score, dict):
        score = {key: score.get(key) for key in ("total", "grade", "coverage", "breakdown", "final", "label")}
    return {"schema_version": report.get("schema_version"),
            "domain": report.get("domain"), "scanned_at": report.get("scanned_at"),
            "checks": checks, "score": score if isinstance(score, dict) else None, "recommendations": recommendations,
            "marketing": marketing}


def public_job(job):
    if not isinstance(job, dict):
        return None
    return {"id": job.get("id"), "domain": job.get("domain"), "created_at": job.get("created_at"),
            "status": job.get("status"), "sources": [{key: source.get(key) for key in ("id", "status")}
                                                       for source in job.get("sources", []) if isinstance(source, dict)],
            "report": public_report(job.get("report")),
            "intelligence": public_intelligence(job.get("intelligence"))}


def public_intelligence(job):
    """Strict allowlist for customer-facing product analysis.

    Only the lifecycle status and the report content leave the server.
    Model names, token usage, finish reasons and raw error text stay in
    backend logs and internal diagnostics, never in user payloads.
    """
    if not isinstance(job, dict): return None
    report = job.get("report")
    safe_report = None
    if isinstance(report, dict):
        keys = ("analysis_stage", "product_summary", "target_customers", "problem", "value_proposition", "competitive_landscape", "workflows", "evidence_refs", "unknowns", "sources", "web_sources", "research_note", "research_trace")
        safe_report = {key: report[key] for key in keys if key in report}
        safe_report["sources"] = [{"label": row.get("label"), "url": row.get("url"), "retrieved_at": row.get("retrieved_at"), "sha256": row.get("sha256")}
                                   for row in safe_report.get("sources", []) if isinstance(row, dict) and isinstance(row.get("url"), str)]
        safe_report["web_sources"] = [{"label": row.get("label"), "url": row.get("url")}
                                       for row in safe_report.get("web_sources", []) if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"].startswith("https://")][:12]
        trace = safe_report.get("research_trace")
        if isinstance(trace, dict):
            safe_report["research_trace"] = {
                key: trace.get(key) for key in
                ("search_calls", "raw_results", "parsed_sources", "report_citations", "fallback_reason", "latency_ms")
                if isinstance(trace.get(key), int) or (key == "fallback_reason" and isinstance(trace.get(key), str))} or None
        else:
            safe_report.pop("research_trace", None)
    status = job.get("status")
    return {"id": job.get("id"), "depth": job.get("depth"), "status": status,
            "created_at": job.get("created_at"), "finished_at": job.get("finished_at"), "report": safe_report}
