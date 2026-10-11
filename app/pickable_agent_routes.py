"""Crawlable Pickable discovery routes and stateless MCP Streamable HTTP.

The discovery files are static, reviewable assets under ``web/``. This module
only maps real files to URLs, substitutes the validated public origin, and
serves the existing scan backend through its read API and the shared scan
admission callback supplied by ``server.py``.
"""
import copy
import json
import math
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlsplit

from app.agent_policy import AgentPolicy, RequestError, ScanRateLimitError


SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18")
LATEST_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
MAX_MCP_REQUEST_BYTES = 32 * 1024
MAX_MCP_RESPONSE_BYTES = 256 * 1024
MAX_REPORT_ID_LENGTH = 128


# Each entry points at an asset that is actually packaged in ``web/``. Aliases
# intentionally share the same file rather than claiming extra functionality.
ROUTES = {
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
    "/sitemap.xml": ("sitemap.xml", "application/xml; charset=utf-8"),
    "/llms.txt": ("llms.txt", "text/plain; charset=utf-8"),
    "/llms-full.txt": ("llms-full.txt", "text/plain; charset=utf-8"),
    "/docs-llms.txt": ("docs-llms.txt", "text/plain; charset=utf-8"),
    "/api-llms.txt": ("api-llms.txt", "text/plain; charset=utf-8"),
    "/openapi.json": ("openapi.json", "application/json; charset=utf-8"),
    "/.well-known/agent.json": (".well-known/agent.json", "application/json; charset=utf-8"),
    "/.well-known/ard.json": (".well-known/ard.json", "application/json; charset=utf-8"),
    "/.well-known/ai-catalog.json": (".well-known/ai-catalog.json", "application/json; charset=utf-8"),
    "/.well-known/ai-catalog": (".well-known/ai-catalog.json", "application/json; charset=utf-8"),
    "/.well-known/api-catalog": (
        ".well-known/api-catalog.json",
        'application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"',
    ),
    "/.well-known/mcp/server-card.json": (
        ".well-known/mcp/server-card.json", "application/mcp-server-card+json; charset=utf-8"
    ),
    "/mcp/server-card": (".well-known/mcp/server-card.json", "application/mcp-server-card+json; charset=utf-8"),
    "/.well-known/agent-skills/index.json": (
        ".well-known/agent-skills/index.json", "application/json; charset=utf-8"
    ),
    "/agents.md": ("AGENTS.md", "text/markdown; charset=utf-8"),
    "/AGENTS.md": ("AGENTS.md", "text/markdown; charset=utf-8"),
    "/skill.md": ("SKILL.md", "text/markdown; charset=utf-8"),
    "/SKILL.md": ("SKILL.md", "text/markdown; charset=utf-8"),
    "/skills/pickable-scan/SKILL.md": (
        "skills/pickable-scan/SKILL.md", "text/markdown; charset=utf-8"
    ),
    "/index.md": ("index.md", "text/markdown; charset=utf-8"),
    "/about": ("about.html", "text/html; charset=utf-8"),
    "/about.md": ("about.md", "text/markdown; charset=utf-8"),
    "/docs": ("docs.html", "text/html; charset=utf-8"),
    "/docs.md": ("docs.md", "text/markdown; charset=utf-8"),
    "/docs/llms.txt": ("docs-llms.txt", "text/plain; charset=utf-8"),
    "/api": ("api-docs.html", "text/html; charset=utf-8"),
    "/api.md": ("api.md", "text/markdown; charset=utf-8"),
    "/api/llms.txt": ("api-llms.txt", "text/plain; charset=utf-8"),
    "/docs/agent-scan": ("agent-scan.html", "text/html; charset=utf-8"),
    "/docs/agent-scan.md": ("agent-scan.md", "text/markdown; charset=utf-8"),
    "/agent-scan.md": ("agent-scan.md", "text/markdown; charset=utf-8"),
    "/auth.md": ("auth.md", "text/markdown; charset=utf-8"),
    "/pricing": ("pricing.html", "text/html; charset=utf-8"),
    "/pricing.md": ("pricing.md", "text/markdown; charset=utf-8"),
    "/pickable-og.svg": ("pickable-og.svg", "image/svg+xml; charset=utf-8"),
    "/pickable-rise.svg": ("pickable-rise.svg", "image/svg+xml; charset=utf-8"),
    "/pickable-rise-favicon.svg": ("pickable-rise-favicon.svg", "image/svg+xml; charset=utf-8"),
    "/pickable-mark.svg": ("pickable-mark.svg", "image/svg+xml; charset=utf-8"),
    "/site-docs.css": ("site-docs.css", "text/css; charset=utf-8"),
}

