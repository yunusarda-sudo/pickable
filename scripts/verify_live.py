#!/usr/bin/env python3
"""Read-only verification of Pickable's public agent discovery surfaces.

The verifier fetches public documents and invokes only MCP's initialize,
notification, ping, tools/list, and read-only tools. It never starts a scan.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


DISCOVERY_ROUTES = (
    "/auth.md",
    "/pricing.md",
    "/index.md",
    "/about.md",
    "/api.md",
    "/docs.md",
    "/skill.md",
    "/docs/llms.txt",
    "/api/llms.txt",
    "/mcp/server-card",
    "/.well-known/agent.json",
    "/.well-known/ard.json",
    "/.well-known/ai-catalog.json",
    "/.well-known/api-catalog",
    "/.well-known/agent-skills/index.json",
)

MARKDOWN_ROUTES = ("/", "/auth.md", "/pricing.md", "/index.md", "/about.md", "/api.md", "/docs.md", "/skill.md")
JSON_ROUTES = (
    "/.well-known/agent.json",
    "/.well-known/ard.json",
    "/.well-known/ai-catalog.json",
    "/.well-known/api-catalog",
    "/.well-known/agent-skills/index.json",
)


class Evidence:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.origin = urllib.parse.urlsplit(self.base_url)
        self.timeout = timeout
        self.checks: list[dict] = []

    def record(self, name, ok, *, method=None, path=None, status=None, content_type=None, detail=None):
        row = {"check": name, "ok": bool(ok)}
        if method:
            row["method"] = method
        if path:
            row["path"] = path
        if status is not None:
            row["status"] = int(status)
        if content_type:
            row["content_type"] = str(content_type).split(";")[0]
        if detail:
            row["detail"] = str(detail)[:240]
        self.checks.append(row)
        return bool(ok)

    def request(self, method, path, *, payload=None, headers=None, name=None, expected=None):
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers = dict(headers or {})
        if data is not None:
            request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Accept", "application/json, text/event-stream, text/markdown, */*")
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        status = None
        body = b""
        response_headers = {}
        error_text = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read()
            response_headers = error.headers
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            error_text = f"{type(error).__name__}: {error}"
        accepted = expected(status, body, response_headers) if expected and status is not None else (status is not None and 200 <= status < 300)
        self.record(
            name or f"{method} {path}", accepted,
            method=method,
            path=path,
            status=status,
            content_type=response_headers.get("Content-Type"),
            detail=error_text or (None if accepted else _safe_response_detail(body)),
        )
        return status, body, response_headers


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: set[str] = set()
        self.json_ld_count = 0
        self.json_ld_values: list[str] = []
        self._in_json_ld = False
        self._json_ld_buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("href", "src", "action"):
            value = attrs.get(key)
            if value:
                self.links.add(value.strip())
        if tag.lower() == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.json_ld_count += 1
            self._in_json_ld = True
            self._json_ld_buffer = []

    def handle_data(self, data):
        if self._in_json_ld:
            self._json_ld_buffer.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_json_ld:
            self.json_ld_values.append("".join(self._json_ld_buffer))
            self._in_json_ld = False
            self._json_ld_buffer = []


def _safe_response_detail(body: bytes) -> str | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                return str(error.get("code", "HTTP error"))
            if isinstance(error, str):
                return error[:180]
        return "response body did not meet the expected contract"
    except (ValueError, UnicodeDecodeError):
        return "response body did not meet the expected contract"


def _decode_json(body: bytes):
    return json.loads(body.decode("utf-8"))


def _local_path(base_url: str, target: str, allowed_origins=None) -> str | None:
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base_url + "/", target))
    base = urllib.parse.urlsplit(base_url)
    accepted_origins = {base.netloc, *(allowed_origins or ())}
    if parsed.scheme not in ("http", "https") or parsed.netloc not in accepted_origins:
        return None
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _urls_in_json(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key.lower() in {"url", "href", "uri", "endpoint", "openapi", "spec"} or ".json" in item.lower()):
                found.append(item)
            else:
                found.extend(_urls_in_json(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_urls_in_json(item))
    return found


def _resolve_component(openapi, value, section):
    if not isinstance(value, dict) or not isinstance(value.get("$ref"), str):
        return value
    prefix = f"#/components/{section}/"
    ref = value["$ref"]
    if not ref.startswith(prefix):
        return value
    resolved = openapi.get("components", {}).get(section, {}).get(ref[len(prefix):])
    return resolved if isinstance(resolved, dict) else value


def _validate_openapi(openapi):
    errors = []
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    if not isinstance(paths, dict):
        return ["OpenAPI document has no paths object"]
    operation_ids = []
    schema_refs = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            errors.append(f"{path} is not an OpenAPI path item")
            continue
        path_params = {
            item["name"] for raw in path_item.get("parameters", [])
            if isinstance(raw, dict) and isinstance((item := _resolve_component(openapi, raw, "parameters")), dict) and "name" in item
        }
        placeholders = set(re.findall(r"\{([^}]+)\}", path))
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(operation, dict):
                errors.append(f"{method.upper()} {path} is not an operation")
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id.strip():
                errors.append(f"{method.upper()} {path} has no operationId")
            else:
                operation_ids.append(operation_id)
            operation_params = {
                item["name"] for raw in operation.get("parameters", [])
                if isinstance(raw, dict) and isinstance((item := _resolve_component(openapi, raw, "parameters")), dict) and "name" in item
            }
            parameters = path_params | operation_params
            if not placeholders.issubset(parameters):
                errors.append(f"{method.upper()} {path} does not document path parameters {sorted(placeholders - parameters)}")
            responses = operation.get("responses", {})
            error_responses = [str(code) for code in responses if str(code).startswith("4") or str(code).startswith("5")]
            if not error_responses:
                errors.append(f"{method.upper()} {path} has no documented error responses")
            for code in error_responses:
                response = _resolve_component(openapi, responses.get(code, {}), "responses")
                content = response.get("content", {}) if isinstance(response, dict) else {}
                schemas = [media.get("schema") for media in content.values() if isinstance(media, dict)]
                if not schemas or not any(isinstance(schema, dict) for schema in schemas):
                    errors.append(f"{method.upper()} {path} {code} has no response schema")
                schema_refs.extend(ref for schema in schemas for ref in _schema_refs(schema))
    if len(operation_ids) != len(set(operation_ids)):
        errors.append("operationId values are not unique")
    components = openapi.get("components", {}).get("schemas", {})
    for ref in schema_refs:
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix) or ref[len(prefix):] not in components:
            errors.append(f"error response schema has unresolved reference {ref}")
    required = {
        ("/api/config", "get"),
        ("/api/scans", "get"),
        ("/api/scans", "post"),
        ("/api/scans/{job_id}", "get"),
        ("/mcp", "post"),
    }
    available = {(path, method.lower()) for path, item in paths.items() if isinstance(item, dict) for method in item if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}}
    if not required.issubset(available):
        errors.append(f"OpenAPI paths do not match handlers: missing {sorted(required - available)}")
    return errors


def _schema_refs(schema):
    refs = []
    if isinstance(schema, dict):
        if isinstance(schema.get("$ref"), str):
            refs.append(schema["$ref"])
        for value in schema.values():
            refs.extend(_schema_refs(value))
    elif isinstance(schema, list):
        for value in schema:
            refs.extend(_schema_refs(value))
    return refs


def verify(base_url: str, timeout: float, scan_id: str | None = None):
    evidence = Evidence(base_url, timeout)
    route_values = {}

    def get_route(path, *, name=None):
        result = evidence.request("GET", path, name=name)
        route_values[path] = result
        return result

    # Public route inventory and content validation.
    for path in DISCOVERY_ROUTES:
        status, body, headers = get_route(path, name=f"discovery {path}")
        if status is not None and 200 <= status < 300:
            content_type = headers.get("Content-Type", "").lower()
            if path in JSON_ROUTES or path == "/mcp/server-card":
                evidence.record(f"JSON content type {path}", "json" in content_type, method="GET", path=path, content_type=content_type, detail=None if "json" in content_type else "expected application/json")
            if "json" in content_type:
                try:
                    _decode_json(body)
                    evidence.record(f"valid JSON {path}", True, method="GET", path=path)
                except (ValueError, UnicodeDecodeError):
                    evidence.record(f"valid JSON {path}", False, method="GET", path=path, detail="invalid JSON")
            elif path.endswith(".md") or path.endswith(".txt") or path.startswith("/docs/") or path.startswith("/api/"):
                text_ok = bool(body.strip()) and ("text/" in content_type)
                evidence.record(f"readable text {path}", text_ok, method="GET", path=path, content_type=content_type, detail=None if text_ok else "expected nonempty text media type")

    plugin_status, _, _ = evidence.request(
        "GET", "/.well-known/ai-plugin.json", name="legacy plugin manifest omitted",
        expected=lambda status, _body, _headers: status == 404,
    )
    evidence.record("legacy plugin manifest omission is explicit", plugin_status == 404, method="GET", path="/.well-known/ai-plugin.json", status=plugin_status)

    # Markdown suffixes and the root's agent-mode query must retain a
    # Vary: Accept hint.
    for path in MARKDOWN_ROUTES:
        route = path if path.endswith(".md") else ("/index.md" if path == "/" else path + ".md")
        status, body, response_headers = evidence.request("GET", route, headers={"Accept": "text/markdown"}, name=f"markdown suffix {route}")
        ctype = response_headers.get("Content-Type", "").lower()
        vary = response_headers.get("Vary", "").lower()
        ok = status == 200 and "markdown" in ctype and bool(body.strip()) and "accept" in vary
        evidence.record(f"markdown representation {route}", ok, method="GET", path=route, status=status, content_type=ctype, detail=None if ok else "expected nonempty Markdown with Vary: Accept")

    agent_route = "/?mode=agent"
    status, body, response_headers = evidence.request("GET", agent_route, headers={"Accept": "text/html"}, name="root agent markdown mode")
    ctype = response_headers.get("Content-Type", "").lower()
    vary = response_headers.get("Vary", "").lower()
    ok = status == 200 and "markdown" in ctype and bool(body.strip()) and "accept" in vary
    evidence.record("root agent markdown mode", ok, method="GET", path=agent_route, status=status, content_type=ctype, detail=None if ok else "expected nonempty Markdown with Vary: Accept")

    # Ensure advertised markdown and manifest links resolve to real resources
    # on this same public origin.
    linked_targets = set()
    for path, (_, body, headers) in route_values.items():
        ctype = headers.get("Content-Type", "").lower()
        if "markdown" in ctype or path.endswith(".txt"):
            text = body.decode("utf-8", errors="replace")
            linked_targets.update(re.findall(r"\]\(([^)]+)\)", text))
            linked_targets.update(target.rstrip(".,;") for target in re.findall(r"https?://[^\s<>\"')]+", text))
        if "json" in ctype:
            try:
                linked_targets.update(_urls_in_json(_decode_json(body)))
            except (ValueError, UnicodeDecodeError):
                pass
    canonical = urllib.parse.urlsplit(os.environ.get("PICKABLE_PUBLIC_URL", "https://solveny.dev"))
    canonical_origins = {canonical.netloc} if canonical.netloc else set()
    sitemap_status, sitemap_body, sitemap_headers = get_route("/sitemap.xml", name="XML sitemap")
    if sitemap_status is not None and 200 <= sitemap_status < 300:
        xml_type_ok = "xml" in sitemap_headers.get("Content-Type", "").lower()
        evidence.record("sitemap XML content type", xml_type_ok, method="GET", path="/sitemap.xml", content_type=sitemap_headers.get("Content-Type"), detail=None if xml_type_ok else "expected an XML content type")
        try:
            xml_root = ET.fromstring(sitemap_body)
            locations = [node.text for node in xml_root.iter() if node.tag.rsplit("}", 1)[-1] == "loc" and node.text]
            evidence.record("valid XML sitemap", bool(locations), method="GET", path="/sitemap.xml", content_type=sitemap_headers.get("Content-Type"), detail=None if locations else "sitemap contains no loc entries")
            location_origins = {urllib.parse.urlsplit(location).netloc for location in locations}
            if len(location_origins) == 1:
                canonical_origins.update(location_origins)
            same_canonical_host = bool(locations) and location_origins.issubset(canonical_origins | {urllib.parse.urlsplit(base_url).netloc})
            evidence.record("sitemap uses one configured public origin", same_canonical_host, method="GET", path="/sitemap.xml", detail=None if same_canonical_host else "sitemap locations use an unexpected or mixed origin")
            linked_targets.update(locations)
            for target in locations:
                target_path = _local_path(base_url, target, canonical_origins)
                if target_path:
                    status, _, _ = evidence.request("GET", target_path, name=f"sitemap link {target_path}")
                    evidence.record(f"sitemap target {target_path}", status == 200, method="GET", path=target_path, status=status)
                else:
                    evidence.record(f"sitemap target {target}", False, method="GET", path=target, detail="target is not on the configured public origin")
        except ET.ParseError:
            evidence.record("valid XML sitemap", False, method="GET", path="/sitemap.xml", detail="invalid XML")

    for target in sorted(linked_targets):
        path = _local_path(base_url, target, canonical_origins)
        if not path or "{" in path or "}" in path or path in {"/mcp", "/openapi.json"}:
            continue
        status, _, _ = evidence.request("GET", path, name=f"advertised resource {path}")
        evidence.record(f"advertised resource {path}", status is not None and (200 <= status < 400), method="GET", path=path, status=status)

    # Validate JSON-LD and serve internal assets and links embedded in the
    # homepage. A broken local link is a discovery failure.
    home_status, home_body, home_headers = evidence.request("GET", "/", headers={"Accept": "text/html"}, name="homepage HTML")
    route_values["/"] = (home_status, home_body, home_headers)
    if home_status == 200:
        parser = LinkCollector()
        parser.feed(home_body.decode("utf-8", errors="replace"))
        json_ld_valid = parser.json_ld_count > 0
        for value in parser.json_ld_values:
            try:
                json.loads(value)
            except ValueError:
                json_ld_valid = False
        evidence.record("homepage includes valid JSON-LD", json_ld_valid, method="GET", path="/", detail=None if json_ld_valid else "missing or invalid application/ld+json block")
        for target in sorted(parser.links):
            path = _local_path(base_url, target)
            if not path or path.startswith(("/api/scans/", "/#")) or path == "#":
                continue
            status, _, _ = evidence.request("GET", path, name=f"homepage link {path}")
            evidence.record(f"homepage link {path}", status is not None and 200 <= status < 400, method="GET", path=path, status=status)

    # The API catalog should either be the OpenAPI document itself or advertise
    # its URL. Validate operation IDs, path params, and error response schemas.
    status, body, headers = get_route("/openapi.json", name="OpenAPI document")
    openapi = None
    if status is not None and 200 <= status < 300:
        openapi_type_ok = "json" in headers.get("Content-Type", "").lower()
        evidence.record("OpenAPI JSON content type", openapi_type_ok, method="GET", path="/openapi.json", content_type=headers.get("Content-Type"), detail=None if openapi_type_ok else "expected application/json")
        try:
            candidate = _decode_json(body)
            if isinstance(candidate, dict) and "paths" in candidate:
                openapi = candidate
        except (ValueError, UnicodeDecodeError):
            pass
    if openapi is None:
        evidence.record("OpenAPI contract", False, path="/.well-known/api-catalog", detail="API catalog did not resolve to an OpenAPI document")
    else:
        errors = _validate_openapi(openapi)
        evidence.record("OpenAPI contract", not errors, path="/.well-known/api-catalog", detail="; ".join(errors) if errors else None)

    # Check that server-generated HEAD responses retain GET metadata.
    for path in ("/", "/index.md", "/docs/llms.txt", "/.well-known/agent.json", "/mcp/server-card"):
        get_status, body, get_headers = route_values.get(path, (None, b"", {}))
        if get_status is None:
            get_status, body, get_headers = get_route(path)
        head_headers_request = {"Accept": "text/html"} if path == "/" else None
        head_status, head_body, head_headers = evidence.request("HEAD", path, headers=head_headers_request, name=f"HEAD {path}")
        ok = get_status == 200 and head_status == 200 and not head_body and head_headers.get("Content-Type") == get_headers.get("Content-Type") and head_headers.get("Content-Length") == str(len(body))
        evidence.record(f"HEAD metadata {path}", ok, method="HEAD", path=path, status=head_status, detail=None if ok else "HEAD metadata differs from GET")

    # A2A discovery is intentionally unsupported and should return a truthful
    # 404 rather than an invented partial capability.
    a2a_status, _, _ = evidence.request(
        "GET", "/.well-known/agent-card.json", name="A2A unsupported",
        expected=lambda status, _body, _headers: status == 404,
    )
    evidence.record("A2A is explicitly unsupported", a2a_status == 404, method="GET", path="/.well-known/agent-card.json", status=a2a_status)

    # Exercise the sessionless, read-only MCP path. No scan tool call is made.
    negotiated_version = None
    def rpc(payload, name):
        headers = {"Accept": "application/json, text/event-stream"}
        if payload.get("method") != "initialize" and negotiated_version:
            headers["MCP-Protocol-Version"] = negotiated_version
        return evidence.request(
            "POST", "/mcp", payload=payload,
            headers=headers,
            name=name,
        )

    init_status, init_body, init_headers = rpc({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pickable-live-verifier", "version": "1"}},
    }, "MCP initialize")
    init = None
    try:
        init = _decode_json(init_body)
    except (ValueError, UnicodeDecodeError):
        pass
    init_ok = init_status == 200 and isinstance(init, dict) and "result" in init and init.get("id") == 1 and not init_headers.get("Mcp-Session-Id")
    if isinstance(init, dict) and isinstance(init.get("result"), dict):
        negotiated_version = init["result"].get("protocolVersion")
        init_ok = init_ok and negotiated_version in ("2025-03-26", "2025-06-18") and init_headers.get("MCP-Protocol-Version") == negotiated_version
    else:
        init_ok = False
    evidence.record("MCP sessionless initialize", init_ok, method="POST", path="/mcp", status=init_status, detail=None if init_ok else "initialize did not return a supported sessionless JSON-RPC result and version header")

    notification_status, notification_body, _ = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, "MCP initialized notification")
    evidence.record("MCP notification accepted without response", notification_status == 202 and not notification_body, method="POST", path="/mcp", status=notification_status)

    ping_status, ping_body, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"}, "MCP ping")
    try:
        ping = _decode_json(ping_body)
    except (ValueError, UnicodeDecodeError):
        ping = None
    evidence.record("MCP ping", ping_status == 200 and isinstance(ping, dict) and ping.get("result") == {}, method="POST", path="/mcp", status=ping_status)

    tools_status, tools_body, _ = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "MCP tools/list")
    try:
        tools_doc = _decode_json(tools_body)
        tools = tools_doc.get("result", {}).get("tools", [])
    except (ValueError, UnicodeDecodeError, AttributeError):
        tools = []
    names = {item.get("name") for item in tools if isinstance(item, dict)}
    required_tools = {"describe_pickable", "list_scans", "get_scan_report"}
    evidence.record("MCP exposes read-only tools", tools_status == 200 and required_tools.issubset(names), method="POST", path="/mcp", status=tools_status, detail=None if required_tools.issubset(names) else f"missing {sorted(required_tools - names)}")

    def call_tool(call_id, name, arguments):
        status, body, _ = rpc({
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, f"MCP tool {name}")
        try:
            response = _decode_json(body)
        except (ValueError, UnicodeDecodeError):
            response = None
        result = response.get("result") if isinstance(response, dict) else None
        ok = status == 200 and isinstance(result, dict) and not result.get("isError") and bool(result.get("content") or result.get("structuredContent"))
        evidence.record(f"MCP tool {name} returns data", ok, method="POST", path="/mcp", status=status, detail=None if ok else "tool call returned no usable result")
        return result

    call_tool(4, "describe_pickable", {}) if "describe_pickable" in names else None
    scans_result = call_tool(5, "list_scans", {}) if "list_scans" in names else None
    if scan_id is None:
        scan_id = _extract_scan_id(scans_result)
    if scan_id:
        call_tool(6, "get_scan_report", {"job_id": scan_id})
    else:
        evidence.record("MCP existing report read", True, detail="skipped: no existing scan was advertised")
    evidence.record("MCP scan tool was not invoked", True, detail="verification made no scan-start call")

    summary = {
        "url": base_url,
        "status": "passed" if all(item["ok"] for item in evidence.checks) else "failed",
        "summary": {"passed": sum(item["ok"] for item in evidence.checks), "failed": sum(not item["ok"] for item in evidence.checks)},
        "checks": evidence.checks,
    }
    return summary


def _extract_scan_id(result):
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    candidates = [structured]
    for item in result.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                candidates.append(json.loads(item["text"]))
            except ValueError:
                pass
    for candidate in candidates:
        if isinstance(candidate, dict):
            scans = candidate.get("scans") or candidate.get("items")
            if isinstance(scans, list):
                for scan in scans:
                    if isinstance(scan, dict) and scan.get("id"):
                        return str(scan["id"])
            if candidate.get("id"):
                return str(candidate["id"])
        elif isinstance(candidate, list):
            for scan in candidate:
                if isinstance(scan, dict) and scan.get("id"):
                    return str(scan["id"])
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("PICKABLE_PUBLIC_URL", "https://solveny.dev"), help="public server origin (default: PICKABLE_PUBLIC_URL or https://solveny.dev)")
    parser.add_argument("--timeout", type=float, default=8.0, help="per-request timeout in seconds (default: 8)")
    parser.add_argument("--scan-id", help="also read this existing report; never starts a scan")
    parser.add_argument("--output", metavar="PATH", help="write JSON evidence to PATH, or '-' for stdout")
    args = parser.parse_args(argv)
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        parser.error("--url must be an HTTP(S) origin without path, query, or fragment")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    report = verify(args.url.rstrip("/"), args.timeout, args.scan_id)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(rendered)
    elif args.output:
        with open(args.output, "w", encoding="utf-8") as target:
            target.write(rendered)
    else:
        print(f"{report['status']}: {report['summary']['passed']} passed, {report['summary']['failed']} failed ({report['url']})")
        for item in report["checks"]:
            if not item["ok"]:
                suffix = f" — {item['detail']}" if item.get("detail") else ""
                print(f"FAIL {item['check']}{suffix}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
