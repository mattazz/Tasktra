from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tasktra.contracts import validate_named


class StateTests(unittest.TestCase):
    def test_planned_goal_budget_and_audit(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            goal = store.create_goal(
                title="Ship", description="Make it work", goal_id="g1", budget_tokens=10,
                acceptance=["tests pass"],
            )
            self.assertEqual(goal["status"], "planned")
            work = store.create_work_unit(goal_id="g1", title="Implement")
            approval = store.record_approval(
                goal_id="g1", work_unit_id=work["id"], decision="approved",
                authority_clause="goal scope", rationale="reversible",
                approver_id="goal-steward", performer_id="implementer",
            )
            self.assertEqual(approval["decision"], "approved")
            self.assertEqual(approval["approver_id"], "goal-steward")
            self.assertEqual(approval["performer_id"], "implementer")
            validate_named(goal, "goal")
            validate_named(work, "work-unit")
            validate_named(approval, "approval")
            budget = store.consume_budget("g1", 4)
            self.assertEqual(budget["remaining_tokens"], 6)
            self.assertEqual(store.status()["events"], 4)

    def test_stage_one_rejects_lifecycle_activation(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Make it work", goal_id="g1")
            with self.assertRaisesRegex(StateError, "planned-only"):
                store.set_goal_status("g1", "active")
            self.assertEqual(store.get_goal("g1")["status"], "planned")
            self.assertEqual(store.status()["events"], 1)

    def test_approval_cannot_cross_goals_or_self_approve(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="One", description="First", goal_id="g1")
            store.create_goal(title="Two", description="Second", goal_id="g2")
            work = store.create_work_unit(goal_id="g1", title="Implement")
            with self.assertRaisesRegex(StateError, "different goal"):
                store.record_approval(
                    goal_id="g2", work_unit_id=work["id"], decision="approved",
                    authority_clause="scope", rationale="reason",
                    approver_id="steward", performer_id="implementer",
                )
            with self.assertRaisesRegex(StateError, "cannot approve"):
                store.record_approval(
                    goal_id="g1", work_unit_id=work["id"], decision="approved",
                    authority_clause="scope", rationale="reason",
                    approver_id="implementer", performer_id="implementer",
                )
            # Failed approvals do not leave standalone audit events behind.
            self.assertEqual(store.status()["events"], 3)

    def test_failed_mutation_rolls_back_its_audit_event(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Make it work", goal_id="g1")
            with self.assertRaises(StateError):
                store.create_goal(title="Duplicate", description="Should fail", goal_id="g1")
            with self.assertRaises(StateError):
                store.create_work_unit(goal_id="missing", title="No goal")
            self.assertEqual(store.status()["goals"], 1)
            self.assertEqual(store.status()["work_units"], 0)
            self.assertEqual(store.status()["events"], 1)

    def test_budget_cannot_exceed_total(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Make it work", goal_id="g1", budget_tokens=1)
            with self.assertRaises(StateError):
                store.consume_budget("g1", 2)

    def test_migration_is_safe_for_parallel_openers_and_legacy_approvals(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            # A minimal v1 database exercises the in-place v2 approval migration.
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE approvals (
                        id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, work_unit_id TEXT,
                        decision TEXT NOT NULL, authority_clause TEXT NOT NULL,
                        rationale TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL
                    )"""
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()
            with ThreadPoolExecutor(max_workers=4) as executor:
                versions = list(executor.map(lambda _: StateStore(path).migrate(), range(4)))
            self.assertEqual(versions, [SCHEMA_VERSION] * 4)
            backup = path.with_name(f"{path.name}.v1.bak")
            self.assertTrue(backup.is_file())
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(backup_connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                backup_connection.close()
            connection = sqlite3.connect(path)
            try:
                columns = {row[1]: row for row in connection.execute("PRAGMA table_info(approvals)")}
                self.assertIn("approver_id", columns)
                self.assertIn("performer_id", columns)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            finally:
                connection.close()

    def test_unknown_goal_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            with self.assertRaises(StateError):
                store.set_goal_status("missing", "planned")

    def test_goal_and_work_unit_ids_share_the_bounded_lowercase_slug_contract(self):
        invalid_ids = ("Uppercase", "a" * 65)
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            for identifier in invalid_ids:
                with self.subTest(goal_id=identifier):
                    with self.assertRaisesRegex(StateError, "lowercase slug"):
                        store.create_goal(title="Ship", description="Make it work", goal_id=identifier)
            store.create_goal(title="Ship", description="Make it work", goal_id="goal-one")
            for identifier in invalid_ids:
                with self.subTest(work_unit_id=identifier):
                    with self.assertRaisesRegex(StateError, "lowercase slug"):
                        store.create_work_unit(goal_id="goal-one", title="Implement", work_unit_id=identifier)

    def test_persisted_invalid_goal_identifier_is_diagnosed_without_rewriting(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(title="Ship", description="Make it work", goal_id="goal-one")
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("UPDATE goals SET id = 'LegacyGoal' WHERE id = 'goal-one'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StateError, "Persisted goal_id is invalid; manual migration"):
                store.list_goals()
