"""Stage 6 deterministic benchmark comparison acceptance tests."""

from __future__ import annotations

import unittest

from tasktra.benchmarking import (
    BenchmarkError, BenchmarkHarness, BenchmarkObservation, BenchmarkPlan, compare_observations,
)


class BenchmarkTests(unittest.TestCase):
    def test_comparison_flags_observed_regressions_without_estimated_savings(self):
        baseline = [BenchmarkObservation("retrieval", ("doc-a", "doc-b"), 100, 0, 0, validation_outcome="passed")]
        candidate = [BenchmarkObservation("retrieval", ("doc-a", "doc-a", "doc-b"), 140, 1, 2, validation_outcome="passed")]
        report = compare_observations(baseline, candidate)
        self.assertEqual(
            [finding.kind for finding in report.findings],
            ["duplicate-retrieval", "avoidable-context-growth", "escalation-increase", "retry-regression"],
        )
        self.assertNotIn("saving", repr(report).casefold())

    def test_new_distinct_retrieval_does_not_claim_context_growth_is_avoidable(self):
        report = compare_observations(
            [BenchmarkObservation("lookup", ("doc-a",), 100, validation_outcome="passed")],
            [BenchmarkObservation("lookup", ("doc-a", "doc-b"), 150, validation_outcome="passed")],
        )
        self.assertEqual(report.findings, ())

    def test_report_preserves_only_measured_efficiency_and_quality_observations(self):
        report = compare_observations(
            [{
                "scenario": "measured", "retrievals": ["evidence-a"], "evidence_count": 2,
                "context_tokens": 120, "retry_count": 0, "elapsed_ms": 400,
                "validation_outcome": "passed", "human_interventions": 1,
            }],
            [{
                "scenario": "measured", "retrievals": ["evidence-a"], "evidence_count": 2,
                "context_tokens": 120, "retry_count": 0, "elapsed_ms": 350,
                "validation_outcome": "passed", "human_interventions": 0,
            }],
        )
        self.assertEqual(report.measurements[0]["baseline"]["elapsed_ms"], 400)
        self.assertEqual(report.measurements[0]["candidate"]["validation_outcome"], "passed")
        self.assertEqual(report.measurements[0]["candidate"]["human_interventions"], 0)
        self.assertNotIn("savings", repr(report).casefold())

    def test_quality_improvement_dominates_additional_recovery_work(self):
        report = compare_observations(
            [BenchmarkObservation("recovery", ("doc-a",), 100, validation_outcome="failed")],
            [BenchmarkObservation(
                "recovery", ("doc-a", "doc-a"), 200, escalation_count=1,
                retry_count=2, validation_outcome="passed", elapsed_ms=1000,
            )],
        )
        self.assertEqual(report.findings, ())
        self.assertFalse(report.has_regressions)
        self.assertTrue(report.is_acceptable)
        self.assertEqual(report.measurements[0]["quality_assessment"], "improved")
        self.assertEqual(report.measurements[0]["candidate"]["retry_count"], 2)

    def test_less_work_with_worse_validation_is_always_a_regression(self):
        for outcome in ("failed", "not-run", "unknown"):
            with self.subTest(outcome=outcome):
                report = compare_observations(
                    [BenchmarkObservation("quality", ("a", "b"), 100, retry_count=2, validation_outcome="passed")],
                    [BenchmarkObservation("quality", (), 0, validation_outcome=outcome)],
                )
                self.assertEqual([item.kind for item in report.findings], ["validation-regression"])
                self.assertTrue(report.has_regressions)
                self.assertFalse(report.is_acceptable)

    def test_unverified_quality_cannot_win_and_failed_equivalence_is_not_acceptable(self):
        for before, after in (("not-run", "not-run"), ("unknown", "passed"), ("failed", "unknown"), ("failed", "failed")):
            with self.subTest(before=before, after=after):
                report = compare_observations(
                    [BenchmarkObservation("unverified", ("doc-a",), 100, validation_outcome=before)],
                    [BenchmarkObservation("unverified", (), 0, validation_outcome=after)],
                )
                self.assertFalse(report.is_acceptable)
                self.assertFalse(report.has_regressions)
                self.assertEqual(report.has_unverified_quality, before != "failed" or after != "failed")

    def test_equivalent_passing_observations_are_not_regressions(self):
        observation = BenchmarkObservation("same", ("doc-a", "doc-a"), 100, validation_outcome="passed")
        report = compare_observations([observation], [observation])
        self.assertEqual(report.findings, ())
        self.assertEqual(report.measurements[0]["quality_assessment"], "equivalent")
        self.assertTrue(report.is_acceptable)

    def test_representative_paired_scenarios_keep_measurements_and_quality_separate(self):
        plan = BenchmarkPlan(("retrieval-reuse", "recovery", "dropped-validation"))
        baseline = [
            BenchmarkObservation("retrieval-reuse", ("a", "a", "b"), 200, validation_outcome="passed"),
            BenchmarkObservation("recovery", ("a",), 100, validation_outcome="failed"),
            BenchmarkObservation("dropped-validation", ("a", "b"), 150, validation_outcome="passed"),
        ]
        candidate = [
            BenchmarkObservation("retrieval-reuse", ("a", "b"), 140, validation_outcome="passed"),
            BenchmarkObservation("recovery", ("a", "b"), 180, retry_count=1, validation_outcome="passed"),
            BenchmarkObservation("dropped-validation", ("a",), 50, validation_outcome="not-run"),
        ]
        report = BenchmarkHarness(plan).compare(baseline, candidate)
        self.assertEqual([(item.kind, item.scenario) for item in report.findings], [("validation-regression", "dropped-validation")])
        self.assertFalse(report.is_acceptable)
        self.assertEqual(len(report.measurements), 3)

    def test_plan_and_comparison_are_deterministic_and_require_matching_scenarios(self):
        plan = BenchmarkPlan(("alpha", "beta"))
        harness = BenchmarkHarness(plan)
        baseline = [BenchmarkObservation("beta"), BenchmarkObservation("alpha")]
        candidate = [BenchmarkObservation("alpha"), BenchmarkObservation("beta")]
        self.assertEqual(harness.compare(baseline, candidate).compared_scenarios, ("alpha", "beta"))
        with self.assertRaisesRegex(BenchmarkError, "same representative scenarios"):
            compare_observations([BenchmarkObservation("alpha")], [BenchmarkObservation("beta")])

    def test_adversarial_bounds_and_closed_input_shape_are_rejected(self):
        with self.assertRaisesRegex(BenchmarkError, "at most"):
            BenchmarkObservation("bounded", tuple(f"item-{index}" for index in range(65)))
        with self.assertRaisesRegex(BenchmarkError, "unsupported"):
            BenchmarkObservation.from_mapping({"scenario": "bounded", "prompt": "secret instructions"})
        with self.assertRaisesRegex(BenchmarkError, "unique"):
            BenchmarkPlan(("same", "same"))


if __name__ == "__main__":
    unittest.main()
