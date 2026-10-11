"""HTTP contract tests for Pickable's agent discovery and MCP surfaces.

These tests deliberately use a small in-memory backend. They exercise the real
HTTP handlers, while keeping calls to Ora and other external services out of
the test process.
"""

import json
import re
import socket
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit
from app.agent_policy import AgentPolicy, ScanLimiter, ScanRateLimitError
from server import make_server


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


def fixture_backend():
    report = {
        "domain": "example.com",
        "checks": [{"id": "robots-txt", "combined_status": "PASS"}],
        "summary": {"passed": 1},
    }
    jobs = {
        "existing-report": {
            "id": "existing-report",
            "domain": "example.com",
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "completed",
            "sources": [],
            "report": report,
        }
    }
    created = []

    def create(domain):
        job_id = f"created-{len(created) + 1}"
        created.append(domain)
        jobs[job_id] = {
            "id": job_id,
            "domain": domain,
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "pending",
            "sources": [],
            "report": None,
        }
        return job_id

    return {
        "mode": "test",
        "create": create,
        "get": jobs.get,
        "raw": lambda _job_id, _source: None,
        "preview": lambda _job_id: None,
        "list": lambda: [
            {key: value for key, value in job.items() if key not in ("report", "sources")}
            for job in jobs.values()
        ],
        "_jobs": jobs,
        "_created": created,
    }


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = set()
        self.json_ld = []
        self._capture = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for attribute in ("href", "src"):
            if attrs.get(attribute):
                self.links.add(attrs[attribute])
        if tag.lower() == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._capture = True
            self._buffer = []


    def handle_data(self, data):
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._capture:
            self.json_ld.append("".join(self._buffer))
            self._capture = False
            self._buffer = []


class AgentPolicyTests(unittest.TestCase):
    def test_defaults_are_read_only_and_limits_are_bounded(self):
        policy = AgentPolicy.from_env({})
        self.assertFalse(policy.agent_api_key)
        self.assertFalse(policy.mcp_scan_enabled)
        self.assertEqual(policy.scans_per_hour, 5)
        self.assertEqual(policy.global_scans_per_hour, 30)

    def test_mcp_scan_requires_explicit_flag_and_long_ascii_key(self):
        key = "a" * 32
        self.assertFalse(AgentPolicy.from_env({"PICKABLE_MCP_SCAN_ENABLED": "1"}).mcp_scan_enabled)
        self.assertFalse(AgentPolicy.from_env({"PICKABLE_AGENT_API_KEY": key}).mcp_scan_enabled)
        policy = AgentPolicy.from_env({"PICKABLE_AGENT_API_KEY": key, "PICKABLE_MCP_SCAN_ENABLED": "1"})
        self.assertTrue(policy.mcp_scan_enabled)
        with self.assertRaises(ValueError):
            AgentPolicy.from_env({"PICKABLE_AGENT_API_KEY": "short"})

    def test_public_url_and_rate_limit_settings_are_validated(self):
        with self.assertRaises(ValueError):
            AgentPolicy.from_env({"PICKABLE_PUBLIC_URL": "http://example.com"})
        with self.assertRaises(ValueError):
            AgentPolicy.from_env({"PICKABLE_SCANS_PER_HOUR": "0"})

    def test_global_limit_and_client_table_fail_closed(self):
        now = [0.0]
        limiter = ScanLimiter(AgentPolicy(), clock=lambda: now[0])
        for index in range(30):
            limiter.admit(f"client-{index}")
        with self.assertRaises(ScanRateLimitError):
            limiter.admit("client-30")
        self.assertEqual(len(limiter.events), 30)

        bounded = ScanLimiter(AgentPolicy(global_scans_per_hour=600), clock=lambda: now[0], max_clients=2)
        bounded.admit("client-a")
        bounded.admit("client-b")
        with self.assertRaises(ScanRateLimitError):
            bounded.admit("client-c")
        now[0] = 3601.0
        bounded.admit("client-c")
        self.assertEqual(set(bounded.clients), {"client-c"})


class AgentHTTPCase(unittest.TestCase):
    def setUp(self):
        self.backend = fixture_backend()
        self.policy = AgentPolicy()
        self.server = make_server(0, self.backend, policy=self.policy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, headers=None, method=None, timeout=3):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def json_request(self, path, payload, *, accept="application/json, text/event-stream", headers=None):
        request_headers = {"Content-Type": "application/json", "Accept": accept}
        request_headers.update(headers or {})
        return self.request(path, payload, request_headers, method="POST")

    def mcp(self, payload, headers=None):
        request_headers = {}
        if payload.get("method") != "initialize":
            request_headers["MCP-Protocol-Version"] = "2025-06-18"
        request_headers.update(headers or {})
        return self.json_request("/mcp", payload, headers=request_headers)