MARKDOWN_PAGES = {
    "/": "index.md",
    "/about": "about.md",
    "/docs": "docs.md",
    "/docs/agent-scan": "agent-scan.md",
    "/api": "api.md",
    "/pricing": "pricing.md",
}


def _json_safe(value):
    """Return only finite JSON values, without exposing arbitrary objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if isinstance(key, str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return None


def _replace_origin(value, public_url):
    """Substitute the reviewed demo origin in every served text asset."""
    if isinstance(value, str):
        return value.replace("https://solveny.dev", public_url).replace("http://solveny.dev", public_url)
    if isinstance(value, list):
        return [_replace_origin(item, public_url) for item in value]
    if isinstance(value, dict):
        return {key: _replace_origin(item, public_url) for key, item in value.items()}
    return value


def _mcp_scan_enabled(policy):
    return bool(getattr(policy, "mcp_scan_enabled", False) and getattr(policy, "agent_api_key", ""))


def _sync_manifest_capabilities(manifest, policy, path):
    """Keep optional MCP scan claims aligned with the runtime policy."""
    if not isinstance(manifest, dict):
        return manifest
    enabled = _mcp_scan_enabled(policy)
    names = [tool["name"] for tool in tool_definitions(policy)]
    # The public Pickable manifest is intentionally not an A2A card. Add a
    # small explicit flag so a crawler can distinguish REST reads from MCP scan
    # creation without inferring OAuth or task-dispatch behavior.
    if path == "/.well-known/agent.json":
        manifest["mcp_scan_enabled"] = enabled
        manifest["mcp_tools"] = names
    for key in ("mcp_tools", "tools"):
        rows = manifest.get(key)
        if not isinstance(rows, list):
            continue
        rows = [row for row in rows if not (
            row == "start_website_scan"
            or (isinstance(row, dict) and row.get("name") == "start_website_scan")
        )]
        if enabled and key == "mcp_tools":
            rows.append("start_website_scan")
        elif enabled and key == "tools":
            rows.append(next(tool for tool in tool_definitions(policy) if tool["name"] == "start_website_scan"))
        manifest[key] = rows
    if "mcp_scan_enabled" in manifest:
        manifest["mcp_scan_enabled"] = enabled
    # The ARD and its ai-catalog alias contain entries with MCP tool names in
    # their capabilities arrays. Keep the optional creation action truthful.
    entries = manifest.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            capabilities = entry.get("capabilities")
            if not isinstance(capabilities, list) or "get_scan_report" not in capabilities:
                continue
            capabilities = [name for name in capabilities if name != "start_website_scan"]
            if enabled:
                capabilities.append("start_website_scan")
            entry["capabilities"] = capabilities
    return manifest


def _accepts_markdown(accept_header):
    for item in (accept_header or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or parts[0].lower() != "text/markdown":
            continue
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    if float(parameter[2:]) <= 0:
                        return False
                except ValueError:
                    return False
        return True
    return False


def _accepts_mcp_json_and_sse(accept_header):
    accepted = set()
    for item in (accept_header or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0
        if quality > 0:
            accepted.add(parts[0].lower())
    return "application/json" in accepted and "text/event-stream" in accepted


def _rpc_response(request_id, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _rpc_response(request_id, error=error)


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _tool_result(data, protocol_version, is_error=False):
    result = {
        "content": [{"type": "text", "text": _compact_json(data)}],
        "isError": bool(is_error),
    }
    # structuredContent was introduced in 2025-06-18. Older clients receive the
    # same complete JSON in text content, without an undeclared extension.
    if not is_error and protocol_version == "2025-06-18":
        result["structuredContent"] = data
    return result


def _response_size(response):
    return len(_compact_json(response).encode("utf-8"))


def _truncate_result_data(data, original_size):
    """Drop only bulky evidence, preserve report scores/status, and say what went."""
    shortened = copy.deepcopy(data)
    omitted = []
    report = shortened.get("report")
    if isinstance(report, dict):
        specific = report.pop("source_specific", None)
        if isinstance(specific, list) and specific:
            omitted.append({"path": "report.source_specific", "items": len(specific)})
        # Normalized status, attribution and reason remain useful; raw provider
        # evidence stays accessible from the linked REST report endpoint.
        for index, check in enumerate(report.get("checks", [])):
            sources = check.get("sources") if isinstance(check, dict) else None
            if not isinstance(sources, dict):
                continue
            for source, observation in sources.items():
                if isinstance(observation, dict) and "raw" in observation:
                    observation.pop("raw", None)
                    omitted.append({"path": f"report.checks[{index}].sources.{source}.raw"})
        marketing = report.get("marketing")
        marketing_checks = marketing.get("checks") if isinstance(marketing, dict) else None
        for index, check in enumerate(marketing_checks or []):
            sources = check.get("sources") if isinstance(check, dict) else None
            if not isinstance(sources, dict):
                continue
            for source, observation in sources.items():
                if isinstance(observation, dict) and "raw" in observation:
                    observation.pop("raw", None)
                    omitted.append({"path": f"report.marketing.checks[{index}].sources.{source}.raw"})
        recommendations = report.get("recommendations")
        if isinstance(recommendations, list) and len(recommendations) > 10:
            omitted.append({"path": "report.recommendations", "items": len(recommendations) - 10})
            report["recommendations"] = recommendations[:10]
    scans = shortened.get("scans")
    if isinstance(scans, list) and len(scans) > 100:
        omitted.append({"path": "scans", "items": len(scans) - 100})
        shortened["scans"] = scans[:100]
    shortened["truncation"] = {
        "truncated": True,
        "limit_bytes": MAX_MCP_RESPONSE_BYTES,
        "untruncated_response_bytes": original_size,
        "omitted": omitted,
        "details_url": shortened.get("report_url") or shortened.get("scan_url"),
    }
    return shortened


def _bounded_tool_response(request_id, data, protocol_version, is_error=False):
    result = _tool_result(data, protocol_version, is_error)
    response = _rpc_response(request_id, result)
    original_size = _response_size(response)
    if original_size <= MAX_MCP_RESPONSE_BYTES:
        return response
    if is_error:
        return _rpc_response(request_id, _tool_result({"error": "Tool response exceeded the configured size limit."}, protocol_version, True))
    truncated = _truncate_result_data(data, original_size)
    response = _rpc_response(request_id, _tool_result(truncated, protocol_version))
    if _response_size(response) <= MAX_MCP_RESPONSE_BYTES:
        return response
    # Extremely large provider messages or scan histories must still produce a
    # valid bounded result. The actual full data remains available at these REST
    # URLs; never claim the abbreviated object is complete.
    minimal = {key: data.get(key) for key in ("job_id", "status", "domain", "created_at", "scan_url", "report_url") if key in data}
    sources = data.get("sources")
    if isinstance(sources, list):
        minimal["sources"] = [
            {key: _json_safe(row.get(key)) for key in ("id", "status") if key in row}
            for row in sources if isinstance(row, dict)
        ]
    omitted = []
    if "scans" in data:
        rows = data["scans"] if isinstance(data["scans"], list) else []
        minimal["scans"] = rows[:20]
        if len(rows) > len(minimal["scans"]):
            omitted.append({"path": "scans", "items": len(rows) - len(minimal["scans"])})
    if "report" in data:
        minimal["report"] = None
        if data["report"] is not None:
            omitted.append({"path": "report", "reason": "MCP response size limit"})
    minimal["truncation"] = {
        "truncated": True,
        "limit_bytes": MAX_MCP_RESPONSE_BYTES,
        "untruncated_response_bytes": original_size,
        "omitted": omitted,
        "details_url": data.get("report_url") or data.get("scan_url"),
    }
    response = _rpc_response(request_id, _tool_result(minimal, protocol_version))
    if _response_size(response) <= MAX_MCP_RESPONSE_BYTES:
        return response
    emergency = {
        "truncation": {
            "truncated": True,
            "limit_bytes": MAX_MCP_RESPONSE_BYTES,
            "untruncated_response_bytes": original_size,
            "omitted": [{"path": "tool_result", "reason": "MCP response size limit"}],
        }
    }
    response = _rpc_response(request_id, _tool_result(emergency, protocol_version))
    if _response_size(response) <= MAX_MCP_RESPONSE_BYTES:
        return response
    # Request bodies are capped at 32 KiB, so the echoed request id is also
    # bounded. This final protocol error is smaller than the configured cap.
    return _rpc_error(request_id, -32603, "MCP response exceeded the configured size limit.")


def _valid_request_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _schema_error(schema, value, at="arguments"):
    """Enforce the JSON Schema subset used by this server's tool definitions."""
    expected = schema.get("type")
    actual = value
    matches = (
        expected == "object" and isinstance(actual, dict)
        or expected == "string" and isinstance(actual, str)
        or expected == "integer" and isinstance(actual, int) and not isinstance(actual, bool)
        or expected == "number" and isinstance(actual, (int, float)) and not isinstance(actual, bool)
        or expected == "boolean" and isinstance(actual, bool)
        or expected == "array" and isinstance(actual, list)
    )
    if expected and not matches:
        return f"{at} must be {expected}."
    if isinstance(actual, dict) and expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in actual:
                return f"{at}.{key} is required."
        if schema.get("additionalProperties") is False:
            extra = next((key for key in actual if key not in properties), None)
            if extra is not None:
                return f"{at}.{extra} is not allowed."
        for key, item in actual.items():
            if key in properties:
                error = _schema_error(properties[key], item, f"{at}.{key}")
                if error:
                    return error
    if isinstance(actual, str):
        if "minLength" in schema and len(actual) < schema["minLength"]:
            return f"{at} is too short."
        if "maxLength" in schema and len(actual) > schema["maxLength"]:
            return f"{at} is too long."
        if "pattern" in schema and re.fullmatch(schema["pattern"], actual) is None:
            return f"{at} has an invalid format."
    return None


