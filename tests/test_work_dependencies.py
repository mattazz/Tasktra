from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tests.runtime_schema_helpers import peel_schema13_interventions


class WorkDependencyTests(unittest.TestCase):
    def _store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(title="Goal", description="Dependency checks", goal_id="goal-one")
        return store

    def _complete(self, store: StateStore, work_unit_id: str) -> None:
        # Readiness intentionally depends only on durable status. Lifecycle
        # completion has separate authorization/workflow tests.
        with store._connection() as connection:
            store._prepare_write(connection)
            connection.execute(
                "UPDATE work_units SET status='complete' WHERE id=?", (work_unit_id,)
            )

    def test_fan_in_out_readiness_and_sorted_immutable_edges(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_work_unit(goal_id="goal-one", title="One", work_unit_id="one")
            store.create_work_unit(goal_id="goal-one", title="Two", work_unit_id="two")
            store.create_work_unit(
                goal_id="goal-one", title="Three", work_unit_id="three",
                prerequisite_ids=("two", "one"),
            )
            store.create_work_unit(
                goal_id="goal-one", title="Four", work_unit_id="four",
                prerequisite_ids=("two",),
            )
            initial = store.work_dependencies("goal-one")
            three = next(unit for unit in initial["units"] if unit["work_unit_id"] == "three")
            self.assertEqual([item["id"] for item in three["prerequisites"]], ["one", "two"])
            self.assertFalse(three["ready"])
            self._complete(store, "one")
            self.assertFalse(store.work_dependencies("goal-one", "three")["units"][0]["ready"])
            self._complete(store, "two")
            report = store.work_dependencies("goal-one")
            self.assertTrue(all(unit["ready"] for unit in report["units"] if unit["work_unit_id"] in {"three", "four"}))
            self.assertTrue(report["read_only"])
            self.assertIn("does not authorize", report["notice"])

    def test_invalid_prerequisites_roll_back_without_edges_or_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_goal(title="Elsewhere", description="Other", goal_id="goal-two")
            store.create_work_unit(goal_id="goal-one", title="One", work_unit_id="one")
            store.create_work_unit(goal_id="goal-two", title="Other", work_unit_id="other")
            failures = (
                ("missing", ("unknown",), "Unknown prerequisite"),
                ("self", ("self",), "cannot depend on itself"),
                ("duplicate", ("one", "one"), "duplicates"),
                ("cross", ("other",), "different goal"),
            )
            for identifier, prerequisites, error in failures:
                with self.subTest(identifier=identifier):
                    before = store.status()["events"]
                    with self.assertRaisesRegex(StateError, error):
                        store.create_work_unit(
                            goal_id="goal-one", title="Invalid", work_unit_id=identifier,
                            prerequisite_ids=prerequisites,
                        )
                    self.assertIsNone(store.get_work_unit(identifier))
                    self.assertEqual(store.status()["events"], before)
            with self.assertRaisesRegex(StateError, "at most 64"):
                store.create_work_unit(
                    goal_id="goal-one", title="Too many", work_unit_id="too-many",
                    prerequisite_ids=tuple(f"item-{index}" for index in range(65)),
                )

    def test_later_checkpoint_prerequisite_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(
                title="Checkpointed", description="Order checks", goal_id="goal-one",
                acceptance=["Done."],
            )
            store.define_goal_contract("goal-one", {
                "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
                "outcome": "Check ordering", "motivation": "Test.", "author_id": "owner",
                "acceptance_criteria": [{"id": "done", "statement": "Done."}],
                "scope": {"paths": ["."], "exclusions": []},
                "allowed_actions": ["goal-activate"], "allowed_effects": ["read-only"], "prohibited_actions": [],
                "quality_requirements": [],
                "budgets": {"tokens": 10, "attempts": 1, "elapsed_seconds": 60, "concurrency": 1},
                "dependencies": [], "checkpoints": ["first", "second"],
                "stop_conditions": [], "escalation_conditions": [],
            }, actor_id="owner")
            scope = {"paths": ["."], "exclusions": []}
            store.create_work_unit(
                goal_id="goal-one", title="Later", work_unit_id="later", checkpoint_id="second", scope=scope,
            )
            with self.assertRaisesRegex(StateError, "later checkpoint"):
                store.create_work_unit(
                    goal_id="goal-one", title="Earlier", work_unit_id="earlier", checkpoint_id="first",
                    scope=scope, prerequisite_ids=("later",),
                )
            same = store.create_work_unit(
                goal_id="goal-one", title="Same", work_unit_id="same", checkpoint_id="second",
                scope=scope, prerequisite_ids=("later",),
            )
            self.assertEqual(same["id"], "same")

    def test_read_only_report_does_not_create_wal_or_change_database(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            path = Path(directory) / "state.sqlite"
            store.create_work_unit(goal_id="goal-one", title="Legacy", work_unit_id="legacy")
            before = {item.name: item.read_bytes() for item in Path(directory).iterdir() if item.is_file()}
            report = store.work_dependencies("goal-one", limit=1, offset=0)
            after = {item.name: item.read_bytes() for item in Path(directory).iterdir() if item.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(report["total"], 1)
            self.assertTrue(report["units"][0]["ready"])
            self.assertIsNone(report["next_offset"])

    def test_graph_cycle_is_rejected_by_the_shared_helper(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            path = Path(directory) / "state.sqlite"
            store.create_work_unit(goal_id="goal-one", title="One", work_unit_id="one")
            store.create_work_unit(
                goal_id="goal-one", title="Two", work_unit_id="two", prerequisite_ids=("one",),
            )
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("INSERT INTO work_unit_dependencies VALUES('one','two')")
                connection.commit()
            finally:
                connection.close()
            with store._connection(write=False) as connection:
                with self.assertRaisesRegex(StateError, "cycle"):
                    StateStore._work_prerequisite_state_in_transaction(connection, "two")
            with self.assertRaisesRegex(StateError, "unsealed|tampered"):
                store.work_dependencies("goal-one")

    def test_deep_acyclic_graph_uses_iterative_validation(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            path = Path(directory) / "state.sqlite"
            store.create_work_unit(goal_id="goal-one", title="First", work_unit_id="unit-0000")
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                first = connection.execute(
                    "SELECT goal_id,status,scope,checkpoint_id,created_at,updated_at FROM work_units WHERE id='unit-0000'"
                ).fetchone()
                for index in range(1, 1100):
                    identifier = f"unit-{index:04d}"
                    prerequisite = f"unit-{index - 1:04d}"
                    connection.execute(
                        "INSERT INTO work_units(id,goal_id,title,status,scope,checkpoint_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (identifier, first[0], identifier, first[1], first[2], first[3], first[4], first[5]),
                    )
                    connection.execute(
                        "INSERT INTO work_unit_dependencies VALUES(?,?)", (identifier, prerequisite)
                    )
                connection.commit()
            finally:
                connection.close()
            with store._connection(write=False) as connection:
                result = StateStore._work_prerequisite_state_in_transaction(connection, "unit-1099")
                graph = StateStore._work_dependency_graph_in_transaction(connection, "goal-one")
            self.assertFalse(result["ready"])
            self.assertEqual(len(graph), 1100)

    def test_attestation_rejects_an_unsealed_cyclic_dependency_graph(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            path = Path(directory) / "state.sqlite"
            store.create_work_unit(goal_id="goal-one", title="One", work_unit_id="one")
            store.create_work_unit(
                goal_id="goal-one", title="Two", work_unit_id="two", prerequisite_ids=("one",),
            )
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("INSERT INTO work_unit_dependencies VALUES('one','two')")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StateError, "cycle"):
                store.attest_ledger(actor_id="human")

    def test_schema_ten_migrates_empty_edges_with_backup_and_rejects_bad_shape(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = self._store(directory)
            legacy = store.create_work_unit(goal_id="goal-one", title="Legacy", work_unit_id="legacy")
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")
                peel_schema13_interventions(connection, target_version=10)
                connection.execute("PRAGMA user_version = 10")
                StateStore._seal_current_state_in_transaction(connection, "2030-01-01T00:00:00Z", existing_only=True)
                connection.commit()
            finally:
                connection.close()
            evidence = store.migrate_with_evidence()
            self.assertEqual(evidence["after_schema"], SCHEMA_VERSION)
            backup = Path(str(evidence["backup_path"]))
            self.assertTrue(backup.is_file())
            self.assertEqual(evidence["backup_sha256"], sha256(backup.read_bytes()).hexdigest())
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(backup_connection.execute("PRAGMA user_version").fetchone()[0], 10)
            finally:
                backup_connection.close()
            report = store.work_dependencies("goal-one")
            self.assertEqual(report["units"], [{
                "work_unit_id": legacy["id"], "status": "planned", "checkpoint_id": None,
                "prerequisites": [], "ready": True,
            }])
            self.assertTrue(store.verify_audit()["ok"])
            connection = sqlite3.connect(path)
            try:
                peel_schema13_interventions(connection, target_version=10)
                connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")
                connection.execute("CREATE TABLE work_unit_dependencies (work_unit_id TEXT PRIMARY KEY)")
                connection.execute("PRAGMA user_version = 10")
                connection.commit()
            finally:
                connection.close()
            connection = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(StateError, "schema10 does not match a complete known lineage structure"):
                    StateStore._migrate(connection)
            finally:
                connection.close()

    def test_schema_ten_rejects_a_nonempty_preexisting_dependency_table(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = self._store(directory)
            store.create_work_unit(goal_id="goal-one", title="One", work_unit_id="one")
            connection = sqlite3.connect(path)
            try:
                peel_schema13_interventions(connection, target_version=10)
                connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")
                connection.execute(
                    "CREATE TABLE work_unit_dependencies ("
                    "work_unit_id TEXT NOT NULL REFERENCES work_units(id), "
                    "prerequisite_id TEXT NOT NULL REFERENCES work_units(id), "
                    "PRIMARY KEY(work_unit_id,prerequisite_id))"
                )
                connection.execute("INSERT INTO work_unit_dependencies VALUES('one','one')")
                connection.execute("PRAGMA user_version = 10")
                with self.assertRaisesRegex(StateError, "schema10 does not match a complete known lineage structure"):
                    StateStore._migrate(connection)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
