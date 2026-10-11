import unittest
from unittest.mock import patch

from app.intelligence import extract_page_evidence, load_skill_instructions, select_research_urls, validate_analysis
from app.security import normalize_site_url
from server import public_intelligence


class IntelligenceContractTests(unittest.TestCase):
    def test_openai_quick_model_uses_legacy_model_as_compatible_fallback(self):
        from app.intelligence import IntelligenceProvider

        with patch.dict("os.environ", {"INTELLIGENCE_PROVIDER": "openai", "OPENAI_API_KEY": "test-key",
                                        "OPENAI_MODEL": "legacy-model", "OPENAI_QUICK_MODEL": "",
                                        "OPENAI_DEEP_MODEL": ""}, clear=True):
            provider = IntelligenceProvider()

        self.assertEqual(provider.model_for("short"), "legacy-model")
        self.assertEqual(provider.model_for("deep"), "gpt-6.1-sol")

    def test_openai_models_have_distinct_defaults_without_legacy_override(self):
        from app.intelligence import IntelligenceProvider

        with patch.dict("os.environ", {"INTELLIGENCE_PROVIDER": "openai", "OPENAI_API_KEY": "test-key",
                                        "OPENAI_MODEL": "", "OPENAI_QUICK_MODEL": "", "OPENAI_DEEP_MODEL": ""}, clear=True):
            provider = IntelligenceProvider()

        self.assertEqual(provider.model_for("short"), "gpt-6-luna")
        self.assertEqual(provider.model_for("deep"), "gpt-6.1-sol")

    def test_openai_deep_analysis_uses_deep_model_and_limits_web_search_calls(self):
        import json
        from app.intelligence import IntelligenceProvider

        answer = {"product_summary": "Product", "target_customers": "Teams", "problem": "Coordination",
                  "value_proposition": "Simpler planning", "competitive_landscape": "Not assessed.",
                  "workflows": [], "evidence_refs": ["homepage"], "unknowns": []}

        class Response:
            status = 200
            def read(self, limit):
                return json.dumps({"output_text": json.dumps(answer), "output": []}).encode()

        evidence = {"ref": "homepage", "url": "https://example.com/", "title": "Example", "visible_text": "Product"}
        with patch.dict("os.environ", {"INTELLIGENCE_PROVIDER": "openai", "OPENAI_API_KEY": "test-key",
                                        "OPENAI_MODEL": "legacy-model", "OPENAI_QUICK_MODEL": "quick-model",
                                        "OPENAI_DEEP_MODEL": "deep-model"}, clear=True), \
                patch("http.client.HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = Response()
            IntelligenceProvider().analyze(evidence, "deep")

        payload = json.loads(connection.return_value.request.call_args.kwargs["body"])
        self.assertEqual(payload["model"], "deep-model")
        self.assertEqual(payload["reasoning"]["effort"], "low")
        self.assertIn({"type": "web_search"}, payload["tools"])
        self.assertEqual(payload["max_tool_calls"], 2)

    def test_openai_short_analysis_uses_quick_model_without_web_search(self):
        import json
        from app.intelligence import IntelligenceProvider

        answer = {"product_summary": "Product", "target_customers": "Teams", "problem": "Coordination",
                  "value_proposition": "Simpler planning", "competitive_landscape": "Not assessed.",
                  "workflows": [], "evidence_refs": ["homepage"], "unknowns": []}

        class Response:
            status = 200
            def read(self, limit):
                return json.dumps({"output_text": json.dumps(answer), "output": []}).encode()

        evidence = {"ref": "homepage", "url": "https://example.com/", "title": "Example", "visible_text": "Product"}
        with patch.dict("os.environ", {"INTELLIGENCE_PROVIDER": "openai", "OPENAI_API_KEY": "test-key",
                                        "OPENAI_MODEL": "legacy-model", "OPENAI_QUICK_MODEL": "quick-model",
                                        "OPENAI_DEEP_MODEL": "deep-model"}, clear=True), \
                patch("http.client.HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = Response()
            IntelligenceProvider().analyze(evidence, "short")

        payload = json.loads(connection.return_value.request.call_args.kwargs["body"])
        self.assertEqual(payload["model"], "quick-model")
        self.assertEqual(payload["reasoning"]["effort"], "none")
        self.assertFalse(any(tool.get("type") == "web_search" for tool in payload.get("tools", [])))

    def test_quick_analysis_is_source_linked_and_marks_every_unverified_claim(self):
        import app.intelligence as intelligence
        quick_analysis = getattr(intelligence, "quick_analysis", None)
        self.assertTrue(callable(quick_analysis), "quick_analysis must exist as the deterministic evidence-first path")

        evidence = {"url": "https://example.com/", "ref": "homepage", "title": "TeamFlow",
                    "description": "Planning for distributed teams", "visible_text": "Keep teams aligned.",
                    "retrieved_at": "2026-10-11T00:00:00Z", "sha256": "abc"}

        report = quick_analysis(evidence)

        self.assertEqual(report["analysis_stage"], "preliminary")
        self.assertIn("TeamFlow", report["product_summary"])
        self.assertIn("ön", report["product_summary"].lower())
        self.assertEqual(report["evidence_refs"], ["homepage"])
        self.assertTrue(report["sources"])
        self.assertEqual(report["sources"][0]["url"], evidence["url"])
        self.assertIn("doğrulanmadı", report["target_customers"].lower())
        self.assertIn("doğrulanmadı", report["problem"].lower())
        self.assertIn("doğrulanmadı", report["value_proposition"].lower())
        self.assertIn("doğrulanmadı", report["competitive_landscape"].lower())

    def test_quick_analysis_handles_empty_homepage_without_inventing_a_product(self):
        import app.intelligence as intelligence
        quick_analysis = getattr(intelligence, "quick_analysis", None)
        self.assertTrue(callable(quick_analysis), "quick_analysis must exist as the deterministic evidence-first path")

        report = quick_analysis({"url": "https://example.com/", "ref": "homepage", "visible_text": ""})

        self.assertEqual(report["analysis_stage"], "preliminary")
        self.assertEqual(report["evidence_refs"], ["homepage"])
        self.assertIn("yeterli", report["product_summary"].lower())

    def test_extracts_bounded_visible_page_evidence(self):
        html = b'''<html><head><title>TeamFlow</title><meta name="description" content="Async planning for remote teams"></head>
        <body><nav><a href="/pricing">Pricing</a></nav><main><h1>Keep distributed teams aligned</h1>
        <p>Plan work across time zones with shared weekly updates.</p><script>secret()</script></main></body></html>'''
        evidence = extract_page_evidence("https://example.com/", 200, {"content-type": "text/html"}, html)
        self.assertEqual(evidence["title"], "TeamFlow")
        self.assertEqual(evidence["description"], "Async planning for remote teams")
        self.assertIn("Keep distributed teams aligned", evidence["visible_text"])
        self.assertNotIn("secret", evidence["visible_text"])
        self.assertEqual(len(evidence["sha256"]), 64)
        self.assertIn("/pricing", evidence["links"])

    def test_fetch_evidence_publishes_homepage_before_discovered_pages(self):
        from app.intelligence import fetch_evidence
        from unittest.mock import patch
        homepage = {"url": "https://example.com/", "ref": "homepage", "links": ["/pricing"]}
        pricing = {"url": "https://example.com/pricing", "ref": "page-1", "links": []}
        observed = []
        def fetch(url, ref, timeout=12):
            observed.append(ref)
            return homepage if ref == "homepage" else pricing
        with patch("app.intelligence._fetch_page", side_effect=fetch):
            result = fetch_evidence("https://example.com/", on_homepage=lambda page: observed.append("published:" + page["ref"]))
        self.assertEqual(observed, ["homepage", "published:homepage", "page-1"])
        self.assertEqual([page["ref"] for page in result["pages"]], ["homepage", "page-1"])

    def test_analysis_requires_source_linked_claims_and_explicit_unknowns(self):
        analysis = {
            "product_summary": "Planning software for distributed teams.",
            "target_customers": "Remote team leads",
            "problem": "Updates are scattered.",
            "value_proposition": "One shared weekly view.",
            "competitive_landscape": "Not assessed in short mode.",
            "workflows": ["Publish a weekly update"],
            "evidence_refs": ["homepage"],
            "unknowns": ["Pricing not verified"],
        }
        self.assertEqual(validate_analysis(analysis)["product_summary"], analysis["product_summary"])
        analysis["evidence_refs"] = []
        with self.assertRaises(ValueError):
            validate_analysis(analysis)
        analysis["evidence_refs"] = ["page-1"]
        self.assertEqual(validate_analysis(analysis, ["homepage", "page-1"])["evidence_refs"], ["page-1"])

    def test_selects_discovered_product_pages_not_invented_paths(self):
        urls = select_research_urls("https://example.com/", ["/login", "/pricing", "https://other.com/docs", "/about"], "example.com", 2)
        self.assertEqual(urls, ["https://example.com/pricing", "https://example.com/about"])

    def test_runtime_skill_instructions_are_available(self):
        text = load_skill_instructions()
        self.assertIn("SaaS Site Intelligence", text)
        self.assertIn("actor → input", text)

    def test_without_provider_or_key_analysis_is_unavailable_not_local(self):
        # Ollama is strictly opt-in: with no INTELLIGENCE_PROVIDER and no hosted
        # key, the provider must report unavailable instead of assuming a local
        # Ollama instance exists (it won't on Render/VPS or a closed laptop).
        from app.intelligence import IntelligenceProvider
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": ""}, clear=False):
            provider = IntelligenceProvider()
        self.assertEqual(provider.provider, "unavailable")
        self.assertFalse(provider.available)
        self.assertIsNone(provider.model)
        self.assertIn("not configured", provider.unavailable_reason)
        with self.assertRaises(RuntimeError):
            provider.analyze({"visible_text": "randevu"}, "short")

    def test_explicit_ollama_opt_in_still_works(self):
        from app.intelligence import IntelligenceProvider
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama"}, clear=False):
            provider = IntelligenceProvider()
        self.assertEqual(provider.provider, "ollama")
        self.assertTrue(provider.available)
        self.assertEqual(provider.model, "qwen3:8b")
        self.assertEqual(provider.ollama_base_url, "http://127.0.0.1:11434")

    def test_ollama_can_use_configured_private_network_endpoint(self):
        from app.intelligence import IntelligenceProvider
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama",
                                        "OLLAMA_BASE_URL": "http://ollama:11434"}, clear=False):
            provider = IntelligenceProvider()
        self.assertEqual((provider.ollama_host, provider.ollama_port), ("ollama", 11434))

    def test_ollama_endpoint_rejects_credentials_and_paths(self):
        from app.intelligence import IntelligenceProvider
        for url in ("http://user:password@ollama:11434", "http://ollama:11434/admin", "file:///tmp/model"):
            with self.subTest(url=url), patch.dict("os.environ", {"OLLAMA_BASE_URL": url}), self.assertRaises(ValueError):
                IntelligenceProvider()

    def test_ollama_returns_validated_schema_response(self):
        import json
        from app.intelligence import ANALYSIS_SCHEMA, IntelligenceProvider

        answer = {"product_summary": "Randevu yönetim sistemi", "target_customers": "Klinikler",
                  "problem": "Randevuların dağınık takibi", "value_proposition": "Tek yerde planlama",
                  "competitive_landscape": "Rakipler değerlendirilmedi.", "workflows": ["Randevu oluşturma"],
                  "evidence_refs": ["homepage"], "unknowns": ["Fiyatlandırma doğrulanmadı"]}
        class Response:
            status = 200
            def read(self, limit):
                return json.dumps({"message": {"content": json.dumps(answer, ensure_ascii=False)}}).encode()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama"}), \
                patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = Response()
            result = IntelligenceProvider().analyze({"visible_text": "randevu"}, "short")
        self.assertEqual(result["product_summary"], answer["product_summary"])
        self.assertEqual(result["competitive_landscape"], "Rakip araştırması bu kısa analizde yapılmadı.")
        connection.assert_called_once_with("127.0.0.1", port=11434, timeout=70)
        args, kwargs = connection.return_value.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/chat"))
        payload = json.loads(kwargs["body"])
        self.assertEqual(payload["model"], "qwen3:8b")
        self.assertEqual(payload["format"], ANALYSIS_SCHEMA)
        self.assertEqual(payload["tools"][0]["function"]["name"], "fetch_page")
        self.assertFalse(payload["stream"])

    def test_ollama_fetches_discovered_same_origin_pages_then_analyzes(self):
        import json
        from app.intelligence import IntelligenceProvider

        homepage = {"ref": "homepage", "url": "https://example.com/", "http_status": 200,
                    "title": "TeamFlow", "description": "", "visible_text": "Weekly planning",
                    "links": ["/pricing", "/login"], "retrieved_at": "2026-10-11T00:00:00Z"}
        pricing = {"ref": "page-1", "url": "https://example.com/pricing", "http_status": 200,
                   "title": "Pricing", "description": "", "visible_text": "29 USD per month",
                   "links": [], "retrieved_at": "2026-10-11T00:00:01Z"}
        answer = {"product_summary": "Planning software.", "target_customers": "Remote leads",
                  "problem": "Scattered updates.", "value_proposition": "One weekly view.",
                  "competitive_landscape": "Not assessed.", "workflows": ["Publish a weekly update"],
                  "evidence_refs": ["homepage", "page-1"], "unknowns": []}
        tool_reply = {"message": {"content": "", "tool_calls": [{"function": {"name": "fetch_page",
                    "arguments": {"url": "https://example.com/pricing"}}}]}}
        final_reply = {"message": {"content": json.dumps(answer)}}
        responses = [type("R", (), {"status": 200, "read": lambda self, limit, body=json.dumps(body).encode(): body})()
                     for body in (tool_reply, final_reply)]
        fetched = []
        def fetch_page(url, ref):
            fetched.append((url, ref))
            return pricing
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama"}), \
                patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = responses
            result = IntelligenceProvider().analyze({"pages": [homepage]}, "short", fetch_page=fetch_page)
        self.assertEqual(fetched, [("https://example.com/pricing", "page-1")])
        self.assertEqual(result["evidence_refs"], ["homepage", "page-1"])
        self.assertEqual(connection.return_value.request.call_count, 2)

    def test_ollama_refuses_invented_or_off_origin_fetch_targets(self):
        import json
        from app.intelligence import IntelligenceProvider

        homepage = {"ref": "homepage", "url": "https://example.com/", "http_status": 200,
                    "title": "TeamFlow", "description": "", "visible_text": "Weekly planning",
                    "links": ["/pricing"], "retrieved_at": "2026-10-11T00:00:00Z"}
        answer = {"product_summary": "Planning software.", "target_customers": "Remote leads",
                  "problem": "Scattered updates.", "value_proposition": "One weekly view.",
                  "competitive_landscape": "Not assessed.", "workflows": ["Publish a weekly update"],
                  "evidence_refs": ["homepage"], "unknowns": ["Pricing not fetched"]}
        tool_reply = {"message": {"content": "", "tool_calls": [{"function": {"name": "fetch_page",
                    "arguments": {"url": "https://evil.test/secret"}}}]}}
        final_reply = {"message": {"content": json.dumps(answer)}}
        responses = [type("R", (), {"status": 200, "read": lambda self, limit, body=json.dumps(body).encode(): body})()
                     for body in (tool_reply, final_reply)]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama"}), \
                patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = responses
            IntelligenceProvider().analyze({"pages": [homepage]}, "short", fetch_page=lambda url, ref: (_ for _ in ()).throw(AssertionError("must not fetch")))
        second = json.loads(connection.return_value.request.call_args_list[1].kwargs["body"])
        self.assertIn("not an allowed same-origin", second["messages"][-1]["content"])

    def test_local_deep_mode_does_not_claim_web_or_competitor_research(self):
        import json
        from app.intelligence import IntelligenceProvider
        answer = {"product_summary": "Product", "target_customers": "Teams", "problem": "Coordination",
                  "value_proposition": "Simpler planning", "competitive_landscape": "No competitors exist.",
                  "workflows": [], "evidence_refs": ["homepage"], "unknowns": [f"Unknown {i}" for i in range(12)]}
        class Response:
            status = 200
            def read(self, limit): return json.dumps({"message": {"content": json.dumps(answer)}}).encode()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "INTELLIGENCE_PROVIDER": "ollama"}), \
                patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = Response()
            result = IntelligenceProvider().analyze({"visible_text": "page"}, "deep")
        self.assertIn("Rakip araştırması yapılmadı", result["competitive_landscape"])
        self.assertEqual(len(result["unknowns"]), 12)
        self.assertIn("no web search", connection.return_value.request.call_args.kwargs["body"].decode())

    def test_site_url_keeps_safe_query_but_drops_fragment_and_rejects_credentials(self):
        self.assertEqual(normalize_site_url("https://example.com/pricing"), ("example.com", "https://example.com/pricing"))
        self.assertEqual(normalize_site_url("https://example.com/pricing?plan=team&utm_source=demo#plans"),
                         ("example.com", "https://example.com/pricing?plan=team&utm_source=demo"))
        for value in ("http://127.0.0.1/admin", "example.com/?access_token=secret", "https://user@example.com/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_site_url(value)

    def test_public_projection_removes_provider_and_raw_fields(self):
        public = public_intelligence({"id": "x", "depth": "short", "status": "running", "provider_trace": "secret",
            "report": {"analysis_stage": "preliminary", "product_summary": "Summary", "prompt": "private", "raw_html": "<script>secret</script>",
                       "sources": [{"label": "Home", "url": "https://example.com/", "sha256": "abc", "body": "secret"}]}})
        self.assertNotIn("provider_trace", public)
        self.assertEqual(public["report"]["analysis_stage"], "preliminary")
        self.assertNotIn("prompt", public["report"])
        self.assertNotIn("raw_html", public["report"])
        self.assertNotIn("body", public["report"]["sources"][0])
        for key in ("model", "usage", "finish_reason", "error"):
            self.assertNotIn(key, public)

    def test_own_host_evidence_falls_back_to_loopback(self):
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from app.intelligence import fetch_evidence

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b"<html><head><title>Own</title></head><body><p>Own product.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {"PORT": str(server.server_port), "ALLOWED_HOSTS": "own.test",
                   "PICKABLE_PUBLIC_URL": "https://own.test", "RENDER_EXTERNAL_HOSTNAME": ""}
            with patch.dict(os.environ, env, clear=False):
                result = fetch_evidence("https://own.test/", extra_pages=0)
            self.assertEqual(result["pages"][0]["url"], "https://own.test/")
            self.assertEqual(result["pages"][0]["title"], "Own")
        finally:
            server.shutdown()
            server.server_close()

    def test_large_homepage_within_cap_is_analyzed(self):
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from app.intelligence import fetch_evidence

        big = "<html><head><title>Big</title></head><body><p>" + ("x" * 1_200_000) + "</p></body></html>"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = big.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {"PORT": str(server.server_port), "ALLOWED_HOSTS": "own.test",
                   "PICKABLE_PUBLIC_URL": "https://own.test", "RENDER_EXTERNAL_HOSTNAME": ""}
            with patch.dict(os.environ, env, clear=False):
                result = fetch_evidence("https://own.test/", extra_pages=0)
            self.assertEqual(result["pages"][0]["title"], "Big")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
