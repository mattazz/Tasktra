import unittest

from tasktra.efficiency import EfficiencyError, compare_trials, compare_verified_trials, summarize_executions


USAGE = {"input_tokens": 10, "cached_input_tokens": 2, "cache_write_input_tokens": 1,
         "output_tokens": 5, "reasoning_output_tokens": 3, "total_tokens": 15}


def record(work_id, **extra):
    value = {"work_id": work_id, "role": "implementer", "state": "succeeded", "provider": "codex",
             "usage": dict(USAGE), "usage_provenance": "host-callback", "observed_model": "gpt-test"}
    value.update(extra)
    return value


def trial(mode, **extra):
    value = {"scenario": "checkout", "replicate": "one", "mode": mode, "config_fingerprint": "same-config",
             "acceptance_fingerprint": "same-acceptance", "success": True, "validation_outcome": "passed"}
    value.update(extra)
    return value


class EfficiencyTests(unittest.TestCase):
    def test_summary_streams_and_reports_unknown_subsets_and_outcomes(self):
        records = (record(f"work-{index}", state="failed" if index == 1 else "cancelled" if index == 2 else "succeeded",
                          usage={**USAGE, "cached_input_tokens": None} if index == 0 else dict(USAGE))
                   for index in range(3))
        result = summarize_executions(records)
        self.assertEqual(result["coverage"]["records"], 3)
        self.assertEqual(result["totals"]["known"]["total_tokens"], 45)
        self.assertIsNone(result["totals"]["subsets"]["cached_input_tokens"])
        self.assertEqual(result["totals"]["outcome_total_tokens"], {"succeeded": 15, "failed": 15, "cancelled": 15, "non_success": 30})

    def test_subset_parent_invariant_and_untrusted_usage_are_withheld(self):
        invalid = record("invalid", usage={**USAGE, "cached_input_tokens": 11})
        manual = record("manual", usage_provenance="manual-assertion")
        result = summarize_executions([invalid, manual])
        self.assertEqual(result["totals"]["known"]["total_tokens"], 0)
        self.assertEqual(result["coverage"]["invalid_usage"], 1)
        self.assertEqual(result["coverage"]["untrusted_usage"], 1)

    def test_only_exact_immutable_duplicate_is_deduplicated(self):
        first = record("same", response_fingerprints=["a", "b"], source_sha256="source")
        exact_copy = dict(first)
        result = summarize_executions([first, exact_copy])
        self.assertEqual((result["coverage"]["deduplicated_records"], result["totals"]["known"]["total_tokens"]), (1, 15))
        conflicting = record("other", response_fingerprints=["b", "c"], source_sha256="source")
        result = summarize_executions([first, conflicting])
        self.assertEqual(result["totals"]["known"]["total_tokens"], 0)
        self.assertEqual(result["coverage"]["ambiguous_receipts"], 2)
        self.assertFalse(result["totals"]["complete"])

    def test_thread_scope_alone_is_not_deduplicated(self):
        first = record("first", thread_id="same")
        second = record("second", thread_id="same")
        self.assertEqual(summarize_executions([first, second])["totals"]["known"]["total_tokens"], 30)

    def test_planned_worker_remains_unknown_and_ambiguous_repeats_stay_quarantined(self):
        result = summarize_executions([record("planned", state="planned", usage=None)])
        self.assertEqual(result["totals"]["unknown_records"], 1)
        self.assertFalse(result["totals"]["complete"])
        first = record("a", response_fingerprints=["x", "y"])
        other = record("b", response_fingerprints=["y", "z"])
        result = summarize_executions([first, other, first, record("c", response_fingerprints=["z"])])
        self.assertEqual(result["totals"]["known"]["total_tokens"], 0)
        self.assertFalse(result["totals"]["complete"])

    def test_lineage_marks_missing_parent_incomplete(self):
        result = summarize_executions([record("child", parent_work_id="missing")])
        self.assertEqual(result["totals"]["lineage"]["status"], "incomplete")

    def test_summary_accepts_a_full_ledger_iterator_beyond_old_input_limit(self):
        result = summarize_executions(record(f"work-{index}") for index in range(513))
        self.assertEqual(result["coverage"]["records"], 513)
        self.assertEqual(result["totals"]["known"]["total_tokens"], 513 * 15)

    def test_external_trials_are_attested_only(self):
        result = compare_trials([trial("direct"), trial("current"), trial("optimized")])
        self.assertEqual(result["evidence_level"], "caller-attested")
        self.assertTrue(all(not pair["eligible"] and pair["claim"] == "attested-comparison-only" for pair in result["pairs"]))
        self.assertIsNone(result["aggregates"]["optimized_vs_direct"]["total_token_delta"])

    def test_verified_trials_require_disjoint_resolved_complete_receipts_and_aggregate_by_arm(self):
        trials = [trial("direct", receipt_ids=["d1"]), trial("current", receipt_ids=["c1"]), trial("optimized", receipt_ids=["o1", "o2"])]
        receipts = {
            "d1": record("d1", usage={**USAGE, "total_tokens": 30, "input_tokens": 25}),
            "c1": record("c1", usage={**USAGE, "total_tokens": 20, "input_tokens": 15}),
            "o1": record("o1", usage={**USAGE, "total_tokens": 5, "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0}),
            "o2": record("o2", parent_work_id="o1", usage={**USAGE, "total_tokens": 5, "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0}),
        }
        result = compare_verified_trials(trials, receipt_resolver=receipts.__getitem__, execution_records=receipts.values())
        self.assertEqual(result["evidence_level"], "resolver-attested")
        self.assertEqual(result["aggregates"]["optimized_vs_direct"]["total_token_delta"], -20)
        self.assertEqual(result["aggregates"]["optimized_vs_current"]["total_token_delta"], -10)
        self.assertIsNone(result["pairs"][0]["derived_retry_count"]["optimized"])
        with self.assertRaisesRegex(EfficiencyError, "disjoint"):
            compare_verified_trials([trial("direct", receipt_ids=["d1"]), trial("optimized", receipt_ids=["d1"])], receipt_resolver=receipts.__getitem__, execution_records=receipts.values())

    def test_verified_trial_with_nonterminal_receipt_is_ineligible(self):
        receipts = {"c": record("c"), "o": record("o", state="started")}
        result = compare_verified_trials([trial("current", receipt_ids=["c"]), trial("optimized", receipt_ids=["o"])],
                                         receipt_resolver=receipts.__getitem__, execution_records=receipts.values())
        self.assertFalse(result["pairs"][0]["eligible"])
        self.assertIn("incomplete-receipt-coverage", result["pairs"][0]["reasons"])

    def test_verified_comparison_includes_unrequested_failed_sibling_in_lineage_spend(self):
        receipts = {
            "base": record("base"),
            "retry": record("retry", parent_work_id="base", state="failed"),
            "opt": record("opt", usage={**USAGE, "total_tokens": 10, "input_tokens": 5}),
        }
        result = compare_verified_trials([trial("current", receipt_ids=["base"]), trial("optimized", receipt_ids=["opt"])],
                                         receipt_resolver=receipts.__getitem__, execution_records=receipts.values())
        pair = result["pairs"][0]
        self.assertEqual(pair["baseline_total_tokens"], 30)
        self.assertIsNone(pair["derived_retry_count"]["baseline"])

    def test_real_supervisor_anchor_is_attribution_not_a_missing_execution(self):
        anchor = {"work_id": "unit", "role": "coordinator", "state": "planned", "attribution_reason": "run-supervisor"}
        records = [anchor, record("implement", parent_work_id="unit"), record("review", parent_work_id="unit"), record("direct")]
        receipts = {row["work_id"]: row for row in records}
        trials = [trial("direct", receipt_ids=["direct"]), trial("optimized", receipt_ids=["implement", "review"])]
        result = compare_verified_trials(trials, receipt_resolver=receipts.__getitem__, execution_records=records)
        self.assertTrue(result["pairs"][0]["eligible"])
        self.assertEqual(result["pairs"][0]["optimized_total_tokens"], 30)
        self.assertIsNone(result["pairs"][0]["derived_retry_count"]["optimized"])
        anchor["thread_id"] = "actually-launched"
        result = compare_verified_trials(trials, receipt_resolver=receipts.__getitem__, execution_records=records)
        self.assertFalse(result["pairs"][0]["eligible"])

    def test_native_parent_attribution_is_not_an_unknown_or_unknown_model_group(self):
        anchor = {"work_id": "unit", "role": "coordinator", "state": "planned", "attribution_reason": "parent-attribution"}
        result = summarize_executions([anchor, record("child", parent_work_id="unit")])
        self.assertEqual((result["totals"]["known"]["total_tokens"], result["totals"]["unknown_records"]), (15, 0))
        self.assertEqual([(item["role"], item["model"]) for item in result["by_role_model"]], [("implementer", "gpt-test")])

    def test_failed_zero_dispatch_pair_is_visible_and_not_an_overall_savings_claim(self):
        receipts = {"d1": record("d1"), "o1": record("o1"), "d2": record("d2")}
        trials = [trial("direct", receipt_ids=["d1"]), trial("optimized", receipt_ids=["o1"]),
                  trial("direct", replicate="two", receipt_ids=["d2"]),
                  trial("optimized", replicate="two", success=False, validation_outcome="failed", receipt_ids=[])]
        result = compare_verified_trials(trials, receipt_resolver=receipts.__getitem__, execution_records=receipts.values())
        aggregate = result["aggregates"]["optimized_vs_direct"]
        self.assertEqual((aggregate["pair_count"], aggregate["eligible_pairs"], aggregate["ineligible_pairs"]), (2, 1, 1))
        self.assertEqual(aggregate["scope"], "complete-case-only")
        self.assertIsNone(aggregate["overall_savings_claim"])

    def test_conflicting_outcome_with_same_receipt_withholds_both_attributions(self):
        rows = [record("same", response_fingerprints=["response"]), record("same", response_fingerprints=["response"], state="failed")]
        report = summarize_executions(rows)
        self.assertFalse(report["totals"]["complete"])
        self.assertEqual(report["totals"]["known"]["total_tokens"], 0)
        self.assertEqual(report["totals"]["unknown_records"], 2)


if __name__ == "__main__":
    unittest.main()
