"""Web-source normalization with loss telemetry (which stage dropped each source)."""
from __future__ import annotations

from urllib.parse import urlsplit

from app.security import public_addresses, validate_url


def _source_label(label, url):
    """Human-readable source label; derive from the URL when the provider omits titles.

    Observed production shape for web_search_call.action.sources is
    {"type": "url", "url": ...} with no title key, which used to collapse
    every source to the useless label "Web source".
    """
    text = str(label or "").strip()
    if text:
        return text[:180]
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Web source"
    host = parts.hostname or ""
    if not host:
        return "Web source"
    path = (parts.path or "").rstrip("/")
    if len(path) > 1:
        segs = [s for s in path.split("/") if s][:2]
        if segs:
            return f"{host} — /{'/'.join(segs)}"[:180]
    return host[:180]


def _safe_research_source(label, url, origin):
    """Keep only fetchable public HTTPS sources; return a canonical row or None."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        parts, port = validate_url(url)
        public_addresses(parts.hostname, port)
    except (ValueError, OSError):
        return None
    return {"label": _source_label(label, url), "url": url, "origin": origin}


def extract_research_sources(envelope):
    """Normalize provider search evidence into canonical sources with loss telemetry.

    Reads both inline `url_citation` annotations and `web_search_call` action
    sources (which require `include: ["web_search_call.action.sources"]` on the
    request). Returns (sources, trace); trace attributes every `web_sources: 0`
    to its exact loss stage instead of leaving it to guesswork.
    """
    trace = {"search_calls": 0, "raw_results": 0, "parsed_sources": 0,
             "report_citations": 0, "provider_errors": 0,
             "fallback_reason": None, "latency_ms": None}
    cited, searched = [], []
    items = envelope.get("output", []) if isinstance(envelope, dict) else []
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            trace["search_calls"] += 1
            action = item.get("action")
            action = action if isinstance(action, dict) else {}
            sources = action.get("sources", [])
            if not isinstance(sources, list):
                continue
            for source in sources:
                if not isinstance(source, dict):
                    continue
                trace["raw_results"] += 1
                row = _safe_research_source(source.get("title"), source.get("url"), "search")
                if row:
                    searched.append(row)
                    trace["parsed_sources"] += 1
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            annotations = part.get("annotations", [])
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    continue
                # Documented shape: {"type": "url_citation", "url": ..., "title": ...}.
                # Also accept a nested {"url_citation": {...}} payload defensively.
                nested = annotation.get("url_citation")
                if isinstance(nested, dict):
                    url, title = nested.get("url"), nested.get("title")
                elif annotation.get("type") == "url_citation":
                    url, title = annotation.get("url"), annotation.get("title")
                else:
                    continue
                if not isinstance(url, str):
                    continue
                trace["raw_results"] += 1
                row = _safe_research_source(title, url, "citation")
                if row:
                    cited.append(row)
                    trace["parsed_sources"] += 1
    merged = list({row["url"]: row for row in [*cited, *searched]}.values())[:12]
    trace["report_citations"] = len(merged)
    if not merged:
        if trace["raw_results"] > 0:
            trace["fallback_reason"] = "all_sources_filtered"
        elif trace["search_calls"] == 0:
            trace["fallback_reason"] = "no_search_calls"
        else:
            trace["fallback_reason"] = "no_annotations_no_sources"
    return merged, trace