class DiscoveryResourceTests(AgentHTTPCase):
    def test_discovery_documents_are_served_with_content_types(self):
        for route in DISCOVERY_ROUTES:
            with self.subTest(route=route):
                status, body, headers = self.request(route)
                self.assertEqual(status, 200)
                self.assertTrue(body.strip(), f"{route} returned an empty document")
                self.assertTrue(headers.get("Content-Type"), f"{route} has no Content-Type")
                self.assertNotIn("text/html", headers["Content-Type"].lower())

    def test_structured_discovery_documents_are_well_formed(self):
        json_routes = (
            "/.well-known/agent.json",
            "/.well-known/ard.json",
            "/.well-known/ai-catalog.json",
            "/.well-known/api-catalog",
            "/.well-known/agent-skills/index.json",
        )
        for route in json_routes:
            with self.subTest(route=route):
                status, body, headers = self.request(route)
                self.assertEqual(status, 200)
                self.assertIn("json", headers.get("Content-Type", "").lower())
                parsed = json.loads(body)
                self.assertIsInstance(parsed, dict | list)

        # The legacy plugin manifest requires verified contact/legal metadata
        # that this deployment does not publish.
        self.assertEqual(self.request("/.well-known/ai-plugin.json")[0], 404)

        # A sitemap is a public discovery surface even when it is linked from
        # another manifest rather than directly listed in the well-known set.
        status, body, headers = self.request("/sitemap.xml")
        self.assertEqual(status, 200)
        self.assertIn("xml", headers.get("Content-Type", "").lower())
        root = ET.fromstring(body)
        locations = [node.text for node in root.iter() if node.tag.endswith("loc")]
        self.assertTrue(locations)
        for location in locations:
            parsed = urlsplit(location)
            self.assertIn(parsed.scheme, ("http", "https"), location)
            self.assertTrue(parsed.netloc, location)
            linked_status, _, _ = self.request(parsed.path)
            self.assertEqual(linked_status, 200, location)

    def test_openapi_matches_real_handlers_and_has_typed_error_responses(self):
        status, body, headers = self.request("/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("json", headers.get("Content-Type", "").lower())
        spec = json.loads(body)
        paths = spec["paths"]
        required = {
            ("/api/config", "get"),
            ("/api/scans", "get"),
            ("/api/scans", "post"),
            ("/api/scans/{job_id}", "get"),
            ("/mcp", "post"),
        }
        available = {
            (path, method.lower())
            for path, item in paths.items()
            for method in item
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
        }
        self.assertTrue(required.issubset(available))

        def dereference(value, section):
            if not isinstance(value, dict) or "$ref" not in value:
                return value
            ref = value["$ref"]
            prefix = f"#/components/{section}/"
            if not ref.startswith(prefix):
                return value
            result = spec.get("components", {}).get(section, {}).get(ref[len(prefix):])
            return result if isinstance(result, dict) else value

        def parameter_names(values):
            result = set()
            for value in values:
                parameter = dereference(value, "parameters")
                if isinstance(parameter, dict) and isinstance(parameter.get("name"), str):
                    result.add(parameter["name"])
            return result

        operation_ids = []
        for path, item in paths.items():
            placeholders = set(re.findall(r"\{([^}]+)\}", path))
            for method, operation in item.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                operation_ids.append(operation["operationId"])
                params = parameter_names(item.get("parameters", []))
                params.update(parameter_names(operation.get("parameters", [])))
                self.assertTrue(placeholders.issubset(params), (path, placeholders, params))
                error_responses = [
                    dereference(response, "responses") for code, response in operation.get("responses", {}).items()
                    if str(code).startswith(("4", "5"))
                ]
                self.assertTrue(error_responses, (method, path, "missing error responses"))
                for response in error_responses:
                    schemas = [media.get("schema") for media in response.get("content", {}).values()]
                    self.assertTrue(any(isinstance(schema, dict) for schema in schemas), (method, path, response))
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_markdown_negotiation_and_agent_mode_are_cache_safe(self):
        for path in ("/index", "/about", "/api", "/docs", "/pricing", "/auth", "/skill"):
            route = path + ".md"
            with self.subTest(route=route):
                status, body, headers = self.request(route, headers={"Accept": "text/markdown"})
                self.assertEqual(status, 200)
                self.assertIn("markdown", headers.get("Content-Type", "").lower())
                self.assertTrue(body.strip())
                self.assertIn("accept", headers.get("Vary", "").lower())

        status, body, headers = self.request("/?mode=agent", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertIn("markdown", headers.get("Content-Type", "").lower())
        self.assertTrue(body.strip())
        self.assertIn("accept", headers.get("Vary", "").lower())

        status, body, headers = self.request("/", headers={"Accept": "text/markdown"})
        self.assertEqual(status, 200)
        self.assertIn("markdown", headers.get("Content-Type", "").lower())
        self.assertIn("accept", headers.get("Vary", "").lower())

    def test_head_matches_get_metadata_without_returning_body(self):
        for route in ("/", "/index.md", "/docs/llms.txt", "/.well-known/agent.json"):
            with self.subTest(route=route):
                get_status, get_body, get_headers = self.request(route)
                head_status, head_body, head_headers = self.request(route, method="HEAD")
                self.assertEqual(get_status, 200)
                self.assertEqual(head_status, 200)
                self.assertEqual(head_body, b"")
                self.assertEqual(head_headers.get("Content-Type"), get_headers.get("Content-Type"))
                self.assertEqual(int(head_headers["Content-Length"]), len(get_body))

    def test_homepage_jsonld_assets_and_local_links(self):
        status, body, headers = self.request("/", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", "").lower())
        page = PageLinks()
        page.feed(body.decode("utf-8"))
        self.assertTrue(page.json_ld, "homepage should expose JSON-LD metadata")
        for block in page.json_ld:
            self.assertIsInstance(json.loads(block), dict | list)

        for target in sorted(page.links):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or target.startswith(("mailto:", "tel:")):
                continue
            path = parsed.path or "/"
            if path == "/" and parsed.fragment:
                continue
            with self.subTest(target=target):
                linked_status, _, _ = self.request(path + ("?" + parsed.query if parsed.query else ""))
                self.assertGreaterEqual(linked_status, 200)
                self.assertLess(linked_status, 400)

    def test_a2a_is_reported_as_unsupported(self):
        status, body, _ = self.request("/.well-known/agent-card.json")
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body.lower())


class MCPContractTests(AgentHTTPCase):
    def test_sessionless_initialize_ping_and_read_tools(self):
        status, body, headers = self.mcp({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "readiness-test", "version": "1"},
            },
        })
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertNotIn("Mcp-Session-Id", headers)
        initialized = json.loads(body)
        self.assertEqual(initialized["id"], 1)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(headers.get("MCP-Protocol-Version"), "2025-06-18")
        self.assertIn("capabilities", initialized["result"])

        notification_status, notification_body, _ = self.mcp({
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })
        self.assertEqual(notification_status, 202)
        self.assertEqual(notification_body, b"")

        ping_status, ping_body, _ = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(ping_status, 200)
        self.assertEqual(json.loads(ping_body)["result"], {})

        list_status, list_body, _ = self.mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(list_status, 200)
        tools = json.loads(list_body)["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue({"describe_pickable", "list_scans", "get_scan_report"}.issubset(names))
        self.assertNotIn("start_website_scan", names)
        self.assertTrue(all("inputSchema" in tool for tool in tools))

        for call_id, tool_name, arguments in (
            (4, "describe_pickable", {}),
            (5, "list_scans", {}),
            (6, "get_scan_report", {"job_id": "existing-report"}),
        ):
            with self.subTest(tool=tool_name):
                call_status, call_body, _ = self.mcp({
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                })
                self.assertEqual(call_status, 200)
                result = json.loads(call_body)
                self.assertNotIn("error", result)
                self.assertFalse(result["result"].get("isError", False))
                self.assertTrue(result["result"].get("content"))
                structured = result["result"].get("structuredContent")
                if structured is None:
                    structured = json.loads(result["result"]["content"][0]["text"])
                if tool_name == "list_scans":
                    self.assertIn("existing-report", {item["id"] for item in structured["scans"]})
                if tool_name == "get_scan_report":
                    self.assertEqual(structured["job_id"], "existing-report")
                    self.assertEqual(structured["report"]["checks"][0]["id"], "robots-txt")
        self.assertEqual(self.backend["_created"], [], "read-only tools must not start scans")

    def test_protocol_errors_use_json_rpc_codes(self):
        invalid = (
            (b"{", 400, -32700),
            (json.dumps({"jsonrpc": "2.0", "id": 1}).encode(), 400, -32600),
        )
        for raw, status_expected, code in invalid:
            with self.subTest(code=code):
                status, body, _ = self.request(
                    "/mcp", raw,
                    {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                    method="POST",
                )
                self.assertEqual(status, status_expected)
                self.assertEqual(json.loads(body)["error"]["code"], code)

        calls = (
            ({"jsonrpc": "2.0", "id": 7, "method": "no/such/method"}, -32601),
            ({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}}, -32602),
            ({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_scan_report", "arguments": {}}}, -32602),
        )
        for payload, code in calls:
            with self.subTest(payload=payload):
                status, body, _ = self.mcp(payload)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["error"]["code"], code)

    def test_initialize_rejects_unusable_protocol_and_client_envelopes(self):
        unsupported = {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}
        status, body, _ = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": unsupported})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")

        for params in (
            {"protocolVersion": "2025-03-26", "capabilities": {}},
            {"protocolVersion": "2025-06-18", "clientInfo": {"name": "x", "version": "1"}},
        ):
            with self.subTest(params=params):
                status, body, _ = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["error"]["code"], -32602)

        status, body, _ = self.json_request(
            "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "ping"},
            headers={"MCP-Protocol-Version": "1999-01-01"},
        )
        self.assertEqual(status, 400)
        self.assertTrue(json.loads(body).get("error"))

        for version in ("2025-03-26", "2025-06-18"):
            with self.subTest(protocol=version):
                status, body, _ = self.mcp({
                    "jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                })
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["result"]["protocolVersion"], version)

    def test_wrong_http_method_and_media_negotiation_are_explicit(self):
        get_status, _, get_headers = self.request("/mcp")
        self.assertEqual(get_status, 405)
        self.assertIn("POST", get_headers.get("Allow", ""))

        status, _, _ = self.request("/mcp", b"{}", {"Content-Type": "text/plain", "Accept": "application/json, text/event-stream"}, method="POST")
        self.assertEqual(status, 415)
        status, _, _ = self.request("/mcp", b"{}", {"Content-Type": "application/json", "Accept": "text/plain"}, method="POST")
        self.assertEqual(status, 406)

    def test_mcp_body_size_and_content_length_are_bounded(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        status, body, _ = self.request("/mcp", b" " * (32 * 1024 + 1), headers, method="POST")
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)

        status, body, _ = self.request("/mcp", b"{}", {**headers, "Content-Length": "invalid"}, method="POST")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)


