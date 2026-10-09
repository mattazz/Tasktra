from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.dependency_impact import dependency_impact
from tasktra.goal_readiness import goal_readiness
from tasktra.state import StateError, StateStore


class GoalReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.sqlite")
        self.store.create_goal(goal_id="goal", title="goal", description="goal", acceptance=["done"])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def unit(self, identifier: str, *, prerequisites: tuple[str, ...] = ()) -> None:
        self.store.create_work_unit(goal_id="goal", work_unit_id=identifier, title=identifier,
                                    prerequisite_ids=prerequisites)

    def status(self, identifier: str, value: str) -> None:
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE work_units SET status=? WHERE id=?", (value, identifier))

    def test_residual_waves_depth_frontiers_and_direct_gate_parity(self) -> None:
        # a -> b -> d, c -> d, and a -> complete-child.  The latter proves the
        # anchored direct-gate count includes complete dependents.
        for identifier, prerequisites in (("a", ()), ("b", ("a",)), ("c", ()),
                                           ("d", ("b", "c")), ("complete-child", ("a",))):
            self.unit(identifier, prerequisites=prerequisites)
        self.status("complete-child", "complete")
        report = goal_readiness(self.store, goal_id="goal")
        self.assertEqual(report["remaining_structure"]["wave_count"], 3)
        self.assertEqual(report["remaining_structure"]["maximum_structural_depth"], 3)
        self.assertEqual([row["remaining_wave"] for row in report["remaining_structure"]["waves"]["items"]], [0, 1, 2])
        rows = {row["work_unit_id"]: row for row in report["frontiers"]["blocking_frontier"]["items"]}
        self.assertEqual(rows["a"]["direct_prerequisite_gates_cleared_if_completed"], 2)
        self.assertEqual(rows["a"]["remaining_direct_prerequisite_gates_cleared_if_completed"], 1)
        self.assertEqual(rows["a"]["incomplete_direct_dependents_count"], 1)
        wave_zero = report["remaining_structure"]["waves"]["items"][0]
        self.assertEqual(wave_zero["direct_gates_clearable_total"], 2)
        self.assertEqual(wave_zero["remaining_direct_gates_clearable_total"], 1)
        self.assertEqual(
            rows["a"]["direct_prerequisite_gates_cleared_if_completed"],
            dependency_impact(self.store, goal_id="goal", work_unit_id="a")["summary"]["direct_prerequisite_gates_cleared_if_completed"],
        )
        self.assertTrue(rows["a"]["deepest_remaining_branch"])
        self.assertEqual([row["work_unit_id"] for row in report["frontiers"]["ready_frontier"]["items"]], ["a", "c"])
        self.assertNotIn("complete-child", rows)

    def test_statuses_remain_residual_and_category_precedence_is_closed(self) -> None:
        for status in ("leased", "blocked", "approval-required", "failed", "exhausted", "paused", "stopped", "retry-wait"):
            self.unit(status)
            self.status(status, status)
        self.unit("done")
        self.status("done", "complete")
        report = goal_readiness(self.store, goal_id="goal")
        summary = report["summary"]
        self.assertEqual(summary["incomplete_total"], 8)
        self.assertEqual(sum(summary["by_category"].values()), 9)
        self.assertEqual(summary["by_category"]["leased"], 1)
        self.assertEqual(summary["by_category"]["terminal_attention"], 5)
        self.assertEqual(summary["by_category"]["structurally_ready"], 2)
        blocking = {row["work_unit_id"]: row for row in report["frontiers"]["blocking_frontier"]["items"]}
        self.assertEqual(set(blocking), {"approval-required", "blocked", "exhausted", "failed", "stopped"})
        self.assertEqual(blocking["failed"]["drilldowns"]["intervention_argv"][:3], ["tasktra", "intervention", "list"])

    def test_paging_order_bounds_and_operational_defaults(self) -> None:
        for identifier in ("z", "a", "m"):
            self.unit(identifier)
        first = goal_readiness(self.store, goal_id="goal", limit=2)
        second = goal_readiness(self.store, goal_id="goal", limit=2, offset=2)
        self.assertEqual([item["work_unit_id"] for item in first["frontiers"]["ready_frontier"]["items"]], ["a", "m"])
        self.assertEqual([item["work_unit_id"] for item in second["frontiers"]["ready_frontier"]["items"]], ["z"])
        self.assertEqual(first["frontiers"]["ready_frontier"]["next_offset"], 2)
        self.assertFalse(first["claimability_evaluated"])
        budget = first["operational_gates"]["budget"]
        self.assertFalse(budget["execution_budgets_configured"])
        self.assertEqual(budget["selection_reason_codes"], ["goal.budgets_missing"])
        self.assertEqual(first["operational_gates"]["authority_contract"]["selection_reason_codes"], ["goal.contract_missing"])
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 101}, {"offset": True}, {"offset": -1}, {"offset": 1_000_001}):
            with self.assertRaises(StateError):
                goal_readiness(self.store, goal_id="goal", **kwargs)

    def test_budget_observation_does_not_infer_token_selection_exhaustion(self) -> None:
        self.unit("unit")
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute(
                "UPDATE budgets SET total_tokens=10,consumed_tokens=5,reserved_tokens=5,total_attempts=2,"
                "consumed_attempts=2,total_elapsed_ms=10,consumed_elapsed_ms=10,max_concurrency=1 WHERE goal_id='goal'"
            )
        budget = goal_readiness(self.store, goal_id="goal")["operational_gates"]["budget"]
        self.assertEqual(budget["fully_allocated_dimensions"], ["attempts", "elapsed_ms", "tokens"])
        self.assertEqual(budget["selection_reason_codes"], ["budget.attempts_exhausted", "budget.elapsed_exhausted"])
        self.assertNotIn("budget.tokens_exhausted", budget["selection_reason_codes"])

    def test_empty_goal_and_unknown_goal(self) -> None:
        empty = goal_readiness(self.store, goal_id="goal")
        self.assertEqual(empty["summary"]["units_total"], 0)
        self.assertEqual(empty["remaining_structure"]["waves"]["items"], [])
        self.assertEqual(empty["remaining_structure"]["maximum_structural_depth"], 0)
        with self.assertRaisesRegex(StateError, "Unknown goal"):
            goal_readiness(self.store, goal_id="missing")


if __name__ == "__main__":
    unittest.main()
