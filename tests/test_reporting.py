import json
import unittest
from app.reporting import build_report, report_from_job
from test_core import sources, ora_checks, oss_checks, ORA_IDS, OSS_IDS


class ScoreTests(unittest.TestCase):
    def test_all_defined_checks_pass_without_credit_for_unmeasured_layers(self):
        report = build_report("example.com", sources(
            ora_checks({i: "pass" for i in ORA_IDS.values()}),
            oss_checks({i: "pass" for i in OSS_IDS.values()})))
        self.assertEqual(report["score"]["total"], 80)
        self.assertEqual(report["score"]["coverage"], 80)
        self.assertIsNone(report["score"]["breakdown"]["security"]["points"])
        self.assertEqual(report["checks"][2]["scoring_source"], "agentready_oss")

    def test_all_fail_is_measured_zero_not_missing(self):
        report = build_report("example.com", sources(oss=oss_checks({i: "fail" for i in OSS_IDS.values()})))
        self.assertEqual(report["score"]["total"], 0)
        self.assertEqual(report["score"]["coverage"], 80)

    def test_unknown_excluded_from_layer_denominator_but_reduces_coverage(self):
        report = build_report("example.com", sources(oss=oss_checks({"sitemap": "pass", "linkHeaders": "error"})))
        self.assertEqual(report["score"]["breakdown"]["discovery"]["points"], 20)
        self.assertEqual(report["score"]["coverage"], 10)

    def test_disagreement_is_preserved_for_partial_match_without_consensus(self):
        report = build_report("example.com", sources(ora_checks({"llms-txt-exists": "pass"}), oss_checks({"llmsTxt": "fail"})))
        self.assertEqual(len(report["divergent_findings"]), 1)
        self.assertEqual(report["divergent_findings"][0]["combined_status"], "UNKNOWN")
        self.assertEqual(report["divergent_findings"][0]["scoring_status"], "FAIL")

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

    def test_malformed_raw_report_preserves_other_provider(self):
        report = report_from_job({"domain": "example.com"}, [
            {"source_id": "ora", "status": "failed", "raw_report": "{bad"},
            {"source_id": "agentready_oss", "status": "completed", "raw_report": oss_checks({"sitemap": "pass"})},
        ])
        self.assertEqual(report["checks"][0]["sources"]["agentready_oss"]["status"], "PASS")
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


if __name__ == "__main__":
    unittest.main()
