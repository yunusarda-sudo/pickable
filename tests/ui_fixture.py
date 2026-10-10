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
        {"id": "future-check", "name": "Extra content check", "status": "warning", "details": "<img src=x onerror=alert('xss')> TEST: must remain plain text."},
    ]}],
    "topFixes": [{"id": "link-headers-discovery", "recommendation": "TEST: Link header ekleyin."}],
}
CLOUDFLARE = {
    "url": "https://example.com", "scannedAt": "2026-10-10T12:00:01Z", "level": 1, "levelName": "Basic Web Presence",
    "checks": {
        "discoverability": {"sitemap": {"status": "pass", "message": "TEST: sitemap.xml exists"},
                            "linkHeaders": {"status": "fail", "message": "TEST: No Link header"}},
        "contentAccessibility": {"markdownNegotiation": {"status": "fail", "message": "TEST: Markdown not negotiated"}},
        "discovery": {"oauthDiscovery": {"status": "fail", "message": "TEST: No OAuth metadata"},
                      "oauthProtectedResource": {"status": "neutral", "message": "TEST: Not applicable"},
                      "mcpServerCard": {"status": "fail", "message": "TEST: Card absent"}},
    },
}


def fixture_runs():
    return [{"source_id": source, "status": "completed", "finished_at": raw["scannedAt"], "raw_report": deepcopy(raw)}
            for source, raw in (("ora", ORA), ("cloudflare", CLOUDFLARE))]
