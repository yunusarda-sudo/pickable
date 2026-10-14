"""Sprint B gate: the 20-case labeled set must stay green.

Guards accuracy, N/A accuracy, evidence coverage, reproducibility and
the zero-fabrication rule. The mocked set is a regression net, not a
replacement for the live-site pilot.
"""
import unittest

from scripts.eval_agentready import evaluate


class EvalGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluate()

    def test_case_count(self):
        self.assertGreaterEqual(self.report["metrics"]["cases"], 20)

    def test_accuracy_is_perfect_on_labeled_set(self):
        self.assertEqual(self.report["metrics"]["accuracy"], 1.0)

    def test_na_accuracy_is_perfect(self):
        self.assertEqual(self.report["metrics"]["na_accuracy"], 1.0)

    def test_evidence_coverage_is_total(self):
        self.assertEqual(self.report["metrics"]["evidence_coverage"], 1.0)

    def test_retest_reproducibility(self):
        self.assertEqual(self.report["metrics"]["reproducibility"], 1.0)

    def test_no_false_positives_or_negatives(self):
        self.assertEqual(self.report["metrics"]["false_positives"], 0)
        self.assertEqual(self.report["metrics"]["false_negatives"], 0)

    def test_no_fabricated_endpoint_advice(self):
        self.assertTrue(self.report["metrics"]["fabrication_free"])
        self.assertTrue(self.report["passed"])


if __name__ == "__main__":
    unittest.main()
