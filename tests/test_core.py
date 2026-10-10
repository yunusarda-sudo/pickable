"""Security boundaries and honest provider comparison contracts."""
import json
import socket
import unittest
from unittest.mock import patch

from app.security import normalize_domain, public_addresses
from app.reporting import build_report


ORA_IDS = {
    "discovery.sitemap": "sitemap",
    "discovery.link_headers": "link-headers-discovery",
    "content.llms_txt": "llms-txt-exists",
    "content.markdown": "markdown-negotiation-vary",
    "auth.oauth_metadata": "mcp-oauth-metadata",
    "auth.protected_resource": "oauth-protected-resource",
    "integration.mcp_card": "mcp-server-card",
}
OSS_IDS = {
    "discovery.sitemap": "sitemap",
    "discovery.link_headers": "linkHeaders",
    "content.llms_txt": "llmsTxt",
    "content.markdown": "markdownNegotiation",
    "auth.oauth_metadata": "oauthDiscovery",
    "auth.protected_resource": "oauthProtectedResource",
    "integration.mcp_card": "mcpServerCard",
}


def sources(ora=None, oss=None):
    def envelope(raw):
        return {"status": "completed" if raw is not None else "unavailable",
                "raw": raw, "observed_at": "2026-10-10T12:00:00Z"}
    return {"ora": envelope(ora), "agentready_oss": envelope(oss)}


def ora_checks(entries):
    return {"contractVersion": "1.27.0", "layers": [
        {"id": "accessibility", "checks": [
            {"id": cid, "status": status, "details": "native Ora evidence"}
            for cid, status in entries.items()]}]}


def oss_checks(entries):
    return {"scanner": "agent_ready_scan.py 1.0.0", "checks": {
        "discoverability": {cid: {"status": status, "message": "native OSS evidence",
                                    "details": {}, "evidence": []}
                            for cid, status in entries.items()}}}


def row(report, check_id):
    return next(check for check in report["checks"] if check["check_id"] == check_id)


def addr(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (ip, 443))


class DomainSecurityTests(unittest.TestCase):
    def test_normalizes_hostname_and_https_root(self):
        for value in ("EXAMPLE.COM", " example.com ", "https://Example.com/"):
            with self.subTest(value=value):
                self.assertEqual(normalize_domain(value), "example.com")

    def test_rejects_ambiguous_or_nonpublic_targets(self):
        bad = ["", None, [], "localhost", "127.0.0.1", "[::1]", "https://[::1]/",
               "https://user:secret@example.com/", "example.com/path",
               "https://example.com/path", "https://example.com/?a=1",
               "https://example.com/#fragment", "example.com:443",
               "https://example.com:443/", "example.com\\evil", "a b.com"]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises((ValueError, TypeError)):
                    normalize_domain(value)

    @patch("socket.getaddrinfo")
    def test_all_public_dns_addresses_are_returned_without_duplicates(self, lookup):
        lookup.return_value = [addr("93.184.216.34"), addr("2606:4700:4700::1111"),
                               addr("93.184.216.34")]
        self.assertEqual(set(public_addresses("example.com", 443)),
                         {"93.184.216.34", "2606:4700:4700::1111"})

    @patch("socket.getaddrinfo")
    def test_one_private_answer_rejects_entire_mixed_dns_result(self, lookup):
        for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1",
                        "::1", "fc00::1", "::ffff:127.0.0.1", "0.0.0.0"):
            with self.subTest(private=private):
                lookup.return_value = [addr("93.184.216.34"), addr(private)]
                with self.assertRaises(ValueError):
                    public_addresses("example.com", 443)

    @patch("socket.getaddrinfo", return_value=[])
    def test_empty_dns_is_rejected(self, lookup):
        with self.assertRaises(ValueError):
            public_addresses("example.com", 443)