def tool_definitions(policy=None):
    read_only = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    tools = [
        {
            "name": "describe_pickable",
            "title": "Describe Pickable",
            "description": "Describe Pickable's actual scan inputs, APIs, methodology, and limits.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": dict(read_only),
        },
        {
            "name": "list_scans",
            "title": "List scans",
            "description": "List scan summaries visible to the caller’s network peer. Does not create a scan.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": dict(read_only),
        },
        {
            "name": "get_scan_report",
            "title": "Get a scan report",
            "description": "Read status, source states, and the actual normalized report for an existing scan.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "minLength": 8, "maxLength": MAX_REPORT_ID_LENGTH, "pattern": "[A-Za-z0-9_-]{8,128}"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            "annotations": dict(read_only),
        },
    ]
    if _mcp_scan_enabled(policy):
        tools.append({
            "name": "start_website_scan",
            "title": "Start a website scan",
            "description": "Create an asynchronous scan of one public domain. Requires an operator-issued Bearer key; results remain preliminary and source-reported.",
            "inputSchema": {
                "type": "object",
                "properties": {"domain": {"type": "string", "minLength": 1, "maxLength": 253}},
                "required": ["domain"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        })
    return tools


def _tool_by_name(policy):
    return {tool["name"]: tool for tool in tool_definitions(policy)}


def _tool_error(request_id, message, protocol_version, code="tool_error", retry_after=None):
    data = {"error": message, "code": code}
    if retry_after is not None:
        data["retry_after"] = retry_after
    return _bounded_tool_response(request_id, data, protocol_version, is_error=True)


def _describe(policy):
    base = policy.public_url
    names = [tool["name"] for tool in tool_definitions(policy)]
    return {
        "name": "Pickable",
        "website": base + "/",
        "description": "Analyze public-domain agent-readiness signals from Ora and Cloudflare and basic marketing checks from Pickable's growth adapter.",
        "documentation": base + "/docs",
        "agent_guide": base + "/docs/agent-scan",
        "openapi": base + "/openapi.json",
        "mcp_endpoint": base + "/mcp",
        "methodology": "The normalized score reflects source-reported checks, coverage, and Pickable's documented rubric. It does not prove that an agent completed a real task.",
        "scan_enabled": _mcp_scan_enabled(policy),
        "available_tools": names,
        "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
    }


def _summary_rows(rows):
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        result.append({key: _json_safe(row.get(key)) for key in ("id", "domain", "status", "created_at") if key in row})
    return result


def _report_data(backend, job_id, policy):
    job = backend["get"](job_id)
    if not isinstance(job, dict):
        return None
    encoded_id = quote(job_id, safe="")
    return _json_safe({
        "job_id": job_id,
        "status": job.get("status"),
        "domain": job.get("domain"),
        "created_at": job.get("created_at"),
        "sources": job.get("sources", []),
        "report": job.get("report"),
        "scan_url": f"{policy.public_url}/api/scans/{encoded_id}",
        "report_url": f"{policy.public_url}/api/scans/{encoded_id}/report",
    })


def _call_tool(request_id, name, args, backend, remote_addr, policy, create_scan, protocol_version):
    if name == "describe_pickable":
        return _bounded_tool_response(request_id, _describe(policy), protocol_version)
    if name == "list_scans":
        try:
            rows = backend["list"]()
        except Exception:
            return _tool_error(request_id, "Could not read the scan list.", protocol_version, "scan_list_failed")
        return _bounded_tool_response(request_id, {"scans": _summary_rows(rows)}, protocol_version)
    if name == "get_scan_report":
        job_id = args["job_id"]
        try:
            data = _report_data(backend, job_id, policy)
        except Exception:
            return _tool_error(request_id, "Could not read the scan report.", protocol_version, "scan_read_failed")
        if data is None:
            return _tool_error(request_id, "Scan not found.", protocol_version, "scan_not_found")
        return _bounded_tool_response(request_id, data, protocol_version)
    if name == "start_website_scan":
        try:
            job_id = create_scan(args["domain"], remote_addr)
        except ScanRateLimitError as error:
            return _tool_error(request_id, "Scan limit reached.", protocol_version, "scan_rate_limited", error.retry_after)
        except ValueError:
            return _tool_error(request_id, "Invalid or unsupported public domain.", protocol_version, "invalid_domain")
        except OverflowError:
            return _tool_error(request_id, "The scan queue is full. Try again later.", protocol_version, "scan_queue_full", 60)
        except Exception:
            return _tool_error(request_id, "Could not start the scan.", protocol_version, "scan_start_failed")
        if not isinstance(job_id, str) or not job_id:
            return _tool_error(request_id, "The scan service did not return a job ID.", protocol_version, "scan_start_failed")
        # The demo backend completes synchronously; live scans may already be
        # running. Read back current state rather than claiming a fixed status.
        try:
            state = _report_data(backend, job_id, policy) or {}
        except Exception:
            state = {}
        return _bounded_tool_response(request_id, {
            "job_id": job_id,
            "domain": state.get("domain", args["domain"]),
            "status": state.get("status", "unknown"),
            "scan_url": state.get("scan_url", f"{policy.public_url}/api/scans/{quote(job_id, safe='')}"),
            "report_url": state.get("report_url", f"{policy.public_url}/api/scans/{quote(job_id, safe='')}/report"),
            "poll_with": "get_scan_report",
        }, protocol_version)
    return _rpc_error(request_id, -32602, "Unknown tool.")


def mcp_dispatch(body, backend, remote_addr, policy=None, create_scan=None, protocol_version=DEFAULT_PROTOCOL_VERSION):
    """Dispatch one already-decoded JSON-RPC request; notifications are inert."""
    policy = policy or AgentPolicy()
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) or not body["method"]:
        return _rpc_error(None, -32600, "Invalid JSON-RPC 2.0 request")
    if "id" not in body:
        return None
    request_id = body["id"]
    if not _valid_request_id(request_id):
        return _rpc_error(None, -32600, "Invalid JSON-RPC request id")
    method = body["method"]
    params = body.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Request params must be an object")
    if "_meta" in params and not isinstance(params["_meta"], dict):
        return _rpc_error(request_id, -32602, "Request _meta must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        capabilities, client_info = params.get("capabilities"), params.get("clientInfo")
        if not isinstance(requested, str) or not isinstance(capabilities, dict) or not isinstance(client_info, dict):
            return _rpc_error(request_id, -32602, "initialize requires protocolVersion, capabilities, and clientInfo")
        if not isinstance(client_info.get("name"), str) or not isinstance(client_info.get("version"), str):
            return _rpc_error(request_id, -32602, "clientInfo requires string name and version")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "pickable", "title": "Pickable", "version": "1.0.0"},
            "instructions": "Pickable normalizes external scan observations. Use describe_pickable for the documented scope; a score does not prove a real agent completed a task.",
        }
        return _rpc_response(request_id, result)
    if method == "ping":
        if set(params) - {"_meta"}:
            return _rpc_error(request_id, -32602, "ping does not accept parameters")
        return _rpc_response(request_id, {})
    if method == "tools/list":
        if set(params) - {"cursor", "_meta"}:
            return _rpc_error(request_id, -32602, "tools/list received unsupported parameters")
        if "cursor" in params and not isinstance(params["cursor"], str):
            return _rpc_error(request_id, -32602, "tools/list cursor must be a string")
        if params.get("cursor"):
            return _rpc_error(request_id, -32602, "tools/list does not recognize this cursor")
        return _rpc_response(request_id, {"tools": tool_definitions(policy)})
    if method == "tools/call":
        if set(params) - {"name", "arguments", "_meta"}:
            return _rpc_error(request_id, -32602, "tools/call received unsupported parameters")
        name = params.get("name")
        if not isinstance(name, str):
            return _rpc_error(request_id, -32602, "tools/call requires a string name")
        definition = _tool_by_name(policy).get(name)
        if definition is None:
            return _rpc_error(request_id, -32602, "Unknown or disabled tool")
        args = params.get("arguments", {})
        error = _schema_error(definition["inputSchema"], args)
        if error:
            return _rpc_error(request_id, -32602, error)
        return _call_tool(request_id, name, args, backend, remote_addr, policy, create_scan, protocol_version)
    return _rpc_error(request_id, -32601, "Method not found")


