"""Frozen 12-case benchmark for the deep-research evidence pipeline.

No network: every case replays a frozen provider envelope through
`IntelligenceProvider.analyze` and asserts the trace + propagation contract.
Each case runs 3 times to prove determinism (frozen inputs, frozen outputs).

Case groups (mirroring the reliability plan):
  simple-*      (3): healthy provider answers with inline citations
  search-only-* (3): evidence only in web_search_call action.sources
  unclear-*     (2): ambiguous positioning -> no sources, explicit fallback
  discovery-*   (2): competitor discovery shapes, incl. partial retrieval
  negative-*    (2): provider error / truncated analysis -> no invented facts
"""
import json
import unittest
from unittest.mock import patch

from app.intelligence import IntelligenceProvider
from tests.test_research_trace import ANSWER, EVIDENCE, OPENAI_ENV, _fake_public_addresses


def _run(envelope, status=200):
    class Response:
        def read(self, limit):
            return json.dumps(envelope).encode()
    Response.status = status
    with patch.dict("os.environ", OPENAI_ENV, clear=True), \
            patch("http.client.HTTPSConnection") as connection, \
            patch("app.research.public_addresses", side_effect=_fake_public_addresses), \
            patch("app.intelligence.public_addresses", side_effect=_fake_public_addresses):
        connection.return_value.getresponse.return_value = Response()
        try:
            return IntelligenceProvider().analyze(EVIDENCE, "deep"), None
        except RuntimeError as error:
            return None, str(error)


def _msg(*annotations):
    return {"output_text": json.dumps(ANSWER), "output": [
        {"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(ANSWER),
             "annotations": list(annotations)}]}]}


def _cite(url, title):
    return {"type": "url_citation", "url": url, "title": title}


def _search_call(*sources, query="competitors"):
    return {"type": "web_search_call", "status": "completed",
            "action": {"type": "search", "query": query,
                       "sources": [{"url": u, "title": t} for u, t in sources]}}


CASES = [
    ("simple-cited-competitor",
     lambda: _msg(_cite("https://rival1.test/", "Rival One")), "propagate"),
    ("simple-multiple-citations",
     lambda: _msg(_cite("https://rival1.test/", "Rival One"),
                  _cite("https://rival2.test/pricing", "Rival Two Pricing")), "propagate"),
    ("simple-duplicate-urls",
     lambda: _msg(_cite("https://rival1.test/", "Rival One"),
                  _cite("https://rival1.test/", "Rival One Again")), "propagate"),
    ("search-only-action-sources",
     lambda: {"output_text": json.dumps(ANSWER), "output": [
         _search_call(("https://niche1.test/", "Niche One"),
                      ("https://niche2.test/", "Niche Two")),
         {"type": "message", "content": [
             {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]},
     "propagate"),
    ("search-only-single-source",
     lambda: {"output_text": json.dumps(ANSWER), "output": [
         _search_call(("https://micro.test/", "Micro SaaS")),
         {"type": "message", "content": [
             {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]},
     "propagate"),
    ("search-only-open-page-action",
     lambda: {"output_text": json.dumps(ANSWER), "output": [
         {"type": "web_search_call", "status": "completed",
          "action": {"type": "open_page", "url": "https://micro.test/deep"}},
         {"type": "message", "content": [
             {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}]},
     "fallback"),
    ("unclear-no-search-no-citations",
     lambda: {"output_text": json.dumps(ANSWER), "output": []}, "fallback"),
    ("unclear-empty-annotations",
     lambda: _msg(), "fallback"),
    ("discovery-partial-one-good-one-bad",
     lambda: _msg(_cite("https://rival1.test/", "Rival One"),
                  _cite("http://insecure.test/", "Insecure"),
                  _cite("https://127.0.0.1/admin", "Loopback")), "propagate"),
    ("discovery-malformed-shapes",
     lambda: {"output_text": json.dumps(ANSWER), "output": [
         {"type": "message"}, {"type": "message", "content": None},
         {"type": "web_search_call", "action": None},
         {"type": "unknown_future_type", "content": [{"annotations": "nope"}]}]},
     "fallback"),
    ("negative-provider-error",
     lambda: {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, "error"),
    ("negative-truncated-json",
     lambda: {"output_text": '{"product_summary": "Unfin', "output": []}, "error"),
]


class ResearchBenchmarkTests(unittest.TestCase):
    def test_frozen_cases_meet_propagation_contract_three_runs(self):
        propagated = 0
        for name, build, expectation in CASES:
            with self.subTest(case=name):
                for run in range(3):
                    envelope = build()
                    if expectation == "error" and name == "negative-provider-error":
                        result, error = _run(envelope, status=429)
                    else:
                        result, error = _run(envelope)
                    if expectation == "error":
                        self.assertIsNone(result, f"run {run}: must not return a report")
                        self.assertTrue(error, f"run {run}: must surface an error")
                        continue
                    self.assertIsNone(error, f"run {run}: unexpected error {error}")
                    trace = result["research_trace"]
                    urls = [s["url"] for s in result["web_sources"]]
                    self.assertEqual(len(urls), len(set(urls)), f"run {run}: duplicate URLs")
                    self.assertTrue(all(u.startswith("https://") for u in urls),
                                    f"run {run}: only public https URLs")
                    if expectation == "propagate":
                        self.assertGreater(len(urls), 0, f"run {run}: sources lost")
                        self.assertIsNone(trace["fallback_reason"], f"run {run}: {trace}")
                        propagated += 1
                    else:
                        self.assertEqual(urls, [], f"run {run}: must not invent sources")
                        self.assertIsNotNone(trace["fallback_reason"], f"run {run}: reason required")
                        self.assertIn("unverified", result["competitive_landscape"].lower())
        self.assertGreaterEqual(propagated, 5 * 3)


if __name__ == "__main__":
    unittest.main()