class ReportingTests(unittest.TestCase):
    def test_missing_sources_produce_seven_unknown_rows_and_no_score(self):
        report = build_report("example.com", sources())
        self.assertEqual({r["check_id"] for r in report["checks"]}, set(ORA_IDS))
        for check in report["checks"]:
            self.assertEqual(check["combined_status"], "UNKNOWN")
            self.assertTrue(check["reason"])
            self.assertIn("category", check)
            self.assertIn("match_type", check)
        score = report["score"]
        self.assertIsNone(score["total"])
        self.assertEqual(score["coverage"], 0)
        for layer, weight in {"discovery": 20, "content": 25, "usability": 35,
                              "security": 10, "payments": 10}.items():
            self.assertEqual(score["breakdown"][layer]["max_points"], weight)
            self.assertIsNone(score["breakdown"][layer]["points"])

    def test_equivalent_matching_passes_are_consensus(self):
        report = build_report("example.com", sources(
            ora_checks({"sitemap": "pass"}), oss_checks({"sitemap": "pass"})))
        check = row(report, "discovery.sitemap")
        self.assertEqual(check["match_type"], "equivalent")
        self.assertEqual(check["combined_status"], "PASS")

    def test_equivalent_pass_fail_is_different_and_not_positive_score(self):
        report = build_report("example.com", sources(
            ora_checks({"sitemap": "pass"}), oss_checks({"sitemap": "fail"})))
        check = row(report, "discovery.sitemap")
        self.assertEqual(check["combined_status"], "DIFFERENT")
        self.assertNotEqual(check.get("scoring_status"), "PASS")

    def test_partial_pairs_never_manufacture_consensus(self):
        report = build_report("example.com", sources(
            ora_checks({cid: "pass" for cid in ORA_IDS.values()}),
            oss_checks({cid: "pass" for cid in OSS_IDS.values()})))
        for check_id in set(ORA_IDS) - {"discovery.sitemap", "discovery.link_headers"}:
            with self.subTest(check_id=check_id):
                check = row(report, check_id)
                self.assertNotEqual(check["match_type"], "equivalent")
                self.assertEqual(check["combined_status"], "UNKNOWN")

    def test_native_states_survive_normalization(self):
        expected = {"warning": "WARNING", "warn": "WARNING", "pending": "UNKNOWN",
                    "na": "NOT_APPLICABLE", "neutral": "UNKNOWN",
                    "skip": "NOT_APPLICABLE", "error": "UNKNOWN"}
        for native, normalized in expected.items():
            with self.subTest(native=native):
                report = build_report("example.com", sources(
                    ora_checks({"sitemap": native}), oss_checks({"sitemap": native})))
                for provider in ("ora", "agentready_oss"):
                    source = row(report, "discovery.sitemap")["sources"][provider]
                    self.assertEqual(source["native_status"], native)
                    self.assertEqual(source["status"], normalized)
                    self.assertEqual(source["raw"]["status"], native)
                self.assertIsNone(report["score"]["total"])
                self.assertEqual(report["score"]["coverage"], 0)

    def test_one_unavailable_provider_does_not_erase_other_evidence(self):
        report = build_report("example.com", sources(oss=oss_checks({"sitemap": "pass"})))
        check = row(report, "discovery.sitemap")
        self.assertEqual(check["sources"]["agentready_oss"]["status"], "PASS")
        self.assertEqual(check["sources"]["ora"]["status"], "UNKNOWN")
        self.assertIsNotNone(report["score"]["total"])
        self.assertGreater(report["score"]["coverage"], 0)
        self.assertLess(report["score"]["coverage"], 100)
        self.assertIsNone(report["score"]["breakdown"]["payments"]["points"])

    def test_unmapped_provider_rows_remain_available(self):
        report = build_report("example.com", sources(
            ora_checks({"ora-future-check": "warning"}),
            oss_checks({"oss-future-check": "skip"})))
        preserved = json.dumps(report["source_specific"])
        self.assertIn("ora-future-check", preserved)
        self.assertIn("oss-future-check", preserved)
        self.assertIn("native Ora evidence", preserved)
        self.assertIn("native OSS evidence", preserved)


if __name__ == "__main__":
    unittest.main()