def _json_error(handler, status, message, rpc_code=-32600, request_id=None, headers=None):
    handler.send(_rpc_error(request_id, rpc_code, message), status, headers=headers)


def _method_not_allowed(handler, method):
    handler.send(
        {"error": f"MCP {method} is unavailable: this endpoint supports stateless JSON-RPC POST requests only."},
        405,
        headers={"Allow": "POST"},
    )


def serve_agent_get(handler, path, backend, root: Path, policy):
    if path == "/api/health":
        handler.send({"ok": True, "product": "pickable", "mode": backend["mode"]})
        return True
    if path == "/mcp":
        _method_not_allowed(handler, "GET/SSE")
        return True

    query = parse_qs(urlsplit(getattr(handler, "path", path)).query, keep_blank_values=True)
    agent_mode = path == "/" and query.get("mode") == ["agent"]
    entry = ROUTES.get(path)
    if agent_mode or (path in MARKDOWN_PAGES and _accepts_markdown(handler.headers.get("Accept", ""))):
        entry = (MARKDOWN_PAGES[path], "text/markdown; charset=utf-8")
    if entry is None:
        return False
    name, mime = entry
    target = Path(root) / "web" / name
    if not target.is_file():
        handler.send({"error": "The requested discovery resource is not packaged."}, 503)
        return True
    try:
        payload = target.read_bytes()
    except OSError:
        handler.send({"error": "The requested discovery resource is unavailable."}, 503)
        return True

    public_url = policy.public_url
    is_json = mime.split(";", 1)[0].strip().lower() == "application/json" or mime.split(";", 1)[0].strip().lower().endswith("+json")
    if is_json:
        try:
            value = json.loads(payload.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
        except (ValueError, UnicodeError):
            handler.send({"error": "The requested JSON discovery resource is invalid."}, 503)
            return True
        value = _replace_origin(value, public_url)
        if path in ("/.well-known/agent.json", "/.well-known/ard.json", "/.well-known/ai-catalog.json", "/.well-known/ai-catalog"):
            value = _sync_manifest_capabilities(value, policy, "/.well-known/agent.json" if path == "/.well-known/agent.json" else path)
        handler.send(value, 200, mime)
    else:
        # All bundled docs are UTF-8 text, except the optional SVG which is
        # still textual XML. Substitute in the raw bytes so HTML, Markdown,
        # XML, CSS, and the home page all use the same validated origin.
        origin = b"https://solveny.dev"
        replacement = public_url.encode("utf-8")
        payload = payload.replace(origin, replacement).replace(b"http://solveny.dev", replacement)
        handler.send(payload, 200, mime)
    return True


def _error_for_request_error(error):
    mapping = {"invalid_json": -32700, "invalid_request": -32600, "body_too_large": -32600,
               "unsupported_media_type": -32600, "request_timeout": -32600}
    return mapping.get(error.code, -32600)


def _is_valid_envelope(body):
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) or not body["method"]:
        return False
    if "id" in body and not _valid_request_id(body["id"]):
        return False
    if "params" in body and not isinstance(body["params"], (dict, list)):
        return False
    return True


