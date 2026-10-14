"""AgentReady shadow-mode adapter (Sprint A).

Read-only, SSRF-protected probes for the six checks. Runs alongside the
existing Ora/Cloudflare/Growth adapters but never feeds the score:
reporting.build_report only scores DEFINITIONS checks, so agentready rows are
informational until Sprint B validates precision/N/A accuracy.

Safety:
- GET only, via app.safe_fetch (blocks loopback/private/link-local/metadata IP,
  non-standard ports, HTTPS->HTTP downgrade).
- No registration, login, payment, message sending or state change.
- Evidence bodies are truncated + redacted (see app.agentready).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from app.adapters.base import Adapter, AdapterResult, CheckRecord
from app import agentready as ar
from app.safe_fetch import SafeFetchError, safe_fetch


def _fetch(url: str, timeout: int = 12):
    try:
        res = safe_fetch(
            url,
            method="GET",
            headers={"User-Agent": "Pickable/0.1 AgentReady shadow probe", "Accept": "*/*"},
            timeout=timeout,
            max_redirects=4,
            max_bytes=200_000,
        )
        return res, None
    except SafeFetchError as e:
        # SSRF blocks, timeouts, oversize: transport-level, never absence proof.
        return None, f"{type(e).__name__}: {e}"
    except Exception as e:  # keep the adapter total: one probe never kills the scan
        return None, f"{type(e).__name__}: {e}"


def _body_text(res) -> str:
    try:
        return res.body.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _content_type(res) -> str:
    for k, v in (res.headers or {}).items():
        if k.lower() == "content-type":
            return str(v)[:120]
    return ""


def _check_record(job_id: str, check_id: str, record: dict) -> CheckRecord:
    return CheckRecord(
        job_id=job_id,
        source_id="agentready",
        native_check_id=check_id,
        canonical_check_id=f"agentready.{check_id}",
        status=record["status"],
        evidence=ar.evidence_json(record),
        observed_at=record["checked_at"],
    )


class AgentReadyAdapter(Adapter):
    """Shadow adapter. Disabled unless PICKABLE_AGENTREADY_SHADOW=1."""

    source_id = "agentready"

    def __init__(self, timeout: int = 12, enabled: bool = True):
        self.timeout = timeout
        self.enabled = enabled

    async def health(self) -> str:
        return "ok"

    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        loop = asyncio.get_running_loop()
        base = f"https://{domain}"
        target = base + "/"
        observed_at = datetime.now(timezone.utc).isoformat()

        def run() -> AdapterResult:
            checks = [
                self._content_signals(job_id, base, target),
                self._oauth_discovery(job_id, base, target, domain),
                self._protected_resource(job_id, base, target),
                self._auth_md(job_id, base, target),
                self._webmcp_unknown(job_id, target),
                self._dns_aid_unknown(job_id, target, domain),
            ]
            raw = json.dumps({
                "source": "pickable-agentready-shadow",
                "version": "sprint-a-0.1",
                "checks": [
                    {"id": c.native_check_id, "status": c.status} for c in checks
                ],
            })
            return AdapterResult(self.source_id, "completed", raw, checks)

        return await loop.run_in_executor(None, run)

    # -- individual probes -------------------------------------------------

    def _content_signals(self, job_id: str, base: str, target: str) -> CheckRecord:
        url = base + "/robots.txt"
        res, err = _fetch(url, self.timeout)
        if res is None:
            rec = ar.build_evidence_record(
                target, "contentSignals", "UNDETERMINED", "ERROR",
                observed=f"robots.txt fetch failed: {err}",
                locator=url, claim="robots.txt could not be retrieved; no policy claim.",
                reason="Transport/SSRF guard blocked or timed out; not absence proof.",
                confidence="INSUFFICIENT",
                next_action="Retry fetch; do not claim absence from a timeout.",
            )
            return _check_record(job_id, "contentSignals", rec)
        body = _body_text(res)
        parsed = ar.parse_content_signal(body, res.status)
        # robots.txt 404 with no policy: applicable if public content exists.
        # Sprint A is conservative: homepage 200 => APPLICABLE, else UNDETERMINED.
        applicability = "APPLICABLE" if res.status in (200, 404) else "UNDETERMINED"
        status = parsed["status"]
        if applicability == "UNDETERMINED" and status == "FAIL":
            status = "UNKNOWN"
        rec = ar.build_evidence_record(
            target, "contentSignals", applicability, status,
            observed=f"HTTP {res.status}; {parsed['reason']}",
            locator=url, http_status=res.status, content_type=_content_type(res),
            claim=f"Content-Signal policy: {parsed['policy']}" if parsed["policy"] else parsed["reason"],
            reason="Observation (HTTP status) is separate from interpretation (policy present?) and from recommendation.",
            confidence="HIGH" if status in ("PASS", "FAIL") else "LOW",
            next_action=(None if status == "PASS"
                         else "Declare a Content-Signal policy in robots.txt if the publisher wants crawlers to see it. train=no is valid; never force train=yes."),
        )
        return _check_record(job_id, "contentSignals", rec)

    def _oauth_discovery(self, job_id: str, base: str, target: str, domain: str) -> CheckRecord:
        paths = [
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ]
        last_status: Optional[int] = None
        saw_transport_error = False
        for path in paths:
            url = base + path
            res, err = _fetch(url, self.timeout)
            if res is None:
                last_status = None
                saw_transport_error = True
                continue
            last_status = res.status
            if 200 <= res.status < 300 and "json" in _content_type(res).lower():
                try:
                    doc = json.loads(_body_text(res))
                except ValueError:
                    rec = ar.build_evidence_record(
                        target, "oauthDiscovery", "APPLICABLE", "FAIL",
                        observed=f"HTTP 200 at {path} but body is not JSON.",
                        locator=url, http_status=res.status,
                        claim="OAuth metadata endpoint returns non-JSON.",
                        reason="HTTP 200 is not correctness; body must be valid metadata.",
                        next_action="Publish RFC 8414 metadata only if an authorization server exists; never fabricate endpoints.",
                    )
                    return _check_record(job_id, "oauthDiscovery", rec)
                verdict = ar.validate_oauth_discovery(doc, expected_host=domain)
                rec = ar.build_evidence_record(
                    target, "oauthDiscovery", "APPLICABLE", verdict["status"],
                    observed=f"HTTP 200 at {path}; issuer={doc.get('issuer')!r}",
                    locator=url, http_status=res.status, content_type=_content_type(res),
                    claim=verdict["reason"], reason=verdict["reason"],
                    confidence="HIGH" if verdict["status"] == "PASS" else "MODERATE",
                    next_action=(None if verdict["status"] == "PASS"
                                 else "Publish verifiable RFC 8414 / OIDC metadata only for the real authorization server."),
                )
                return _check_record(job_id, "oauthDiscovery", rec)
            if res.status == 404:
                continue
            if 200 <= res.status < 300:
                # Endpoint answered 200 but did not serve JSON metadata:
                # applicable and broken (HTTP 200 is not correctness).
                rec = ar.build_evidence_record(
                    target, "oauthDiscovery", "APPLICABLE", "FAIL",
                    observed=f"HTTP 200 at {path} but Content-Type is {(_content_type(res) or 'missing')!r}, not JSON metadata.",
                    locator=url, http_status=res.status, content_type=_content_type(res),
                    claim="OAuth metadata endpoint exists but does not serve valid metadata.",
                    reason="HTTP 200 is not correctness; body must be valid RFC 8414 / OIDC metadata.",
                    next_action="Publish RFC 8414 metadata only if an authorization server exists; never fabricate endpoints.",
                )
                return _check_record(job_id, "oauthDiscovery", rec)
            # Non-404 non-JSON (e.g. 403/429/5xx): cannot decide applicability.
            rec = ar.build_evidence_record(
                target, "oauthDiscovery", "UNDETERMINED", "ERROR" if res.status >= 500 else "UNKNOWN",
                observed=f"HTTP {res.status} at {path}; no JSON metadata.",
                locator=url, http_status=res.status,
                claim="OAuth metadata endpoint access was blocked or errored.",
                reason="Access problem is not absence proof.",
                confidence="INSUFFICIENT",
                next_action="Retry; only claim NOT_APPLICABLE after confirming no authorization server is advertised.",
            )
            return _check_record(job_id, "oauthDiscovery", rec)
        # All paths 404 or unreachable: most marketing sites have no AS -> NOT_APPLICABLE (T01).
        # But transport errors (T11/SSRF/timeout) are never absence proof.
        if saw_transport_error and last_status is None:
            rec = ar.build_evidence_record(
                target, "oauthDiscovery", "UNDETERMINED", "UNKNOWN",
                observed="OAuth metadata endpoints unreachable (transport/SSRF/timeout); no 404 observed.",
                locator=base + paths[0],
                claim="Could not determine whether an authorization server exists.",
                reason="T11: transport block/timeout is not absence proof.",
                confidence="INSUFFICIENT",
                next_action="Retry; only claim NOT_APPLICABLE after a clean 404 on standard paths.",
            )
            return _check_record(job_id, "oauthDiscovery", rec)
        # Conservative: 404 on both well-known paths => NOT_APPLICABLE with LOW confidence
        # (a non-standard path AS is still possible; Sprint B adds deeper hints).
        rec = ar.build_evidence_record(
            target, "oauthDiscovery", "NOT_APPLICABLE", "NOT_APPLICABLE",
            observed=f"Both well-known OAuth metadata paths 404/unreachable (last={last_status}).",
            locator=base + paths[0],
            claim="No OAuth/OIDC authorization server advertised at standard metadata paths.",
            reason="T01: missing auth metadata on a non-OAuth site is not a failure.",
            confidence="LOW",
            next_action="None unless the product actually operates an authorization server.",
        )
        return _check_record(job_id, "oauthDiscovery", rec)

    def _protected_resource(self, job_id: str, base: str, target: str) -> CheckRecord:
        url = base + "/.well-known/oauth-protected-resource"
        res, err = _fetch(url, self.timeout)
        if res is None:
            rec = ar.build_evidence_record(
                target, "oauthProtectedResource", "UNDETERMINED", "UNKNOWN",
                observed=f"Protected-resource metadata fetch failed: {err}",
                locator=url,
                claim="Could not determine whether an OAuth-protected resource exists.",
                reason="Transport failure is not absence proof.",
                confidence="INSUFFICIENT",
                next_action="Retry; check 401 WWW-Authenticate hints on the real API before claiming N/A.",
            )
            return _check_record(job_id, "oauthProtectedResource", rec)
        if res.status == 404:
            rec = ar.build_evidence_record(
                target, "oauthProtectedResource", "NOT_APPLICABLE", "NOT_APPLICABLE",
                observed="HTTP 404 at oauth-protected-resource metadata path.",
                locator=url, http_status=404,
                claim="No OAuth-protected resource metadata advertised.",
                reason="Resource does not use OAuth bearer tokens, or metadata is unpublished; not a failure by itself.",
                confidence="LOW",
                next_action="Only publish RFC 9728 metadata for genuinely OAuth-protected APIs/MCP resources.",
            )
            return _check_record(job_id, "oauthProtectedResource", rec)
        if not (200 <= res.status < 300):
            rec = ar.build_evidence_record(
                target, "oauthProtectedResource", "UNDETERMINED",
                "ERROR" if res.status >= 500 else "UNKNOWN",
                observed=f"HTTP {res.status} at protected-resource metadata path.",
                locator=url, http_status=res.status,
                claim="Protected-resource metadata endpoint errored or blocked.",
                reason="Access problem is not absence proof.",
                confidence="INSUFFICIENT",
            )
            return _check_record(job_id, "oauthProtectedResource", rec)
        try:
            doc = json.loads(_body_text(res))
        except ValueError:
            rec = ar.build_evidence_record(
                target, "oauthProtectedResource", "APPLICABLE", "FAIL",
                observed="HTTP 200 but body is not JSON.",
                locator=url, http_status=res.status,
                claim="Protected-resource metadata is not valid JSON.",
                reason="HTTP 200 is not correctness.",
            )
            return _check_record(job_id, "oauthProtectedResource", rec)
        verdict = ar.validate_protected_resource(doc)
        rec = ar.build_evidence_record(
            target, "oauthProtectedResource", "APPLICABLE", verdict["status"],
            observed=f"resource={doc.get('resource')!r} servers={doc.get('authorization_servers')!r}",
            locator=url, http_status=res.status, content_type=_content_type(res),
            claim=verdict["reason"], reason=verdict["reason"],
            confidence="HIGH" if verdict["status"] == "PASS" else "MODERATE",
        )
        return _check_record(job_id, "oauthProtectedResource", rec)

    def _auth_md(self, job_id: str, base: str, target: str) -> CheckRecord:
        url = base + "/auth.md"
        res, err = _fetch(url, self.timeout)
        if res is None:
            rec = ar.build_evidence_record(
                target, "authMd", "UNDETERMINED", "UNKNOWN",
                observed=f"auth.md fetch failed: {err}",
                locator=url,
                claim="Could not determine whether agent registration is supported.",
                reason="Transport failure is not absence proof.",
                confidence="INSUFFICIENT",
            )
            return _check_record(job_id, "authMd", rec)
        if res.status == 404:
            # T08: OAuth existing does not make auth.md mandatory.
            rec = ar.build_evidence_record(
                target, "authMd", "NOT_APPLICABLE", "NOT_APPLICABLE",
                observed="HTTP 404 at /auth.md.",
                locator=url, http_status=404,
                claim="No agent registration document advertised.",
                reason="T08: auth.md absence is not a failure unless agent registration is offered.",
                confidence="LOW",
                next_action="Only add auth.md when an approved agent registration/claim flow exists.",
            )
            return _check_record(job_id, "authMd", rec)
        if not (200 <= res.status < 300):
            rec = ar.build_evidence_record(
                target, "authMd", "UNDETERMINED",
                "ERROR" if res.status >= 500 else "UNKNOWN",
                observed=f"HTTP {res.status} at /auth.md.",
                locator=url, http_status=res.status,
                claim="auth.md endpoint errored or blocked.",
                confidence="INSUFFICIENT",
            )
            return _check_record(job_id, "authMd", rec)
        body = _body_text(res)
        if ar.is_prompt_injection_probe(body):
            # T10: treat as data, flag it.
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "UNKNOWN",
                observed="auth.md retrieved but contains instruction-like text; treated as DATA not instruction.",
                locator=url, http_status=res.status, content_type=_content_type(res),
                claim="auth.md content cannot be trusted at face value; manual review required.",
                reason="T10: remote text is data, never instruction.",
                confidence="LOW",
                next_action="Manually review registration/claim/revocation steps before trusting this document.",
            )
            return _check_record(job_id, "authMd", rec)
        verdict = ar.validate_auth_md(body)
        if verdict["status"] != "PASS":
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", verdict["status"],
                observed=f"HTTP 200 at /auth.md; {verdict['reason']}",
                locator=url, http_status=res.status, content_type=_content_type(res),
                claim=verdict["reason"],
                reason="auth.md presence alone is not a security proof.",
                confidence="LOW",
                next_action=("Serve /auth.md with an H1 containing 'auth.md' and a complete "
                            "agent_auth block or self-contained registration flow. Never probe "
                            "registration endpoints during passive scans."),
            )
            return _check_record(job_id, "authMd", rec)
        if verdict["detail"] == "agent_auth":
            # Upstream skill: agent_auth must match advertised PRM/AS metadata.
            return self._auth_md_crosscheck(job_id, base, target, url, res)
        rec = ar.build_evidence_record(
            target, "authMd", "APPLICABLE", "PASS",
            observed=f"HTTP 200 at /auth.md; {verdict['reason']}",
            locator=url, http_status=res.status, content_type=_content_type(res),
            claim="auth.md present with H1 and self-contained registration description.",
            reason="auth.md presence alone is not a security proof; revocation/audit need manual review.",
            confidence="MODERATE",
        )
        return _check_record(job_id, "authMd", rec)

    def _auth_md_crosscheck(self, job_id: str, base: str, target: str, url: str, res) -> CheckRecord:
        """agent_auth block exists: verify it against PRM + AS metadata (read-only GETs)."""
        prm_url = base + "/.well-known/oauth-protected-resource"
        prm_res, prm_err = _fetch(prm_url, self.timeout)
        if prm_res is None or prm_res.status == 404:
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "UNKNOWN",
                observed="auth.md declares agent_auth but no protected-resource metadata is advertised.",
                locator=url, http_status=res.status, content_type=_content_type(res),
                claim="agent_auth references OAuth metadata that is not published; consistency unverifiable.",
                reason="Upstream skill requires agent_auth to match advertised PRM/AS metadata.",
                confidence="LOW",
                next_action="Publish RFC 9728 PRM plus RFC 8414 AS metadata matching agent_auth, or keep auth.md self-contained.",
            )
            return _check_record(job_id, "authMd", rec)
        if not (200 <= prm_res.status < 300):
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "UNKNOWN",
                observed=f"PRM endpoint returned HTTP {prm_res.status}; cross-check blocked.",
                locator=url, http_status=res.status,
                claim="agent_auth consistency could not be verified; access blocked.",
                confidence="INSUFFICIENT",
            )
            return _check_record(job_id, "authMd", rec)
        try:
            prm_doc = json.loads(_body_text(prm_res))
        except ValueError:
            prm_doc = None
        check = ar.validate_protected_resource(prm_doc)
        if check["status"] != "PASS":
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "FAIL",
                observed=f"PRM invalid: {check['reason']}",
                locator=prm_url, http_status=prm_res.status,
                claim="agent_auth points at invalid protected-resource metadata.",
                reason=check["reason"],
                confidence="MODERATE",
            )
            return _check_record(job_id, "authMd", rec)
        servers = prm_doc.get("authorization_servers", [])
        as_url = (servers[0].rstrip("/") + "/.well-known/oauth-authorization-server"
                  if servers else None)
        as_res, _ = _fetch(as_url, self.timeout) if as_url else (None, "no server")
        issuer_ok = False
        if as_res is not None and 200 <= as_res.status < 300:
            try:
                as_doc = json.loads(_body_text(as_res))
            except ValueError:
                as_doc = None
            issuer_ok = (isinstance(as_doc, dict) and isinstance(as_doc.get("issuer"), str)
                         and as_doc["issuer"] in servers)
        if issuer_ok:
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "PASS",
                observed="auth.md agent_auth matches advertised PRM and AS issuer.",
                locator=url, http_status=res.status, content_type=_content_type(res),
                claim="auth.md agent_auth is consistent with published PRM/AS metadata.",
                reason="Read-only cross-check only; registration endpoints were never probed.",
                confidence="HIGH",
            )
        else:
            rec = ar.build_evidence_record(
                target, "authMd", "APPLICABLE", "FAIL",
                observed="AS metadata missing or issuer does not match PRM authorization_servers.",
                locator=as_url or prm_url,
                claim="agent_auth chain is inconsistent; issuer does not match PRM.",
                reason="Upstream skill requires the AS issuer to match the PRM advertisement.",
                confidence="MODERATE",
            )
        return _check_record(job_id, "authMd", rec)

    def _webmcp_unknown(self, job_id: str, target: str) -> CheckRecord:
        # Static declarative markers (upstream webmcp SKILL.md) make WebMCP
        # APPLICABLE, but only a version-pinned browser execution can PASS.
        # T06: no browser in this probe env => UNKNOWN, never site FAIL.
        res, _ = _fetch(target, self.timeout)
        html = _body_text(res) if res is not None and 200 <= res.status < 300 else ""
        found = ar.find_webmcp_markers(html)
        applicability = "APPLICABLE" if found["found"] else "UNDETERMINED"
        observed = ("Declarative WebMCP markers found: " + ", ".join(found["markers"]) + "."
                    if found["found"] else
                    "No static WebMCP markers in homepage HTML; runtime registration still possible.")
        rec = ar.build_evidence_record(
            target, "webMcp", applicability, "UNKNOWN",
            observed=observed + " No WebMCP-capable test browser in this probe environment.",
            locator=target, claim="WebMCP support not measured in this environment.",
            reason="T06: missing browser capability is an environment limit, not a site failure. Spec is early-preview; version must be pinned before scoring.",
            confidence="INSUFFICIENT", next_action="Run version-pinned browser tool-registration tests in Sprint C.",
            evidence_type="browser",
        )
        return _check_record(job_id, "webMcp", rec)

    _DOH_RESOLVERS = (
        "https://cloudflare-dns.com/dns-query",
        "https://dns.google/resolve",
    )

    def _doh_query(self, name: str, qtype: str) -> dict:
        """Query one DNS name via DNS-over-HTTPS (Cloudflare, Google fallback).

        Upstream dns-aid SKILL.md names these exact resolvers. Read-only GETs.
        """
        saw_failure = False
        for endpoint in self._DOH_RESOLVERS:
            url = f"{endpoint}?name={name}&type={qtype}"
            res, _ = _fetch(url, timeout=8)
            if res is None or not (200 <= res.status < 300):
                saw_failure = True
                continue
            try:
                doc = json.loads(_body_text(res))
            except ValueError:
                saw_failure = True
                continue
            parsed = ar.parse_doh_answer(doc)
            if parsed["status"] == "BAD_RESPONSE":
                saw_failure = True
                continue
            return parsed
        return {"status": "RESOLVER_FAILED" if saw_failure else "NO_RECORDS"}

    def _dns_aid_unknown(self, job_id: str, target: str, domain: str) -> CheckRecord:
        # Real DNS-AID discovery per upstream SKILL.md: SVCB/HTTPS under the
        # _agents namespace (_index + _a2a), DNSSEC noted via the AD flag.
        # T09: experimental draft; NO_RECORDS on real DNS => NOT_APPLICABLE,
        # never a hard failure for classic SaaS.
        host = domain.strip().lower().rstrip(".")
        names = (f"_index._agents.{host}", f"_a2a._agents.{host}")
        resolver_failed = False
        for name in names:
            for qtype in ("SVCB", "HTTPS"):
                answer = self._doh_query(name, qtype)
                if answer["status"] == "HAS_RECORDS":
                    records = "; ".join(answer.get("records", []))[:300]
                    dnssec = "DNSSEC AD flag set." if answer.get("dnssec") else "DNSSEC AD flag not set."
                    rec = ar.build_evidence_record(
                        target, "dnsAid", "APPLICABLE", "PASS",
                        observed=f"{qtype} record(s) at {name}: {records} {dnssec}",
                        locator=name, claim="Agent DNS records published and parseable.",
                        reason="Records follow the draft SVCB/HTTPS shape; DNSSEC noted separately.",
                        confidence="MODERATE",
                        next_action="Keep records and DNSSEC in sync; do not auto-remediate DNS.",
                        evidence_type="dns",
                    )
                    return _check_record(job_id, "dnsAid", rec)
                if answer["status"] == "RESOLVER_FAILED":
                    resolver_failed = True
        if resolver_failed:
            rec = ar.build_evidence_record(
                target, "dnsAid", "UNDETERMINED", "UNKNOWN",
                observed="DoH resolvers unreachable for _agents namespace queries.",
                locator=names[0],
                claim="DNS-AID publish state could not be determined; resolvers failed.",
                reason="Resolver failure is not absence proof.",
                confidence="INSUFFICIENT", evidence_type="dns",
            )
            return _check_record(job_id, "dnsAid", rec)
        rec = ar.build_evidence_record(
            target, "dnsAid", "NOT_APPLICABLE", "NOT_APPLICABLE",
            observed=f"No SVCB/HTTPS records at {names[0]} or {names[1]} (live DNS-over-HTTPS).",
            locator=names[0], claim="No DNS-AID agent records published.",
            reason="T09: experimental DNS discovery is not mandatory for classic SaaS.",
            confidence="MODERATE",
            next_action="Only evaluate further when the org publishes discoverable agent services via DNS.",
            evidence_type="dns",
        )
        return _check_record(job_id, "dnsAid", rec)
