"""Regression tests for the deep-research evidence pipeline.

Every test uses a frozen provider envelope (no network): the goal is to prove
that search evidence is preserved exactly when the provider returns it, and
that every `web_sources: 0` is attributed to its loss stage via research_trace.
"""
import json
import unittest
from unittest.mock import patch

from app.intelligence import IntelligenceProvider, extract_research_sources

ANSWER = {"product_summary": "Product", "target_customers": "Teams", "problem": "Coordination",
          "value_proposition": "Simpler planning", "competitive_landscape": "Rival X exists.",
          "workflows": [], "evidence_refs": ["homepage"], "unknowns": []}
EVIDENCE = {"ref": "homepage", "url": "https://example.com/",
            "title": "Example", "visible_text": "Product"}
OPENAI_ENV = {"INTELLIGENCE_PROVIDER": "openai", "OPENAI_API_KEY": "test-key",
              "OPENAI_MODEL": "", "OPENAI_QUICK_MODEL": "quick-model",
              "OPENAI_DEEP_MODEL": "deep-model"}


def _response(body, status=200):
    class Response:
        def read(self, limit):
            return json.dumps(body).encode()
    Response.status = status
    return Response()


def _fake_public_addresses(host, port):
    if host in ("127.0.0.1", "localhost") or host.startswith(("10.", "192.168.", "172.16.")):
        raise ValueError("Access to private, local or reserved IP addresses is blocked.")
    return ["93.184.216.34"]


def _analyze(envelope, status=200):
    with patch.dict("os.environ", OPENAI_ENV, clear=True), \
            patch("http.client.HTTPSConnection") as connection, \
            patch("app.intelligence.public_addresses", side_effect=_fake_public_addresses):
        connection.return_value.getresponse.return_value = _response(envelope, status)
        result = IntelligenceProvider().analyze(EVIDENCE, "deep")
        payload = json.loads(connection.return_value.request.call_args.kwargs["body"])
    return result, payload


def _message_envelope(annotations):
    return {"output_text": json.dumps(ANSWER),
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(ANSWER), "annotations": annotations}]}]}


