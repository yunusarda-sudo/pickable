"""Synthetic provider payloads, explicitly labelled for UI tests only."""
from copy import deepcopy

ORA = {
    "contractVersion": "1.27.0", "score": 61, "grade": "D", "scannedAt": "2026-10-10T12:00:00Z",
    "analysisStatus": "complete", "pendingChecks": [],
    "layers": [{"id": "accessibility", "checks": [
        {"id": "sitemap", "name": "Sitemap exists", "status": "pass", "details": "TEST: XML sitemap found."},
        {"id": "link-headers-discovery", "name": "HTTP Link headers", "status": "fail", "details": "TEST: No Link header."},
        {"id": "llms-txt-exists", "name": "llms.txt exists", "status": "pass", "details": "TEST: Found /.well-known/llms.txt."},
        {"id": "markdown-negotiation-vary", "name": "Markdown negotiation", "status": "warning", "details": "TEST: Vary: Accept missing."},
        {"id": "mcp-oauth-metadata", "name": "MCP OAuth metadata", "status": "pass", "details": "TEST: OAuth metadata found at MCP host."},
        {"id": "oauth-protected-resource", "name": "Protected Resource", "status": "na", "details": "TEST: Authentication not assessed."},
        {"id": "mcp-server-card", "name": "MCP server card", "status": "fail", "details": "TEST: Card absent."},
        {"id": "future-check", "name": "Ek içerik kontrolü", "status": "warning", "details": "<img src=x onerror=alert('xss')> TEST: must remain plain text."},
    ]}],
    "topFixes": [{"id": "link-headers-discovery", "recommendation": "TEST: Link header ekleyin."}],
}
OSS = {
    "scanner": "agent_ready_scan.py 1.0.0", "level": 1, "levelName": "Basic Web Presence",
    "scannedAt": "2026-10-10T12:00:01Z", "checks": {
        "discoverability": {
            "sitemap": {"title": "XML sitemap", "status": "pass", "message": "TEST: Valid sitemap", "evidence": [{"label": "GET /sitemap.xml", "outcome": "positive", "summary": "HTTP 200"}]},
            "linkHeaders": {"title": "Link headers", "status": "pass", "message": "TEST: Different observation", "details": {"header": "</sitemap.xml>; rel=sitemap"}},
        },
        "extended": {"llmsTxt": {"title": "llms.txt", "status": "fail", "message": "TEST: No root /llms.txt"}},
        "contentAccessibility": {"markdownNegotiation": {"title": "Markdown", "status": "warn", "message": "TEST: Explicit markdown route only"}},
        "discovery": {
            "oauthDiscovery": {"title": "OAuth metadata", "status": "fail", "message": "TEST: No root metadata"},
            "oauthProtectedResource": {"title": "Protected resource", "status": "skip", "message": "TEST: Not applicable"},
            "mcpServerCard": {"title": "Server card", "status": "error", "message": "TEST: Request timeout"},
        },
    },
}


def fixture_runs():
    return [{"source_id": source, "status": "completed", "finished_at": raw["scannedAt"], "raw_report": deepcopy(raw)}
            for source, raw in (("ora", ORA), ("agentready_oss", OSS))]
