from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
import sys
import unittest
from unittest.mock import patch

from tasktra.diagnostics import runtime_provenance
from tasktra.operator_cockpit import MAX_SNAPSHOT_BYTES, capture_operator_cockpit, export_operator_cockpit
from tasktra.state import StateError, StateStore


class OperatorCockpitCaptureTests(unittest.TestCase):
    def _capture(self, store: StateStore, root: Path) -> dict:
        return capture_operator_cockpit(
            store, project_root=root, project_name="Example",
            source_provenance=runtime_provenance(Path.cwd()),
            at="2030-01-02T03:04:05Z",
        )

    def test_snapshot_is_coherent_minimized_and_carries_complete_graph(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "runtime.sqlite")
            store.create_goal(title="token=secret", description="not included", goal_id="goal")
            store.create_work_unit(goal_id="goal", title="first", work_unit_id="first")
            store.create_work_unit(goal_id="goal", title="second", work_unit_id="second", prerequisite_ids=("first",))
            snapshot = self._capture(store, root)
            self.assertEqual(snapshot["capture"]["captured_at"], "2030-01-02T03:04:05Z")
            self.assertTrue(snapshot["read_only"])
            self.assertFalse(snapshot["claimability_evaluated"])
            goal = snapshot["goals"][0]
            self.assertNotIn("secret", goal["title"])
            self.assertTrue(goal["completeness"]["graph_complete"])
            second = next(unit for unit in goal["work_units"] if unit["id"] == "second")
            self.assertEqual(second["prerequisite_ids"], ["first"])
            self.assertNotIn("description", repr(snapshot))
            self.assertEqual(set(snapshot["guidance_templates"]), {
                "goal-overview", "goal-status", "work-dependencies", "work-impact", "doctor",
            })

    def test_incomplete_goal_omits_all_graph_derived_fields(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "runtime.sqlite")
            store.create_goal(title="Goal", description="Description", goal_id="goal")
            store.create_work_unit(goal_id="goal", title="one", work_unit_id="one")
            store.create_work_unit(goal_id="goal", title="two", work_unit_id="two", prerequisite_ids=("one",))
            with patch("tasktra.operator_cockpit.MAX_WORK_UNITS_PER_GOAL", 1):
                snapshot = self._capture(store, root)
            goal = snapshot["goals"][0]
            self.assertFalse(goal["completeness"]["graph_complete"])
            self.assertEqual(snapshot["completeness"]["truncated_goal_ids"], ["goal"])
            self.assertNotIn("structural_ready", goal["work_units"][0])
            self.assertNotIn("prerequisite_ids", goal["work_units"][0])
            self.assertEqual(goal["completeness"]["dependency_edges_captured"], 0)

    def test_aggregate_effect_counts_and_template_objects_are_snapshot_local(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "runtime.sqlite")
            store.create_goal(title="Goal", description="Description", goal_id="goal")
            with store._connection() as connection:
                store._prepare_write(connection)
                connection.execute(
                    "INSERT INTO effect_intents(idempotency_key,goal_id,effect_class,operation,request_sha256,request_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("effect", "goal", "local", "test", "a" * 64, "{}", "failed", "2030-01-01T00:00:00Z"),
                )
            first = self._capture(store, root)
            self.assertEqual(set(first["aggregates"]), {"goals", "work_units", "leases", "budgets", "provider_effects"})
            self.assertEqual(first["aggregates"]["provider_effects"], {"failed": 1})
            first["guidance_templates"]["doctor"]["argv_suffix"].append("mutated")
            second = self._capture(store, root)
            self.assertNotIn("mutated", second["guidance_templates"]["doctor"]["argv_suffix"])

    def test_argument_and_missing_runtime_errors_do_not_create_a_database(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "missing.sqlite")
            with self.assertRaisesRegex(StateError, "does not exist"):
                self._capture(store, root)
            self.assertFalse(store.path.exists())
            store.create_goal(title="Goal", description="Description", goal_id="goal")
            with self.assertRaisesRegex(StateError, "page_size"):
                capture_operator_cockpit(
                    store, project_root=root, project_name="Example", source_provenance={}, page_size=101,
                )

    def test_export_checks_snapshot_size_and_dangling_symlink_before_rendering(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "runtime.sqlite")
            store.create_goal(title="Goal", description="Description", goal_id="goal")
            snapshot = self._capture(store, root)
            rendered: list[object] = []
            view = ModuleType("tasktra.cockpit_view")
            view.render = lambda value: (rendered.append(value) or b"<html></html>")  # type: ignore[attr-defined]
            oversized = {**snapshot, "project": {**snapshot["project"], "name": "x" * MAX_SNAPSHOT_BYTES}}
            destination = root / "uncreated" / "cockpit.html"
            with patch.dict(sys.modules, {"tasktra.cockpit_view": view}):
                with self.assertRaisesRegex(StateError, "20 MiB"):
                    export_operator_cockpit(oversized, destination)
            self.assertFalse(destination.parent.exists())
            self.assertEqual(rendered, [])
            dangling = root / "dangling.html"
            try:
                dangling.symlink_to(root / "absent-target")
            except OSError:
                self.skipTest("this filesystem does not permit symlink test setup")
            with patch.dict(sys.modules, {"tasktra.cockpit_view": view}):
                with self.assertRaisesRegex(StateError, "already exists"):
                    export_operator_cockpit(snapshot, dangling)
            self.assertTrue(dangling.is_symlink())
            self.assertEqual(rendered, [])


if __name__ == "__main__":
    unittest.main()
