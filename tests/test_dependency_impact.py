from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.dependency_impact import dependency_impact
from tasktra.state import StateError, StateStore


class DependencyImpactTests(unittest.TestCase):
    def _store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(title="Goal", description="Dependency impact", goal_id="goal-one")
        return store

    def _status(self, store: StateStore, work_unit_id: str, status: str) -> None:
        with store._connection() as connection:
            store._prepare_write(connection)
            connection.execute("UPDATE work_units SET status=? WHERE id=?", (status, work_unit_id))

    def _unit(self, store: StateStore, identifier: str, prerequisites: tuple[str, ...] = ()) -> None:
        store.create_work_unit(
            goal_id="goal-one", title=f"sensitive token={identifier}", work_unit_id=identifier,
            scope={"secret": "never report"}, prerequisite_ids=prerequisites,
        )

    def test_fan_in_out_closure_distances_and_data_minimization(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._unit(store, "root-a")
            self._unit(store, "root-b")
            self._unit(store, "left", ("root-a",))
            self._unit(store, "right", ("root-b",))
            self._unit(store, "anchor", ("left", "right"))
            self._unit(store, "fan-a", ("anchor",))
            self._unit(store, "fan-b", ("anchor",))
            self._unit(store, "join", ("fan-a", "fan-b"))
            report = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor")
            self.assertTrue(report["read_only"])
            self.assertFalse(report["claimability_evaluated"])
            self.assertEqual(report["summary"], {
                "direct_prerequisites_total": 2, "all_prerequisites_total": 4,
                "incomplete_blockers_total": 4, "direct_dependents_total": 2,
                "all_dependents_total": 3, "direct_prerequisite_gates_cleared_if_completed": 2,
            })
            self.assertEqual(
                [(row["relation"], row["distance"], row["work_unit_id"]) for row in report["relations"]],
                [("prerequisite", 1, "left"), ("prerequisite", 1, "right"),
                 ("prerequisite", 2, "root-a"), ("prerequisite", 2, "root-b"),
                 ("dependent", 1, "fan-a"), ("dependent", 1, "fan-b"),
                 ("dependent", 2, "join")],
            )
            self.assertEqual(set(report["relations"][0]), {
                "work_unit_id", "relation", "distance", "direct", "status", "checkpoint_id",
                "structural_ready", "incomplete_blocker", "would_clear_direct_prerequisite_gate",
            })
            self.assertNotIn("title", repr(report))
            self.assertNotIn("never report", repr(report))

    def test_multiple_paths_keep_minimum_distance_and_direct_gate_is_structural(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._unit(store, "root")
            self._unit(store, "short", ("root",))
            self._unit(store, "long", ("root",))
            self._unit(store, "anchor", ("root", "short"))
            self._unit(store, "direct", ("anchor",))
            self._unit(store, "blocked-by-root", ("anchor", "root"))
            self._unit(store, "transitive", ("direct",))
            report = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor")
            roots = [row for row in report["relations"] if row["work_unit_id"] == "root"]
            self.assertEqual([(row["relation"], row["distance"]) for row in roots], [("prerequisite", 1)])
            gates = {row["work_unit_id"]: row["would_clear_direct_prerequisite_gate"] for row in report["relations"]}
            self.assertTrue(gates["direct"])
            self.assertFalse(gates["blocked-by-root"])
            self.assertFalse(gates["transitive"])
            self.assertEqual(report["summary"]["direct_prerequisite_gates_cleared_if_completed"], 1)

    def test_gate_counter_uses_status_agnostic_formula_and_complete_anchor_is_noop(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._unit(store, "anchor")
            for identifier in ("failed", "blocked", "leased", "complete"):
                self._unit(store, identifier, ("anchor",))
                self._status(store, identifier, identifier)
            report = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor", direction="dependents")
            self.assertEqual(report["summary"]["direct_prerequisite_gates_cleared_if_completed"], 4)
            self.assertTrue(all(row["would_clear_direct_prerequisite_gate"] for row in report["relations"]))
            self.assertEqual({row["status"] for row in report["relations"]}, {"failed", "blocked", "leased", "complete"})
            self._status(store, "anchor", "complete")
            complete = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor")
            self.assertEqual(complete["summary"]["direct_prerequisite_gates_cleared_if_completed"], 0)
            self.assertFalse(any(row["would_clear_direct_prerequisite_gate"] for row in complete["relations"]))

    def test_pagination_and_summary_are_stable_across_direction(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._unit(store, "p-one")
            self._unit(store, "p-two")
            self._unit(store, "anchor", ("p-one", "p-two"))
            self._unit(store, "d-one", ("anchor",))
            self._unit(store, "d-two", ("anchor",))
            full = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor", limit=100)
            first = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor", limit=2)
            second = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor", limit=2, offset=2)
            dependent = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor", direction="dependents", limit=1)
            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(first["summary"], dependent["summary"])
            self.assertEqual(first["total"], 4)
            self.assertEqual(dependent["total"], 2)
            self.assertEqual(first["next_offset"], 2)
            self.assertIsNone(second["next_offset"])
            self.assertEqual(first["relations"] + second["relations"], full["relations"])

    def test_argument_failures_missing_database_and_read_only_result(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._unit(store, "anchor")
            path = Path(directory) / "state.sqlite"
            before = {item.name: item.read_bytes() for item in Path(directory).iterdir() if item.is_file()}
            report = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor")
            after = {item.name: item.read_bytes() for item in Path(directory).iterdir() if item.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(report["read_only"])
            for kwargs in (
                {"direction": "sideways"}, {"limit": True}, {"limit": 101},
                {"offset": True}, {"offset": 1_000_001}, {"goal_id": "invalid id"},
                {"work_unit_id": "invalid id"},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(StateError):
                        arguments = {"goal_id": "goal-one", "work_unit_id": "anchor", **kwargs}
                        dependency_impact(store, **arguments)
            missing = StateStore(Path(directory) / "missing.sqlite")
            with self.assertRaisesRegex(StateError, "does not exist"):
                dependency_impact(missing, goal_id="goal-one", work_unit_id="anchor")
            with self.assertRaisesRegex(StateError, "Unknown goal"):
                dependency_impact(store, goal_id="unknown", work_unit_id="anchor")
            with self.assertRaisesRegex(StateError, "different or unknown"):
                dependency_impact(store, goal_id="goal-one", work_unit_id="unknown")


if __name__ == "__main__":
    unittest.main()