class ResearchTraceTests(unittest.TestCase):
    def test_deep_payload_requests_search_action_sources(self):
        _, payload = _analyze({"output_text": json.dumps(ANSWER), "output": []})
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])

    def test_citation_annotations_propagate_with_trace(self):
        annotations = [{"type": "url_citation", "url": "https://rival.test/x",
                        "title": "Rival X"}]
        result, _ = _analyze(_message_envelope(annotations))
        self.assertEqual([(s["url"], s["origin"]) for s in result["web_sources"]],
                         [("https://rival.test/x", "citation")])
        trace = result["research_trace"]
        self.assertEqual(trace["search_calls"], 0)
        self.assertEqual(trace["raw_results"], 1)
        self.assertEqual(trace["parsed_sources"], 1)
        self.assertEqual(trace["report_citations"], 1)
        self.assertIsNone(trace["fallback_reason"])
        self.assertIsInstance(trace["latency_ms"], int)

    def test_search_action_sources_propagate_without_annotations(self):
        envelope = {"output_text": json.dumps(ANSWER), "output": [
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "rival x",
                        "sources": [{"url": "https://rival.test/x", "title": "Rival X"},
                                    {"url": "https://docs.test/y", "title": "Docs Y"}]}},
            {"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]}
        result, _ = _analyze(envelope)
        self.assertEqual([s["url"] for s in result["web_sources"]],
                         ["https://rival.test/x", "https://docs.test/y"])
        self.assertTrue(all(s["origin"] == "search" for s in result["web_sources"]))
        trace = result["research_trace"]
        self.assertEqual(trace["search_calls"], 1)
        self.assertEqual(trace["raw_results"], 2)
        self.assertEqual(trace["parsed_sources"], 2)

    def test_untitled_search_sources_get_url_derived_labels(self):
        # Production shape is {"type": "url", "url": ...} with no title key.
        envelope = {"output_text": json.dumps(ANSWER), "output": [
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "rival x",
                        "sources": [{"type": "url", "url": "https://rival.test/docs/pricing/plans"},
                                    {"type": "url", "url": "https://rival.test/"},
                                    {"url": "https://docs.test/y", "title": "Docs Y"}]}},
            {"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]}
        result, _ = _analyze(envelope)
        self.assertEqual([(s["url"], s["label"]) for s in result["web_sources"]],
                         [("https://rival.test/docs/pricing/plans", "rival.test — /docs/pricing"),
                          ("https://rival.test/", "rival.test"),
                          ("https://docs.test/y", "Docs Y")])
        self.assertIsNone(result["research_trace"]["fallback_reason"])

    def test_untitled_citation_gets_url_derived_label(self):
        annotations = [{"type": "url_citation", "url": "https://rival.test/x"}]
        result, _ = _analyze(_message_envelope(annotations))
        self.assertEqual(result["web_sources"],
                         [{"label": "rival.test — /x", "url": "https://rival.test/x", "origin": "citation"}])

    def test_no_search_calls_sets_fallback_reason(self):
        result, _ = _analyze({"output_text": json.dumps(ANSWER), "output": []})
        self.assertEqual(result["web_sources"], [])
        self.assertEqual(result["research_trace"]["fallback_reason"], "no_search_calls")
        self.assertIn("unverified", result["competitive_landscape"].lower())

    def test_search_without_sources_sets_fallback_reason(self):
        envelope = {"output_text": json.dumps(ANSWER), "output": [
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "rival x"}},
            {"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]}
        result, _ = _analyze(envelope)
        self.assertEqual(result["research_trace"]["fallback_reason"], "no_annotations_no_sources")

    def test_unfetchable_sources_are_filtered_and_counted(self):
        annotations = [{"type": "url_citation", "url": "http://plain.test/x", "title": "Plain"},
                       {"type": "url_citation", "url": "https://127.0.0.1/secret", "title": "Loop"},
                       {"type": "url_citation", "url": "https://ok.test/good", "title": "Good"}]
        result, _ = _analyze(_message_envelope(annotations))
        self.assertEqual([s["url"] for s in result["web_sources"]], ["https://ok.test/good"])
        trace = result["research_trace"]
        self.assertEqual(trace["raw_results"], 3)
        self.assertEqual(trace["parsed_sources"], 1)

    def test_all_filtered_sources_set_fallback_reason(self):
        annotations = [{"type": "url_citation", "url": "http://plain.test/x", "title": "Plain"}]
        result, _ = _analyze(_message_envelope(annotations))
        self.assertEqual(result["research_trace"]["fallback_reason"], "all_sources_filtered")

    def test_malformed_output_shapes_never_crash_or_invent_urls(self):
        for output in (None, {}, [{"type": "message"}],
                       [{"type": "message", "content": None}],
                       [{"type": "message", "content": [{"type": "output_text"}]}],
                       [{"type": "web_search_call", "action": None}],
                       [{"type": "web_search_call", "action": {"sources": "nope"}}]):
            sources, trace = extract_research_sources({"output": output, "output_text": "{}"})
            self.assertEqual(sources, [])
            self.assertIsNotNone(trace["fallback_reason"])
        sources, _ = extract_research_sources(None)
        self.assertEqual(sources, [])

    def test_provider_error_raises_without_sources(self):
        with patch.dict("os.environ", OPENAI_ENV, clear=True), \
                patch("http.client.HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = _response(
                {"error": {"code": "credit_balance_exhausted", "message": "nope"}}, 429)
            with self.assertRaises(RuntimeError):
                IntelligenceProvider().analyze(EVIDENCE, "deep")

    def test_public_projection_exposes_only_trace_counts(self):
        from server import public_intelligence
        annotations = [{"type": "url_citation", "url": "https://rival.test/x", "title": "Rival"}]
        result, _ = _analyze(_message_envelope(annotations))
        public = public_intelligence({"id": "x", "depth": "deep", "status": "completed", "report": result})
        trace = public["report"]["research_trace"]
        self.assertEqual(trace["report_citations"], 1)
        self.assertNotIn("provider_errors", trace)
        self.assertEqual(public["report"]["web_sources"],
                         [{"label": "Rival", "url": "https://rival.test/x"}])


if __name__ == "__main__":
    unittest.main()
