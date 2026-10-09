from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.autonomy import LOCAL_REVERSIBLE_WRITE
from tasktra.overview import _public_text, format_overview, orchestration_overview
from tasktra.state import SCHEMA_VERSION, StateError, StateStore


class OverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.store = StateStore(self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def goal(self, identifier: str, *, priority: int = 0, budget: int | None = None) -> None:
        self.store.create_goal(goal_id=identifier, title=identifier, description=identifier,
                               priority=priority, budget_tokens=budget, acceptance=["Done"])

    def test_empty_and_missing_database_are_read_only(self) -> None:
        missing = StateStore(Path(self.directory.name) / "missing.sqlite")
        with self.assertRaisesRegex(StateError, "does not exist"):
            orchestration_overview(missing)
        self.assertFalse(missing.path.exists())
        self.store.migrate()
        before = self.path.read_bytes()
        report = orchestration_overview(self.store)
        self.assertEqual(report["aggregates"]["goals"]["total"], 0)
        self.assertEqual(report["goals"], [])
        self.assertEqual(before, self.path.read_bytes())

    def test_snapshot_observes_committed_wal_state(self) -> None:
        self.store.migrate()
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            connection.execute("BEGIN")
            connection.execute("SELECT count(*) FROM goals").fetchone()
            self.goal("wal-goal")
            self.assertTrue(self.path.with_name(f"{self.path.name}-wal").exists())
            report = orchestration_overview(self.store)
            self.assertEqual([goal["id"] for goal in report["goals"]], ["wal-goal"])
        finally:
            connection.close()

    def test_corrupt_database_becomes_a_structured_state_error(self) -> None:
        corrupt = Path(self.directory.name) / "corrupt.sqlite"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(StateError, "unable to read runtime database"):
            orchestration_overview(StateStore(corrupt))

    def test_schema_newer_after_preflight_requires_a_compatible_build(self) -> None:
        self.store.migrate()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        finally:
            connection.close()
        with patch.object(StateStore, "inspect_schema_version", return_value=SCHEMA_VERSION):
            with self.assertRaisesRegex(StateError, "compatible Tasktra build"):
                orchestration_overview(self.store)

    def test_goal_pagination_is_stable_and_aggregates_are_whole_project(self) -> None:
        self.goal("middle", priority=2)
        self.goal("same-b", priority=3)
        self.goal("same-a", priority=3)
        self.store.create_work_unit(goal_id="middle", work_unit_id="unit-middle", title="Middle")
        first = orchestration_overview(self.store, limit=2)
        self.assertEqual([item["id"] for item in first["goals"]], ["same-a", "same-b"])
        self.assertEqual(first["pagination"], {"scope": "goals", "limit": 2, "offset": 0, "total": 3, "next_offset": 2})
        self.assertEqual(first["aggregates"]["work_units"]["total"], 1)
        second = orchestration_overview(self.store, limit=2, offset=2)
        self.assertEqual([item["id"] for item in second["goals"]], ["middle"])
        self.assertIsNone(second["pagination"]["next_offset"])

    def test_goal_detail_marks_expired_and_live_leases_and_paginates_units(self) -> None:
        self.goal("one")
        for identifier in ("unit-a", "unit-b"):
            self.store.create_work_unit(goal_id="one", work_unit_id=identifier, title=identifier)
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE work_units SET status='leased',lease_expires_at=? WHERE id='unit-a'", ("2000-01-01T00:00:00Z",))
            connection.execute("UPDATE work_units SET status='leased',lease_expires_at=? WHERE id='unit-b'", ("2999-01-01T00:00:00Z",))
            connection.execute("UPDATE budgets SET total_elapsed_ms=60000,max_concurrency=2 WHERE goal_id='one'")
            connection.execute("INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,acquired_at,heartbeat_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)", ("a", "unit-a", 1, "worker", 1, "hash", "1999-01-01T00:00:00Z", "1999-01-01T00:00:00Z", "2000-01-01T00:00:00Z", "leased"))
            connection.execute("INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,acquired_at,heartbeat_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)", ("b", "unit-b", 1, "worker", 1, "hash", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2999-01-01T00:00:00Z", "leased"))
        report = orchestration_overview(self.store, goal_id="one", limit=1, offset=1)
        self.assertEqual(report["pagination"]["scope"], "work_units")
        self.assertEqual(report["goal"]["leases"], {"stored_leased": 2, "live": 1, "expired": 1})
        self.assertTrue(report["goal"]["budget"]["exhausted"])
        self.assertGreater(report["goal"]["budget"]["elapsed_ms"]["reserved_held"], 60000)
        self.assertEqual(report["goal"]["budget"]["concurrency"], {"maximum": 2, "occupied": 2, "available": 0})
        self.assertEqual(report["goal"]["work_units"][0]["lease"]["state"], "live")
        self.assertIn("expired-lease-recovery", {item["code"] for item in report["goal"]["attention"]})
        self.assertNotIn("hash", format_overview(report))

    def test_null_and_zero_budgets_incomplete_dependency_and_invalid_bounds(self) -> None:
        self.goal("null-budget", budget=None)
        self.goal("zero-budget", budget=0)
        null = orchestration_overview(self.store)["goals"]
        view = {item["id"]: item for item in null}
        self.assertIsNone(view["null-budget"]["budget"]["tokens"]["total"])
        self.assertEqual(view["zero-budget"]["budget"]["tokens"]["total"], 0)
        self.assertTrue(view["zero-budget"]["budget"]["exhausted"])
        with self.assertRaisesRegex(StateError, "Unknown goal"):
            orchestration_overview(self.store, goal_id="absent")
        for kwargs in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": True}):
            with self.assertRaises(StateError):
                orchestration_overview(self.store, **kwargs)
        with self.assertRaisesRegex(StateError, "offset"):
            orchestration_overview(self.store, offset=1 << 63)

    def test_incomplete_contract_dependency_is_attention_not_authorization(self) -> None:
        self.goal("dependent")
        contract = {
            "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "dependent",
            "outcome": "Done", "motivation": "Test overview", "author_id": "owner",
            "acceptance_criteria": [{"id": "done", "statement": "Done"}],
            "scope": {"paths": ["."], "exclusions": []},
            "allowed_actions": ["goal-activate"], "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
            "quality_requirements": [], "budgets": {"tokens": None, "attempts": 1, "elapsed_seconds": 60, "concurrency": 1},
            "dependencies": ["missing-goal"], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
        }
        self.store.define_goal_contract("dependent", contract, actor_id="owner")
        view = orchestration_overview(self.store, goal_id="dependent")["goal"]
        self.assertEqual(view["dependencies"], [{"id": "missing-goal", "status": "missing"}])
        self.assertIn("dependency-waiting", {item["code"] for item in view["attention"]})
        self.assertNotIn("authorized", str(view).lower())

    def test_formatter_scrubs_terminal_controls(self) -> None:
        self.goal("clean")
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE goals SET title=? WHERE id='clean'", ("bad\x1b[31m token=secret-value",))
        output = format_overview(orchestration_overview(self.store))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("secret-value", output)
        self.assertNotIn("—", output)
        self.assertTrue(output.endswith("\n"))

    def test_text_redaction_requires_a_credential_value(self) -> None:
        benign = "passwordless credential-manager secretariat cookiecutter authorizationcode"
        self.assertEqual(_public_text(benign), benign)
        self.assertEqual(_public_text("Bearer private-value"), "Bearer [redacted]")
        self.assertEqual(_public_text("api key = private-value"), "api key = [redacted]")

    def test_emergency_stop_and_provider_attention_are_visible(self) -> None:
        self.goal("stopped")
        self.store.set_emergency_stop(actor_id="safety", reason="Bearer private-value\nstop")
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute(
                "INSERT INTO effect_intents(idempotency_key,goal_id,effect_class,operation,request_sha256,request_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("effect-one", "stopped", "local", "test", "a" * 64, "{}", "indeterminate", "2026-01-01T00:00:00Z"),
            )
        report = orchestration_overview(self.store, goal_id="stopped")
        self.assertTrue(report["runtime"]["emergency_stop"]["active"])
        self.assertNotIn("private-value", report["runtime"]["emergency_stop"]["reason"])
        self.assertIn("provider-effect-indeterminate", {item["code"] for item in report["goal"]["attention"]})
        terminal = format_overview(report)
        self.assertIn("runtime emergency stop active", terminal)
        self.assertIn("provider-effect-indeterminate", terminal)


if __name__ == "__main__":
    unittest.main()
