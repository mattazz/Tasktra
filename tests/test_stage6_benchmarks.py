"""Stage 6 deterministic benchmark comparison acceptance tests."""

from __future__ import annotations

import unittest

from tasktra.benchmarking import (
    BenchmarkError, BenchmarkHarness, BenchmarkObservation, BenchmarkPlan, compare_observations,
)


class BenchmarkTests(unittest.TestCase):
    def test_comparison_flags_observed_regressions_without_estimated_savings(self):
        baseline = [BenchmarkObservation("retrieval", ("doc-a", "doc-b"), 100, 0, 0)]
        candidate = [BenchmarkObservation("retrieval", ("doc-a", "doc-a", "doc-b"), 140, 1, 2)]
        report = compare_observations(baseline, candidate)
        self.assertEqual(
            [finding.kind for finding in report.findings],
            ["duplicate-retrieval", "avoidable-context-growth", "unnecessary-escalation", "retry-regression"],
        )
        self.assertNotIn("saving", repr(report).casefold())

    def test_new_distinct_retrieval_does_not_claim_context_growth_is_avoidable(self):
        report = compare_observations(
            [BenchmarkObservation("lookup", ("doc-a",), 100)],
            [BenchmarkObservation("lookup", ("doc-a", "doc-b"), 150)],
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
