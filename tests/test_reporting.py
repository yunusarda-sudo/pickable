import json
import unittest
from app.reporting import build_report, report_from_job
from test_core import sources, ora_checks, ORA_IDS, row


class ScoreTests(unittest.TestCase):
    def test_failed_source_evidence_is_preserved_but_never_scored(self):
        for source in ("ora", "cloudflare"):
            raw = (ora_checks({"sitemap": "pass"}) if source == "ora" else
                   {"checks": {"discoverability": {"sitemap": {"status": "pass"}}}})
            for status in ("failed", "running", "unavailable"):
                with self.subTest(source=source, status=status):
                    report = build_report("example.com", {source: {"status": status, "raw": raw}})
                    self.assertIsNone(report["score"]["total"])
                    self.assertIsNone(row(report, "discovery.sitemap")["scoring_source"])
                    self.assertEqual(row(report, "discovery.sitemap")["sources"][source]["native_status"], "pass")

    def test_cached_receipt_time_is_not_observation_time(self):
        raw = ora_checks({"sitemap": "fail"})
        raw.pop("scannedAt", None)
        raw.update(servedFromCache=True, resultAgeSeconds=86400)
        runs = [
            {"source_id": "ora", "status": "completed", "raw_report": raw,
             "finished_at": "2026-10-11T12:00:00Z"},
            {"source_id": "cloudflare", "status": "completed", "raw_report": {
                "scannedAt": "2026-10-11T11:59:00Z",
                "checks": {"discoverability": {"sitemap": {"status": "pass"}}}}},
        ]
        result = report_from_job({"domain": "example.com"}, runs)
        self.assertEqual(row(result, "discovery.sitemap")["scoring_source"], "cloudflare")
        self.assertEqual(result["sources"][0]["observed_at"], "2026-10-10T12:00:00+00:00")

    def test_newest_equivalent_definitive_observation_wins(self):
        for ora_time, cf_time, expected in (
            ("2025-01-01T00:00:00Z", "2026-10-10T12:00:00Z", "cloudflare"),
            ("2026-10-10T12:00:00Z", "2025-01-01T00:00:00Z", "ora"),
            ("2026-10-10T12:00:00Z", "2026-10-10T15:00:00+03:00", "ora"),
            ("not-a-date", "2026-10-10T12:00:00Z", "ora"),
            ("2026-10-10T12:00:00Z", "not-a-date", "ora"),
        ):
            with self.subTest(ora_time=ora_time, cf_time=cf_time):
                raw = ora_checks({"sitemap": "pass"})
                raw["scannedAt"] = ora_time
                report = build_report("example.com", {
                    "ora": {"status": "completed", "raw": raw, "served_from_cache": True},
                    "cloudflare": {"status": "completed", "observed_at": cf_time,
                                   "raw": {"checks": {"discoverability": {"sitemap": {"status": "fail"}}}}},
                })
                check = row(report, "discovery.sitemap")
                self.assertEqual(check["scoring_source"], expected)
                self.assertEqual(check["scoring_status"], "FAIL" if expected == "cloudflare" else "PASS")

    def test_source_disagreement_only_flags_opposite_definitive_results(self):
        for ora_status, cf_status, expected in (
            ("pass", "fail", True), ("fail", "pass", True),
            ("pass", "pass", False), ("warning", "fail", False),
        ):
            with self.subTest(ora_status=ora_status, cf_status=cf_status):
                report = build_report("example.com", {
                    **sources(ora_checks({"sitemap": ora_status})),
                    "cloudflare": {"status": "completed", "raw": {
                        "checks": {"discoverability": {"sitemap": {"status": cf_status}}}}},
                })
                self.assertIs(row(report, "discovery.sitemap")["source_disagreement"], expected)

    def test_cloudflare_markdown_observation_cannot_score_stricter_ora_definition(self):
        report = build_report("example.com", {"cloudflare": {"status": "completed", "raw": {
            "checks": {"content": {"markdownNegotiation": {"status": "pass", "message": "Markdown returned"}}}}}})
        check = row(report, "content.markdown")
        self.assertEqual(check["sources"]["cloudflare"]["native_status"], "pass")
        self.assertIsNone(check["scoring_source"])
        self.assertIsNone(report["score"]["total"])
        self.assertIn("vary", check["reason"].lower())

    def test_source_summary_exposes_cache_provenance_and_result_age(self):
        raw = ora_checks({"sitemap": "pass"})
        raw["scannedAt"] = "2026-10-10T12:00:00Z"
        raw.update(servedFromCache=True, resultAgeSeconds=60)
        report = report_from_job({"domain": "example.com"}, [{
            "source_id": "ora", "status": "completed", "raw_report": json.dumps(raw),
            "finished_at": "2026-10-10T12:00:59Z",
        }])
        summary = report["sources"][0]
        self.assertIs(summary["served_from_cache"], True)
        self.assertEqual(summary["result_age_seconds"], 60)

    def test_newer_narrower_markdown_pass_requires_recheck_of_old_cached_failure(self):
        ora = ora_checks({"markdown-negotiation-vary": "fail"})
        ora.update(scannedAt="2026-10-11T04:00:00Z", servedFromCache=True)
        cf = {"scannedAt": "2026-10-11T08:00:00Z", "checks": {
            "content": {"markdownNegotiation": {"status": "pass"}}}}
        report = build_report("example.com", {**sources(ora), "cloudflare": {
            "status": "completed", "raw": cf}})
        check = row(report, "content.markdown")
        self.assertEqual(check["scoring_status"], "UNKNOWN")
        self.assertIsNone(check["scoring_source"])
        self.assertIn("recheck", check["reason"].lower())

    def test_all_defined_checks_pass_without_credit_for_unmeasured_layers(self):
        report = build_report("example.com", sources(
            ora_checks({i: "pass" for i in ORA_IDS.values()})))
        # Layer weights 20/25/35 are relative; a perfect run is 100/100 at full coverage.
        self.assertEqual(report["score"]["total"], 100)
        self.assertEqual(report["score"]["coverage"], 100)
        self.assertEqual(report["checks"][2]["scoring_source"], "ora")

    def test_all_fail_is_measured_zero_not_missing(self):
        report = build_report("example.com", sources(ora_checks({i: "fail" for i in ORA_IDS.values()})))
        self.assertEqual(report["score"]["total"], 0)
        self.assertEqual(report["score"]["coverage"], 100)

    def test_unknown_excluded_from_layer_denominator_but_reduces_coverage(self):
        report = build_report("example.com", sources(ora_checks({"sitemap": "pass", "link-headers-discovery": "error"})))
        self.assertEqual(report["score"]["breakdown"]["discovery"]["points"], 20)
        self.assertEqual(report["score"]["coverage"], 12.5)  # 10 of 80 weight points

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
        # Either source may return newer evidence, so shared checks keep waiting.
        ora_first = build_report("example.com", sources(ora), pending=["cloudflare"])
        self.assertEqual(row_status(ora_first, "discovery.sitemap"), "PENDING")
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
