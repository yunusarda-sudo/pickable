import json
import unittest
from app.reporting import build_report, report_from_job
from test_core import sources, ora_checks, ORA_IDS


class ScoreTests(unittest.TestCase):
    def test_all_defined_checks_pass_without_credit_for_unmeasured_layers(self):
        report = build_report("example.com", sources(
            ora_checks({i: "pass" for i in ORA_IDS.values()})))
        self.assertEqual(report["score"]["total"], 80)
        self.assertEqual(report["score"]["coverage"], 80)
        self.assertIsNone(report["score"]["breakdown"]["security"]["points"])
        self.assertEqual(report["checks"][2]["scoring_source"], "ora")

    def test_all_fail_is_measured_zero_not_missing(self):
        report = build_report("example.com", sources(ora_checks({i: "fail" for i in ORA_IDS.values()})))
        self.assertEqual(report["score"]["total"], 0)
        self.assertEqual(report["score"]["coverage"], 80)

    def test_unknown_excluded_from_layer_denominator_but_reduces_coverage(self):
        report = build_report("example.com", sources(ora_checks({"sitemap": "pass", "link-headers-discovery": "error"})))
        self.assertEqual(report["score"]["breakdown"]["discovery"]["points"], 20)
        self.assertEqual(report["score"]["coverage"], 10)

    def test_top_fixes_order_and_original_status_are_not_changed(self):
        raw = ora_checks({"sitemap": "warning"})
        raw["topFixes"] = [{"id": "b", "recommendation": "<script>alert(1)</script>"}, {"id": "a"}]
        before = json.dumps(raw)
        report = build_report("example.com", sources(raw))
        self.assertEqual(report["recommendations"], raw["topFixes"])
        self.assertEqual(json.dumps(raw), before)

    def test_provider_scanned_time_survives_storage_bridge(self):
        raw = ora_checks({"sitemap": "pass"})
        raw["scannedAt"] = "2025-01-01T00:00:00Z"
        report = report_from_job({"domain": "example.com"}, [{"source_id": "ora", "status": "completed", "raw_report": json.dumps(raw), "finished_at": "2026-10-10T12:00:00Z"}])
        self.assertEqual(report["checks"][0]["sources"]["ora"]["observed_at"], "2025-01-01T00:00:00Z")

    def test_malformed_raw_report_is_an_error_not_a_crash(self):
        report = report_from_job({"domain": "example.com"}, [
            {"source_id": "ora", "status": "failed", "raw_report": "{bad"}])
        self.assertIsNone(report["score"]["total"])
        self.assertIsNotNone(report["sources"][0]["error"])

    def test_malformed_provider_metadata_is_safe_without_changing_raw(self):
        raw = ora_checks({"sitemap": "pass"})
        raw.update(topFixes={"message": "bad"}, scannedAt={"value": "bad"},
                   score={"value": 100}, grade=["A"], analysisStatus={})
        before = json.dumps(raw)
        report = build_report("example.com", sources(raw))
        self.assertEqual(report["recommendations"], [])
        for key in ("score", "grade", "analysis_status"):
            self.assertIsNone(report["sources"][0][key])
        self.assertEqual(report["sources"][0]["observed_at"], "2026-10-10T12:00:00Z")
        self.assertEqual(json.dumps(raw), before)

    def test_missing_status_envelope_and_bad_metadata_do_not_crash(self):
        report = build_report("example.com", {"ora": {"raw": {"layers": []}}})
        self.assertEqual(report["sources"][0]["status"], "unavailable")

    def test_unrecognized_source_does_not_enter_score(self):
        report = report_from_job({"domain": "example.com"}, [{"source_id": "future", "status": "completed", "raw_report": {"score": 100}}])
        self.assertIsNone(report["score"]["total"])

    def test_bad_provider_envelope_is_insufficient_not_a_crash(self):
        for raw in ([1], {"layers": None}, {"layers": [{"checks": None}]}):
            with self.subTest(raw=raw):
                report = build_report("example.com", sources(ora=raw))
                self.assertIsNone(report["score"]["total"])

    def test_partial_results_never_show_what_a_running_source_could_override(self):
        cf = {"checks": {"discoverability": {"sitemap": {"status": "fail"}}}}
        ora = ora_checks({"sitemap": "pass"})
        # Cloudflare landed first: Ora outranks it, so nothing is decided and no score is final.
        early = build_report("example.com", {"cloudflare": {"status": "completed", "raw": cf}}, pending=["ora"])
        self.assertTrue(all(c["scoring_status"] == "PENDING" for c in early["checks"]))
        self.assertFalse(early["score"]["final"])
        # Ora landed first with a definitive result: final, even while Cloudflare runs.
        ora_first = build_report("example.com", sources(ora), pending=["cloudflare"])
        self.assertEqual(row_status(ora_first, "discovery.sitemap"), "PASS")
        # An undecided Ora check waits for the Cloudflare fallback.
        self.assertEqual(row_status(ora_first, "discovery.link_headers"), "PENDING")
        self.assertFalse(ora_first["score"]["final"])
        done = build_report("example.com", {**sources(ora), "cloudflare": {"status": "completed", "raw": cf}})
        self.assertEqual(row_status(done, "discovery.sitemap"), "PASS")
        self.assertTrue(done["score"]["final"])


def row_status(report, check_id):
    return next(c["scoring_status"] for c in report["checks"] if c["check_id"] == check_id)


if __name__ == "__main__":
    unittest.main()