class RESTSecurityTests(AgentHTTPCase):
    def csrf_headers(self):
        status, body, _ = self.request("/api/config")
        self.assertEqual(status, 200)
        return {"Content-Type": "application/json", "X-CSRF-Token": json.loads(body)["csrf_token"]}

    def test_post_validation_has_distinct_statuses(self):
        csrf = self.csrf_headers()

        status, body, _ = self.request("/api/scans", b"{", {**csrf, "Content-Type": "application/json"}, method="POST")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body).get("code"), "invalid_json")

        status, _, _ = self.request("/api/scans", b"{}", {**csrf, "Content-Type": "text/plain"}, method="POST")
        self.assertEqual(status, 415)

        status, _, _ = self.request("/api/scans", b"{}", {**csrf, "Content-Type": "application/json", "Content-Length": "4097"}, method="POST")
        self.assertEqual(status, 413)

        status, _, _ = self.request("/api/scans", b"{}", {**csrf, "Content-Type": "application/json", "Content-Length": "not-a-number"}, method="POST")
        self.assertEqual(status, 400)

    def test_bearer_api_key_can_authorize_rest_scan(self):
        from app.agent_policy import AgentPolicy

        policy = AgentPolicy(agent_api_key="test-rest-key-" + "x" * 24)
        backend = fixture_backend()
        server = make_server(0, backend, policy=policy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            req = urllib.request.Request(
                base + "/api/scans",
                data=json.dumps({"domain": "example.com"}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer test-rest-key-" + "x" * 24},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read())["job_id"], "created-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_bad_bearer_does_not_authorize_rest_scan(self):
        policy = AgentPolicy(agent_api_key="k" * 32)
        backend = fixture_backend()
        server = make_server(0, backend, policy=policy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            req = urllib.request.Request(
                base + "/api/scans",
                data=json.dumps({"domain": "example.com"}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + "x" * 32},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
            self.assertEqual(backend["_created"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_shared_scan_limit_is_enforced_before_backend_mutation(self):
        # The default is five starts per client per hour. Every request below
        # uses the same peer address; the sixth must be rejected before create.
        self.assertEqual(self.server.scan_limiter.max_clients, 1024)
        csrf = self.csrf_headers()
        outcomes = []
        for _ in range(6):
            status, body, headers = self.request(
                "/api/scans", {"domain": "example.com"}, csrf
            )
            outcomes.append((status, body, headers))
        self.assertEqual([status for status, _, _ in outcomes], [202] * 5 + [429])
        self.assertTrue(outcomes[-1][2].get("Retry-After"))
        self.assertEqual(len(self.backend["_created"]), 5)

    def test_enabled_mcp_scan_requires_bearer_and_reads_back_job(self):
        policy = AgentPolicy(agent_api_key="m" * 32, mcp_scan_enabled=True)
        backend = fixture_backend()
        server = make_server(0, backend, policy=policy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def send(arguments, authorization=None):
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
            if authorization:
                headers["Authorization"] = authorization
            request = urllib.request.Request(
                base + "/mcp",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "start_website_scan", "arguments": arguments},
                }).encode(),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, response.read(), response.headers
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read(), error.headers

        def rest_scan():
            request = urllib.request.Request(
                base + "/api/scans", data=json.dumps({"domain": "example.com"}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + "m" * 32},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                error.close()
                return error.code

        try:
            init_request = urllib.request.Request(
                base + "/mcp", data=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "scan-test", "version": "1"}},
                }).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, method="POST",
            )
            with urllib.request.urlopen(init_request, timeout=3) as response:
                self.assertEqual(json.loads(response.read())["result"]["protocolVersion"], "2025-06-18")
            notification = urllib.request.Request(
                base + "/mcp", data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}, method="POST",
            )
            with urllib.request.urlopen(notification, timeout=3) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.read(), b"")

            scan_notification = urllib.request.Request(
                base + "/mcp", data=json.dumps({
                    "jsonrpc": "2.0", "method": "tools/call",
                    "params": {"name": "start_website_scan", "arguments": {"domain": "example.com"}},
                }).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18", "Authorization": "Bearer " + "m" * 32},
                method="POST",
            )
            with urllib.request.urlopen(scan_notification, timeout=3) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.read(), b"")
            self.assertEqual(backend["_created"], [], "JSON-RPC notifications must never run tools")
            list_request = urllib.request.Request(
                base + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}, method="POST",
            )
            with urllib.request.urlopen(list_request, timeout=3) as response:
                tools = json.loads(response.read())["result"]["tools"]
            self.assertIn("start_website_scan", {tool["name"] for tool in tools})

            status, _, headers = send({"domain": "example.com"})
            self.assertEqual(status, 401)
            self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
            status, _, _ = send({"domain": "example.com"}, "Bearer " + "x" * 32)
            self.assertEqual(status, 401)
            self.assertEqual(backend["_created"], [])

            self.assertEqual([rest_scan() for _ in range(4)], [202] * 4)

            status, body, _ = send({"domain": "example.com"}, "Bearer " + "m" * 32)
            self.assertEqual(status, 200)
            result = json.loads(body)["result"]
            self.assertEqual(result["structuredContent"]["job_id"], "created-5")
            self.assertEqual(result["structuredContent"]["domain"], "example.com")
            status, body, _ = send({"domain": "example.com"}, "Bearer " + "m" * 32)
            self.assertEqual(status, 200)
            limited = json.loads(body)["result"]
            self.assertTrue(limited["isError"])
            self.assertIn("Scan limit reached", limited["content"][0]["text"])
            self.assertEqual(backend["_created"], ["example.com"] * 5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_slow_request_body_returns_408(self):
        csrf = self.csrf_headers()["X-CSRF-Token"]
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            connection.sendall((
                "POST /api/scans HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"X-CSRF-Token: {csrf}\r\n"
                "Content-Length: 40\r\n"
                "Connection: close\r\n\r\n"
                "{\"domain\":"
            ).encode())
            connection.settimeout(12)
            chunks = []
            while True:
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    self.fail("server did not enforce its 10-second request-body timeout")
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
            self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
            self.assertIn(b'"request_timeout"', response)
            self.assertEqual(self.backend["_created"], [])
        finally:
            connection.close()

    def test_host_and_origin_checks_cover_agent_endpoints(self):
        for path, body in (("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}), ("/.well-known/agent.json", None)):
            status, _, _ = self.request(path, body, {"Host": "attacker.example", "Content-Type": "application/json"}, method="POST" if body else None)
            self.assertEqual(status, 403)
            status, _, _ = self.request(path, body, {"Origin": "https://attacker.example", "Content-Type": "application/json"}, method="POST" if body else None)
            self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