def _send_accepted_notification(handler):
    handler.send_response(202)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Vary", "Accept")
    handler.end_headers()


def serve_agent_post(handler, backend, raw_path, policy, create_scan):
    if urlsplit(raw_path).path != "/mcp":
        return False
    if not _accepts_mcp_json_and_sse(handler.headers.get("Accept", "")):
        _json_error(handler, 406, "Accept must include application/json and text/event-stream.")
        return True
    try:
        body = handler.read_json(MAX_MCP_REQUEST_BYTES)
    except RequestError as error:
        if error.status == 415:
            _json_error(handler, error.status, error.message)
        else:
            _json_error(handler, error.status, error.message, _error_for_request_error(error))
        return True
    if not _is_valid_envelope(body):
        _json_error(handler, 400, "Invalid JSON-RPC 2.0 request.")
        return True

    protocol_header = handler.headers.get("MCP-Protocol-Version")
    if protocol_header is not None and protocol_header not in SUPPORTED_PROTOCOL_VERSIONS:
        _json_error(handler, 400, "Unsupported MCP-Protocol-Version.")
        return True
    # In JSON-RPC, every request without an id is a notification. It must not
    # dispatch a tool or create a scan, regardless of the method name.
    if "id" not in body:
        _send_accepted_notification(handler)
        return True

    method = body["method"]
    if method == "initialize":
        requested = (body.get("params") or {}).get("protocolVersion") if isinstance(body.get("params", {}), dict) else None
        protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    else:
        # Per Streamable HTTP, clients that omit this header are treated as
        # 2025-03-26 clients. The server has no session state to consult.
        protocol_version = protocol_header or DEFAULT_PROTOCOL_VERSION

    if method == "tools/call":
        params = body.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if name == "start_website_scan" and _mcp_scan_enabled(policy):
            if not policy.agent_authorized(handler.headers.get("Authorization")):
                _json_error(
                    handler,
                    401,
                    "A valid operator-issued Bearer key is required to start a scan.",
                    -32001,
                    body["id"],
                    {"WWW-Authenticate": 'Bearer realm="pickable"'},
                )
                return True

    response = mcp_dispatch(body, backend, handler.client_address[0], policy, create_scan, protocol_version)
    if response is None:
        _send_accepted_notification(handler)
    else:
        status = 400 if "error" in response and response["error"].get("code") == -32600 else 200
        handler.send(response, status, "application/json; charset=utf-8", headers={"MCP-Protocol-Version": protocol_version})
    return True
