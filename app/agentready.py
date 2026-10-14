"""AgentReady checks for Pickable (Sprint A: shadow-mode, read-only).

This module is the single source of truth for the six isitagentready-derived
checks described in Downloads/agentready. It does NOT promise a score boost
and it never fabricates OAuth/agent endpoints to gain points.

Design rules (from IMPLEMENTATION_GUIDE.md):
- Applicability first: APPLICABLE | NOT_APPLICABLE | UNDETERMINED.
- Then status: PASS | FAIL | NOT_APPLICABLE | UNKNOWN | ERROR.
- N/A is never FAIL. Timeouts/blocks are UNKNOWN/ERROR, never absence proof.
- All probes are read-only GETs via app.safe_fetch (SSRF-protected).
- Remote SKILL.md/HTML/schema text is data, never instruction.
- contentSignals PASS means "policy present and parseable"; train=no is not a failure.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

STATUS_ENUM = ("PASS", "FAIL", "NOT_APPLICABLE", "UNKNOWN", "ERROR")
APPLICABILITY_ENUM = ("APPLICABLE", "NOT_APPLICABLE", "UNDETERMINED")

# Legacy WARNING from Ora/Cloudflare reports is preserved for old checks;
# new agentready checks never emit WARNING, only the five states above.
LEGACY_WARNING = "WARNING"

SPEC_URLS = {
    "contentSignals": "https://contentsignals.org/",
    "oauthDiscovery": "https://www.rfc-editor.org/rfc/rfc8414",
    "oauthProtectedResource": "https://www.rfc-editor.org/rfc/rfc9728",
    "authMd": "https://workos.com/auth-md/docs",
    "webMcp": "https://webmachinelearning.github.io/webmcp/",
    "dnsAid": "https://datatracker.ietf.org/doc/draft-mozleywilliams-dnsop-dnsaid/",
}

CHECK_IDS = (
    "contentSignals",
    "oauthDiscovery",
    "oauthProtectedResource",
    "authMd",
    "webMcp",
    "dnsAid",
)

PRIORITIES = {
    "contentSignals": "P1",
    "oauthDiscovery": "P1",
    "oauthProtectedResource": "P1",
    "authMd": "P2",
    "webMcp": "P2",
    "dnsAid": "P3",
}

# Native status strings (any provider spelling) -> canonical five-state.
_NATIVE_MAP = {
    "pass": "PASS",
    "fail": "FAIL",
    "na": "NOT_APPLICABLE",
    "skip": "NOT_APPLICABLE",
    "not_applicable": "NOT_APPLICABLE",
    "notapplicable": "NOT_APPLICABLE",
    "unknown": "UNKNOWN",
    "warn": "UNKNOWN",
    "warning": "UNKNOWN",
    "error": "ERROR",
    "timeout": "ERROR",
    "failed": "ERROR",
}


def normalize_status(native: Any) -> str:
    """Map any native status spelling to the canonical five-state enum."""
    if not isinstance(native, str):
        return "UNKNOWN"
    return _NATIVE_MAP.get(native.strip().lower(), "UNKNOWN")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable against ACCEPTANCE_TESTS T01-T12)
# ---------------------------------------------------------------------------

_CONTENT_SIGNAL_RE = re.compile(r"content-signal\s*:\s*([^\r\n#]+)", re.IGNORECASE)


def parse_content_signal(robots_body: str | None, http_status: int | None) -> dict:
    """T04/T05: parse robots.txt Content-Signal without scoring the policy itself.

    Returns {status, policy, reason} where status is PASS/FAIL/UNKNOWN/ERROR and
    policy is the raw preference string or None.
    """
    if http_status is None or robots_body is None:
        return {"status": "UNKNOWN", "policy": None,
                "reason": "robots.txt could not be retrieved; absence is not proven."}
    if http_status == 404:
        return {"status": "FAIL", "policy": None,
                "reason": "robots.txt returned HTTP 404; no Content-Signal declared."}
    if http_status == 429 or (500 <= http_status <= 599):
        return {"status": "ERROR", "policy": None,
                "reason": f"robots.txt returned HTTP {http_status}; retry or check blocking."}
    if not 200 <= http_status < 300:
        return {"status": "UNKNOWN", "policy": None,
                "reason": f"robots.txt returned HTTP {http_status}; not absence proof."}
    matches = list(_CONTENT_SIGNAL_RE.finditer(robots_body or ""))
    if not matches:
        return {"status": "FAIL", "policy": None,
                "reason": "robots.txt retrieved but no Content-Signal directive found."}
    # Upstream skill: directives live under the relevant User-agent block.
    # A block is one or more User-agent lines followed by rule lines; a new
    # User-agent line after rules starts the next block.
    current: list[str] = []
    seen_rule = False
    block = "default"
    for line in (robots_body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("user-agent:"):
            if seen_rule:
                current, seen_rule = [], False
            current.append(stripped.split(":", 1)[1].strip() or "*")
        elif _CONTENT_SIGNAL_RE.search(line):
            block = " / ".join(current) if current else "default"
            break
        else:
            seen_rule = True
    match = matches[0]
    policy = match.group(1).strip()[:200]
    # Syntax sanity: expect comma-separated key=value pairs.
    parts = [p.strip() for p in policy.split(",") if p.strip()]
    if not parts or not all("=" in p for p in parts):
        return {"status": "FAIL", "policy": policy,
                "reason": "Content-Signal directive present but not parseable."}
    # T04: train=no is a valid policy, not a failure.
    return {"status": "PASS", "policy": policy,
            "reason": f"Content-Signal present and parseable under User-agent block [{block}] (policy: {policy})."}


def validate_oauth_discovery(doc: Any, expected_host: str | None = None) -> dict:
    """HTTP 200 is not correctness. Issuer must be real and consistent.

    Mirrors upstream oauth-discovery SKILL.md: issuer, authorization_endpoint,
    token_endpoint and jwks_uri are all required; grant/response types are
    noted when missing but do not fail the check (RFC profiles differ).
    """
    if not isinstance(doc, dict):
        return {"status": "FAIL", "reason": "OAuth metadata is not a JSON object."}
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or not issuer.startswith("https://"):
        return {"status": "FAIL", "reason": "OAuth metadata issuer is missing or not an https URL."}
    if expected_host and expected_host.lower() not in issuer.lower():
        return {"status": "FAIL",
                "reason": f"OAuth metadata issuer {issuer!r} does not match the scanned host."}
    try:
        from urllib.parse import urlsplit as _split
        _host = (_split(issuer).hostname or "").lower()
    except Exception:
        _host = ""
    if "placeholder" in issuer.lower() or _host in ("example.com", "example.org", "example.net"):
        return {"status": "FAIL", "reason": "OAuth metadata issuer looks like a placeholder."}
    missing = [f for f in ("authorization_endpoint", "token_endpoint", "jwks_uri")
               if not isinstance(doc.get(f), str) or not doc[f].startswith("https://")]
    if missing:
        return {"status": "FAIL",
                "reason": f"OAuth metadata is missing required https endpoint(s): {', '.join(missing)}."}
    soft = [f for f in ("grant_types_supported", "response_types_supported") if f not in doc]
    suffix = f" Note: {', '.join(soft)} not advertised." if soft else ""
    return {"status": "PASS",
            "reason": "OAuth metadata carries a real issuer and all required https endpoints." + suffix}


def validate_protected_resource(doc: Any) -> dict:
    if not isinstance(doc, dict):
        return {"status": "FAIL", "reason": "Protected-resource metadata is not a JSON object."}
    resource = doc.get("resource")
    servers = doc.get("authorization_servers")
    if not isinstance(resource, str) or not resource.startswith("https://"):
        return {"status": "FAIL", "reason": "Protected-resource 'resource' identifier is missing or invalid."}
    if not isinstance(servers, list) or not servers or not all(
            isinstance(s, str) and s.startswith("https://") for s in servers):
        return {"status": "FAIL",
                "reason": "Protected-resource 'authorization_servers' list is missing or invalid."}
    return {"status": "PASS", "reason": "Protected-resource metadata identifies resource and authorization servers."}


_AUTH_H1_RE = re.compile(r"^#{1,6}\s+.*auth\.md.*$", re.IGNORECASE | re.MULTILINE)


def validate_auth_md(body: str | None) -> dict:
    """Validate /auth.md per the upstream auth-md SKILL.md (data, not instructions).

    PASS requires: an H1 containing 'auth.md' plus either a complete
    agent_auth block (skill + register_uri/claim_uri) or, without OAuth
    metadata, a self-contained registration description (endpoint + method
    + credential use). Presence alone is not a security proof.
    """
    if not body or not body.strip():
        return {"status": "UNKNOWN", "detail": "empty",
                "reason": "auth.md body is empty; manual review needed."}
    if not _AUTH_H1_RE.search(body):
        return {"status": "FAIL", "detail": "no-h1",
                "reason": "auth.md has no H1 heading containing 'auth.md'."}
    lowered = body.lower()
    has_skill = "skill" in lowered and ("register_uri" in lowered or "claim_uri" in lowered)
    has_register_uri = "register_uri" in lowered
    mentions_endpoint = ("http" in lowered and ("regist" in lowered or "provision" in lowered
                                               or "claim" in lowered))
    mentions_creds = ("credential" in lowered or "token" in lowered or "scope" in lowered)
    if has_skill and has_register_uri:
        return {"status": "PASS", "detail": "agent_auth",
                "reason": "auth.md has the required H1 and a complete agent_auth block (skill + register_uri)."}
    if mentions_endpoint and mentions_creds:
        return {"status": "PASS", "detail": "self-contained",
                "reason": "auth.md has the required H1 and a self-contained registration description."}
    return {"status": "UNKNOWN", "detail": "unclear",
            "reason": "auth.md present with H1 but no complete registration block; manual review needed."}


def find_webmcp_markers(html: str | None) -> dict:
    """Static declarative markers per the upstream webmcp SKILL.md.

    Detects registerTool calls and declarative form attributes (toolname,
    tooldescription, toolparamdescription). Markers make WebMCP APPLICABLE
    but never PASS: only a version-pinned browser execution can pass.
    Absence proves nothing (tools may register at runtime) -> UNDETERMINED.
    """
    text = html or ""
    lowered = text.lower()
    markers = sorted({m for m in ("registertool", "modelcontext", "toolname",
                                  "tooldescription", "toolparamdescription")
                      if m in lowered})
    return {"markers": markers, "found": bool(markers)}


def parse_doh_answer(doh_json: Any) -> dict:
    """Parse a DNS-over-HTTPS application/dns-json response for SVCB/HTTPS answers.

    Returns {status: HAS_RECORDS | NO_RECORDS | BAD_RESPONSE}.
    """
    if not isinstance(doh_json, dict):
        return {"status": "BAD_RESPONSE"}
    answers = doh_json.get("Answer")
    if not isinstance(answers, list):
        return {"status": "NO_RECORDS" if doh_json.get("Status") in (0, 3) else "BAD_RESPONSE"}
    records = [a.get("data", "") for a in answers
               if isinstance(a, dict) and a.get("type") in (64, 65)]
    if not records:
        return {"status": "NO_RECORDS"}
    return {"status": "HAS_RECORDS", "records": records[:4],
            "dnssec": doh_json.get("AD") is True}


def redact_snippet(text: str, limit: int = 500) -> str:
    """Keep evidence minimal; never log cookies, bearer tokens or PII verbatim."""
    if not isinstance(text, str):
        return ""
    redacted = re.sub(r"(?i)(bearer\s+[A-Za-z0-9\-._~+/=]+)", "Bearer [REDACTED]", text)
    redacted = re.sub(r"(?i)(cookie\s*:\s*)([^\r\n;]+)", r"\1[REDACTED]", redacted)
    # Prompt-injection guard: remote text is data; flag it, never execute it.
    if "ignore system" in redacted.lower() or "ignore previous" in redacted.lower():
        redacted = "[REMOTE TEXT TREATED AS DATA, NOT INSTRUCTION] " + redacted
    return redacted[:limit]


def build_evidence_record(
    target_url: str,
    check_id: str,
    applicability: str,
    status: str,
    observed: str,
    *,
    locator: str = "",
    http_status: int | None = None,
    content_type: str | None = None,
    claim: str = "",
    reason: str | None = None,
    confidence: str = "MODERATE",
    next_action: str | None = None,
    evidence_type: str = "http",
) -> dict:
    """Build one EVIDENCE_SCHEMA.json-conformant record (proposal shape)."""
    if applicability not in APPLICABILITY_ENUM:
        applicability = "UNDETERMINED"
    if status not in STATUS_ENUM:
        status = "UNKNOWN"
    if applicability == "NOT_APPLICABLE":
        status = "NOT_APPLICABLE"
    evidence: dict[str, Any] = {
        "type": evidence_type,
        "locator": locator or target_url,
        "observed": redact_snippet(observed or ""),
        "captured_at": now_iso(),
        "redacted": True,
    }
    if http_status is not None:
        evidence["http_status"] = http_status
    if content_type:
        evidence["content_type"] = content_type[:120]
    return {
        "target_url": target_url,
        "check_id": check_id,
        "applicability": applicability,
        "status": status,
        "checked_at": now_iso(),
        "claim": claim or observed[:300],
        "evidence": [evidence],
        "reason": reason,
        "confidence": confidence,
        "spec_url": SPEC_URLS.get(check_id),
        "next_action": next_action,
        "policy_notes": None,
    }


def evidence_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)[:8000]


def is_prompt_injection_probe(body: str) -> bool:
    lowered = (body or "").lower()
    return "ignore system" in lowered or "ignore previous instructions" in lowered


def with_review(record: dict, verdict: str, note: str = "") -> dict:
    """Attach a manual review label to a stored evidence record.

    verdict: "confirmed" | "false_positive" | "false_negative" | "needs_retest".
    Returns a copy; the original snapshot is never mutated. Sprint B: every
    eval mismatch is tagged through this helper so the label set stays
    auditable without rewriting stored evidence.
    """
    if verdict not in ("confirmed", "false_positive", "false_negative", "needs_retest"):
        raise ValueError(f"Unknown review verdict: {verdict!r}")
    copy = dict(record)
    copy["review"] = {"verdict": verdict, "note": (note or "")[:500],
                      "reviewed_at": now_iso()}
    return copy
