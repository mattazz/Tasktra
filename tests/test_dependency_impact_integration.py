"""Integration boundaries for the read-only dependency-impact projection."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from tasktra.autonomy import LOCAL_REVERSIBLE_WRITE, AutonomyStore
from tasktra.dependency_impact import dependency_impact
from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tests.runtime_schema_helpers import peel_schema13_interventions
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints


class DependencyImpactIntegrationTests(unittest.TestCase):
    def _store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(goal_id="goal-one", title="Goal", description="Impact integration")
        return store

    @staticmethod
    def _files(directory: Path) -> dict[str, bytes]:
        return {
            item.name: item.read_bytes()
            for item in directory.iterdir()
            if item.is_file()
        }

    @staticmethod
    def _authority_counts(store: StateStore) -> dict[str, int]:
        connection = sqlite3.connect(store.path)
        try:
            return {
                "approvals": connection.execute("SELECT count(*) FROM transition_approvals").fetchone()[0],
                "attempts": connection.execute("SELECT count(*) FROM work_attempts").fetchone()[0],
                "effects": connection.execute("SELECT count(*) FROM effect_intents").fetchone()[0],
                "receipts": connection.execute("SELECT count(*) FROM effect_receipts").fetchone()[0],
            }
        finally:
            connection.close()

    def test_deep_chain_and_wide_fanout_are_iterative_and_page_bounded(self) -> None:
        """Insert a large valid graph once, then attest it before the public read."""
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_work_unit(goal_id="goal-one", title="first", work_unit_id="chain-0000")
            store.create_work_unit(goal_id="goal-one", title="fan root", work_unit_id="fan-root")
            connection = sqlite3.connect(store.path)
            try:
                root = connection.execute(
                    "SELECT goal_id,status,scope,checkpoint_id,created_at,updated_at "
                    "FROM work_units WHERE id='chain-0000'"
                ).fetchone()
                assert root is not None
                units = [
                    (f"chain-{index:04d}", root[0], f"chain-{index:04d}", root[1], root[2], root[3], root[4], root[5])
                    for index in range(1, 1101)
                ]
                units.extend(
                    (f"fan-{index:04d}", root[0], f"fan-{index:04d}", root[1], root[2], root[3], root[4], root[5])
                    for index in range(129)
                )
                connection.executemany(
                    "INSERT INTO work_units(id,goal_id,title,status,scope,checkpoint_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    units,
                )
                edges = [(f"chain-{index:04d}", f"chain-{index - 1:04d}") for index in range(1, 1101)]
                edges.extend((f"fan-{index:04d}", "fan-root") for index in range(129))
                connection.executemany(
                    "INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)", edges,
                )
                connection.commit()
            finally:
                connection.close()
            store.attest_ledger(actor_id="fixture-steward")

            deep = dependency_impact(
                store, goal_id="goal-one", work_unit_id="chain-1100",
                direction="prerequisites", limit=3, offset=1097,
            )
            self.assertEqual(deep["summary"]["all_prerequisites_total"], 1100)
            self.assertEqual(deep["total"], 1100)
            self.assertEqual([row["work_unit_id"] for row in deep["relations"]], [
                "chain-0002", "chain-0001", "chain-0000",
            ])
            self.assertIsNone(deep["next_offset"])

            fan = dependency_impact(
                store, goal_id="goal-one", work_unit_id="fan-root",
                direction="dependents", limit=2,
            )
            self.assertEqual(fan["summary"]["direct_dependents_total"], 129)
            self.assertEqual(fan["summary"]["all_dependents_total"], 129)
            self.assertEqual(fan["total"], 129)
            self.assertEqual(len(fan["relations"]), 2)
            self.assertEqual(fan["next_offset"], 2)
            self.assertTrue(all(row["distance"] == 1 for row in fan["relations"]))

    def test_integrity_failures_are_public_fail_closed_reads_without_writes(self) -> None:
        def seeded() -> tuple[TemporaryDirectory[str], StateStore]:
            directory = TemporaryDirectory()
            store = self._store(directory.name)
            store.create_goal(goal_id="goal-two", title="Elsewhere", description="Elsewhere")
            store.create_work_unit(goal_id="goal-one", title="one", work_unit_id="one")
            store.create_work_unit(goal_id="goal-one", title="two", work_unit_id="two", prerequisite_ids=("one",))
            store.create_work_unit(goal_id="goal-two", title="other", work_unit_id="other")
            return directory, store

        cases = (
            ("missing anchor", lambda store: ("goal-one", "missing"), "different or unknown goal"),
            ("cross goal anchor", lambda store: ("goal-one", "other"), "different or unknown goal"),
        )
        for label, arguments, message in cases:
            with self.subTest(label=label):
                directory, store = seeded()
                try:
                    before = self._files(Path(directory.name))
                    goal_id, unit_id = arguments(store)
                    with self.assertRaisesRegex(StateError, message):
                        dependency_impact(store, goal_id=goal_id, work_unit_id=unit_id)
                    self.assertEqual(self._files(Path(directory.name)), before)
                finally:
                    directory.cleanup()

        # These mutations deliberately bypass sealing; the report must reject
        # them at the authority boundary before it can derive a partial graph.
        for label, statement, parameters in (
            ("cycle", "INSERT INTO work_unit_dependencies VALUES(?,?)", ("one", "two")),
            ("missing prerequisite", "INSERT INTO work_unit_dependencies VALUES(?,?)", ("two", "absent")),
        ):
            with self.subTest(label=label):
                directory, store = seeded()
                try:
                    connection = sqlite3.connect(store.path)
                    try:
                        if label == "missing prerequisite":
                            connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute(statement, parameters)
                        connection.commit()
                    finally:
                        connection.close()
                    before = self._files(Path(directory.name))
                    with self.assertRaisesRegex(StateError, "unsealed or tampered"):
                        dependency_impact(store, goal_id="goal-one", work_unit_id="two")
                    self.assertEqual(self._files(Path(directory.name)), before)
                finally:
                    directory.cleanup()

        directory, store = seeded()
        try:
            connection = sqlite3.connect(store.path)
            try:
                peel_schema13_interventions(connection, target_version=SCHEMA_VERSION - 1)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
                connection.commit()
            finally:
                connection.close()
            before = self._files(Path(directory.name))
            with self.assertRaisesRegex(StateError, "requires migration"):
                dependency_impact(store, goal_id="goal-one", work_unit_id="two")
            self.assertEqual(self._files(Path(directory.name)), before)
        finally:
            directory.cleanup()

    def test_gate_counter_is_structural_across_terminal_and_lease_statuses(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_work_unit(goal_id="goal-one", title="anchor", work_unit_id="anchor")
            store.create_work_unit(goal_id="goal-one", title="other", work_unit_id="other")
            for identifier in ("failed", "blocked", "leased", "complete"):
                store.create_work_unit(
                    goal_id="goal-one", title=identifier, work_unit_id=identifier,
                    prerequisite_ids=("anchor",),
                )
            store.create_work_unit(
                goal_id="goal-one", title="still blocked", work_unit_id="still-blocked",
                prerequisite_ids=("anchor", "other"),
            )
            with store._connection() as connection:
                store._prepare_write(connection)
                for identifier in ("failed", "blocked", "leased"):
                    connection.execute("UPDATE work_units SET status=? WHERE id=?", (identifier, identifier))
                connection.execute("UPDATE work_units SET status='complete' WHERE id='complete'")

            report = dependency_impact(store, goal_id="goal-one", work_unit_id="anchor")
            by_id = {row["work_unit_id"]: row for row in report["relations"]}
            self.assertEqual(report["summary"]["direct_prerequisite_gates_cleared_if_completed"], 4)
            for identifier, status in (("failed", "failed"), ("blocked", "blocked"), ("leased", "leased"), ("complete", "complete")):
                self.assertEqual(by_id[identifier]["status"], status)
                self.assertTrue(by_id[identifier]["would_clear_direct_prerequisite_gate"])
            self.assertFalse(by_id["still-blocked"]["would_clear_direct_prerequisite_gate"])
            self.assertFalse(report["claimability_evaluated"])
            self.assertNotIn("claimable", report)
            self.assertNotIn("runnable", report)

    def test_projection_is_authority_neutral_and_ignores_non_graph_state(self) -> None:
        fixture = stage3.AutonomyTests()
        fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(fixture.tearDown)
        store = fixture.store
        store.create_work_unit(
            goal_id="goal-1", title="child", work_unit_id="impact-child",
            scope={"paths": ["src/tasktra"], "exclusions": []}, prerequisite_ids=("unit-1",),
        )
        original_dependencies = store.work_dependencies("goal-1")
        original_explain = store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest, at=stage3.NOW,
        )
        before = self._authority_counts(store)
        bytes_before = self._files(Path(fixture.directory.name))
        original_impact = dependency_impact(store, goal_id="goal-1", work_unit_id="unit-1")
        self.assertEqual(self._authority_counts(store), before)
        self.assertEqual(self._files(Path(fixture.directory.name)), bytes_before)

        expiry = stage3.NOW + timedelta(days=1)
        store.record_transition_approval(
            goal_id="goal-1", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=fixture.digest, approver_id="different-steward", approver_kind="steward",
            performer_id="someone-else", valid_until=expiry, at=stage3.NOW,
        )
        store.consume_budget("goal-1", 0)
        with store._connection() as connection:
            store._prepare_write(connection)
            connection.execute(
                "UPDATE work_units SET lease_holder=?, lease_expires_at=? WHERE id='unit-1'",
                ("unrelated-lease-field", "2031-01-01T00:00:00Z"),
            )
        before_second_read = self._authority_counts(store)
        self.assertEqual(
            dependency_impact(store, goal_id="goal-1", work_unit_id="unit-1"), original_impact,
        )
        self.assertEqual(self._authority_counts(store), before_second_read)
        self.assertEqual(store.work_dependencies("goal-1"), original_dependencies)
        self.assertEqual(
            store.explain_next_work(
                goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest, at=stage3.NOW,
            ),
            original_explain,
        )

    def test_real_completion_and_report_read_observe_one_snapshot(self) -> None:
        fixture = stage3.AutonomyTests()
        fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(fixture.tearDown)
        store: AutonomyStore = fixture.store
        store.create_work_unit(
            goal_id="goal-1", title="dependent", work_unit_id="dependent",
            scope={"paths": ["src/tasktra"], "exclusions": []}, prerequisite_ids=("unit-1",),
        )
        store.record_transition_approval(
            goal_id="goal-1", action="work-complete", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=fixture.digest, approver_id="completion-steward", approver_kind="steward",
            performer_id="worker", valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
        )
        claim = store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest,
            lease_seconds=10, repository="repo", revision="revision", branch="main", workspace="workspace",
            at=stage3.NOW,
        )
        assert claim is not None
        connection = sqlite3.connect(store.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        finally:
            connection.close()

        # This wrapper is a scheduling hook only: it calls the production graph
        # reader, holds its read transaction, and never substitutes graph data.
        graph_read = threading.Event()
        release_report = threading.Event()
        report_result: list[dict[str, object]] = []
        report_errors: list[BaseException] = []
        original = StateStore._work_dependency_graph_in_transaction

        def controlled_graph_reader(connection, goal_id, **kwargs):
            graph = original(connection, goal_id, **kwargs)
            if goal_id == "goal-1":
                graph_read.set()
                if not release_report.wait(timeout=10):
                    raise TimeoutError("report synchronization timed out")
            return graph

        def read_report() -> None:
            try:
                report_result.append(dependency_impact(store, goal_id="goal-1", work_unit_id="unit-1"))
            except BaseException as error:  # surfaced on the test thread
                report_errors.append(error)

        with patch.object(StateStore, "_work_dependency_graph_in_transaction", staticmethod(controlled_graph_reader)):
            reader = threading.Thread(target=read_report)
            reader.start()
            try:
                self.assertTrue(graph_read.wait(timeout=10), "report did not reach its real graph read")

                completion_errors: list[BaseException] = []
                def finish() -> None:
                    try:
                        store.finish_attempt(
                            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                            outcome="success", workflow=checkpoints.complete_workflow("goal-1", "unit-1"),
                            at=stage3.NOW + timedelta(seconds=1),
                        )
                    except BaseException as error:
                        completion_errors.append(error)
                writer = threading.Thread(target=finish)
                writer.start()
                writer.join(timeout=10)
                self.assertFalse(writer.is_alive(), "concurrent completion did not finish")
                self.assertEqual(completion_errors, [])
            finally:
                release_report.set()
                reader.join(timeout=10)
            self.assertFalse(reader.is_alive(), "concurrent report did not finish")

        self.assertEqual(report_errors, [])
        self.assertEqual(report_result[0]["anchor"]["status"], "leased")
        self.assertEqual(report_result[0]["summary"]["direct_prerequisite_gates_cleared_if_completed"], 1)
        after = dependency_impact(store, goal_id="goal-1", work_unit_id="unit-1")
        self.assertEqual(after["anchor"]["status"], "complete")
        self.assertEqual(after["summary"]["direct_prerequisite_gates_cleared_if_completed"], 0)


if __name__ == "__main__":
    unittest.main()
