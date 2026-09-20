"""Adversarial checks for Stage 3 authoritative current-state sealing."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.state import SCHEMA_VERSION, StateError, StateStore


class CurrentStateIntegrityTests(unittest.TestCase):
    @staticmethod
    def _checkpoint_contract() -> dict[str, object]:
        return {
            "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
            "outcome": "Migrate safely", "motivation": "Regression coverage", "author_id": "steward",
            "acceptance_criteria": [{"id": "done", "statement": "Done"}],
            "scope": {"paths": ["."], "exclusions": []}, "allowed_actions": ["work-claim"],
            "allowed_effects": ["local-reversible-write"], "prohibited_actions": [],
            "quality_requirements": [], "budgets": {"tokens": 1, "attempts": 1, "elapsed_seconds": 1, "concurrency": 1},
            "dependencies": [], "checkpoints": ["first", "second"], "stop_conditions": [], "escalation_conditions": [],
        }

    def _store(self) -> tuple[TemporaryDirectory[str], StateStore]:
        directory = TemporaryDirectory()
        store = StateStore(Path(directory.name) / "state.sqlite")
        store.create_goal(title="Goal", description="Description", goal_id="goal-one")
        store.create_work_unit(goal_id="goal-one", title="Work", work_unit_id="work-one")
        return directory, store

    def _assert_direct_sql_is_detected(self, statement: str, params: tuple[object, ...], table: str) -> None:
        directory, store = self._store()
        try:
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(statement, params)
                connection.commit()
            finally:
                connection.close()
            report = store.verify_audit(limit=32)
            self.assertFalse(report["ok"])
            self.assertTrue(any(table in issue for issue in report["issues"]), report)
            with self.assertRaisesRegex(StateError, "unsealed or tampered"):
                store.consume_budget("goal-one", 0)
        finally:
            directory.cleanup()

    def test_direct_sql_lifecycle_and_budget_mutations_are_not_ratifed(self) -> None:
        for statement, params, table in (
            ("UPDATE goals SET status='complete' WHERE id=?", ("goal-one",), "goals"),
            ("UPDATE budgets SET consumed_tokens=7 WHERE goal_id=?", ("goal-one",), "budgets"),
            ("UPDATE work_units SET status='complete' WHERE id=?", ("work-one",), "work_units"),
            ("UPDATE runtime_control SET emergency_stopped=1 WHERE id=1", (), "runtime_control"),
        ):
            with self.subTest(table=table):
                self._assert_direct_sql_is_detected(statement, params, table)

    def test_direct_sql_attempt_effect_and_evidence_inserts_are_detected(self) -> None:
        cases = (
            (
                "INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,acquired_at,heartbeat_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("attempt-one", "work-one", 1, "worker", 1, "hash", "2030-01-01T00:00:00Z", "2030-01-01T00:00:00Z", "2030-01-01T01:00:00Z", "leased"),
                "work_attempts",
            ),
            (
                "INSERT INTO workflow_evidence(work_unit_id,workflow_json,workflow_sha256,recorded_at) VALUES(?,?,?,?)",
                ("work-one", "{}", "0" * 64, "2030-01-01T00:00:00Z"),
                "workflow_evidence",
            ),
            (
                "INSERT INTO acceptance_evidence(goal_id,criterion_id,evidence_json,recorded_at) VALUES(?,?,?,?)",
                ("goal-one", "criterion", "{}", "2030-01-01T00:00:00Z"),
                "acceptance_evidence",
            ),
            (
                "INSERT INTO effect_intents(idempotency_key,goal_id,effect_class,operation,request_sha256,request_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("intent-one", "goal-one", "local-reversible-write", "write", "0" * 64, "{}", "received", "2030-01-01T00:00:00Z"),
                "effect_intents",
            ),
        )
        for statement, params, table in cases:
            with self.subTest(table=table):
                self._assert_direct_sql_is_detected(statement, params, table)

    def test_direct_sql_effect_receipt_is_detected(self) -> None:
        directory, store = self._store()
        try:
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    "INSERT INTO effect_intents(idempotency_key,goal_id,effect_class,operation,request_sha256,request_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("intent-one", "goal-one", "local-reversible-write", "write", "0" * 64, "{}", "received", "2030-01-01T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO effect_receipts(id,intent_key,outcome,evidence_json,performed_by,recorded_at) VALUES(?,?,?,?,?,?)",
                    ("receipt-one", "intent-one", "completed", "{}", "worker", "2030-01-01T00:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()
            report = store.verify_audit(limit=32)
            self.assertFalse(report["ok"])
            self.assertTrue(any("effect_receipts" in issue for issue in report["issues"]), report)
        finally:
            directory.cleanup()

    def test_mid_transaction_crash_rolls_back_state_event_and_seals(self) -> None:
        directory, store = self._store()
        try:
            before = store.verify_audit()
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                with store._connection() as connection:
                    store._prepare_write(connection)
                    connection.execute("UPDATE goals SET status='complete' WHERE id='goal-one'")
                    store._append_event_in_transaction(
                        connection, "fault.injected", goal_id="goal-one"
                    )
                    raise RuntimeError("simulated crash")
            self.assertEqual(store.get_goal("goal-one")["status"], "planned")
            after = store.verify_audit()
            self.assertTrue(after["ok"], after)
            self.assertEqual(after["checked"], before["checked"])
        finally:
            directory.cleanup()

    def test_write_locked_preflight_rejects_a_corrupt_audit_tail(self) -> None:
        directory, store = self._store()
        try:
            connection = sqlite3.connect(store.path)
            try:
                last = connection.execute("SELECT max(sequence) FROM audit_events").fetchone()[0]
                connection.execute(
                    "INSERT INTO audit_events(sequence,event_type,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (last + 1, "forged", "{}", "0" * 64, "1" * 64, "2030-01-01T00:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StateError, "audit integrity verification failed"):
                store.consume_budget("goal-one", 0)
            self.assertEqual(store.get_goal("goal-one")["status"], "planned")
        finally:
            directory.cleanup()

    def test_attestation_rejects_forged_lifecycle_and_invalid_enums(self) -> None:
        for statement, message in (
            ("UPDATE goals SET status='complete' WHERE id='goal-one'", "lifecycle evidence"),
            ("UPDATE work_units SET status='bogus' WHERE id='work-one'", "work-unit status"),
        ):
            directory, store = self._store()
            try:
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(statement)
                    connection.execute("DROP TABLE authority_seals")
                    connection.execute("PRAGMA user_version=3")
                    connection.commit()
                finally:
                    connection.close()
                store.migrate()
                with self.assertRaisesRegex(StateError, message):
                    store.attest_ledger(actor_id="human")
            finally:
                directory.cleanup()

    def test_contract_cannot_lower_budget_below_consumed_usage(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(
                title="Goal", description="Description", goal_id="goal-one",
                acceptance=["Done"], budget_tokens=10,
            )
            store.consume_budget("goal-one", 8)
            contract = self._checkpoint_contract()
            contract["checkpoints"] = []
            contract["budgets"]["tokens"] = 5
            with self.assertRaisesRegex(StateError, "below consumed"):
                store.define_goal_contract("goal-one", contract, actor_id="human")

    def test_stale_approval_is_attested_as_inert_history(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(
                title="Goal", description="Description", goal_id="goal-one",
                acceptance=["Done"],
            )
            first = self._checkpoint_contract()
            first["checkpoints"] = []
            stored = store.define_goal_contract("goal-one", first, actor_id="human")
            store.record_transition_approval(
                goal_id="goal-one", action="work-claim", effect="local-reversible-write",
                envelope_sha256=stored["envelope_sha256"], approver_id="human",
                performer_id="worker", valid_until="2031-01-01T00:00:00Z",
            )
            second = self._checkpoint_contract()
            second["checkpoints"] = []
            second["quality_requirements"] = ["new contract"]
            current = store.define_goal_contract("goal-one", second, actor_id="human")
            connection = sqlite3.connect(store.path)
            try:
                connection.execute("DROP TABLE authority_seals")
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            finally:
                connection.close()
            store.migrate()
            store.attest_ledger(actor_id="human")
            self.assertTrue(store.verify_audit()["ok"])
            with self.assertRaisesRegex(StateError, "no current approval"):
                store.check_authorization(
                    goal_id="goal-one", action="work-claim",
                    envelope_sha256=current["envelope_sha256"], performer_id="worker",
                    effect="local-reversible-write",
                )

    def test_v3_checkpoint_migration_backfills_and_normalizes_legacy_scope(self) -> None:
        directory = TemporaryDirectory()
        try:
            path = Path(directory.name) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(title="Goal", description="Description", goal_id="goal-one", acceptance=["Done"])
            store.define_goal_contract("goal-one", self._checkpoint_contract(), actor_id="steward")
            store.create_work_unit(goal_id="goal-one", title="Work", work_unit_id="work-one", checkpoint_id="first", scope={"paths": ["."], "exclusions": []})
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE work_units SET scope=? WHERE id='work-one'", ('{"paths":["."]}',))
                connection.execute("DROP TABLE goal_checkpoints")
                connection.execute("DROP TABLE authority_seals")
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(store.migrate(), SCHEMA_VERSION)
            self.assertEqual([row["checkpoint_id"] for row in store.get_goal_checkpoints("goal-one")], ["first", "second"])
            self.assertEqual(store.get_work_unit("work-one")["scope"], {"paths": ["."], "exclusions": []})
            store.attest_ledger(actor_id="human")
            self.assertTrue(store.verify_audit()["ok"])
        finally:
            directory.cleanup()

    def test_only_human_can_repair_an_unsealed_migrated_approval_scope(self) -> None:
        directory = TemporaryDirectory()
        try:
            store = StateStore(Path(directory.name) / "state.sqlite")
            store.create_goal(title="Goal", description="Description", goal_id="goal-one", acceptance=["Done"])
            contract = self._checkpoint_contract()
            contract["checkpoints"] = []
            contract["allowed_actions"] = ["work-claim"]
            stored = store.define_goal_contract("goal-one", contract, actor_id="steward")
            approval = store.record_transition_approval(
                goal_id="goal-one", action="work-claim", effect="local-reversible-write",
                envelope_sha256=stored["envelope_sha256"], approver_id="human", performer_id="worker",
                scope={"paths": ["."], "exclusions": []}, valid_until="2031-01-01T00:00:00Z", approval_id="approval-one",
            )
            connection = sqlite3.connect(store.path)
            try:
                connection.execute("UPDATE transition_approvals SET scope='{}' WHERE id='approval-one'")
                connection.execute("DROP TABLE authority_seals")
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            finally:
                connection.close()
            store.migrate()
            replacement = {
                "kind": "tasktra.transition-approval", "version": 1, "approval_id": approval["id"],
                "goal_id": "goal-one", "work_unit_id": None, "action": "work-claim", "effect": "local-reversible-write",
                "scope": {"paths": ["."], "exclusions": []}, "envelope_sha256": stored["envelope_sha256"],
                "decision": "approved", "approver": {"kind": "human", "id": "human"}, "performer_id": "worker",
                "authority_clause": "human approval", "evidence": [], "valid_until": "2031-01-01T00:00:00Z", "revoked_at": None,
            }
            with self.assertRaisesRegex(StateError, "human actor"):
                store.repair_unsealed_transition_approval("approval-one", replacement, actor_id="steward", actor_kind="steward")
            repaired = store.repair_unsealed_transition_approval("approval-one", replacement, actor_id="human", actor_kind="human")
            self.assertEqual(repaired["scope"], {"paths": ["."], "exclusions": []})
            self.assertFalse(store.verify_audit()["ok"])
            store.attest_ledger(actor_id="human")
            self.assertTrue(store.verify_audit()["ok"])
            with self.assertRaisesRegex(StateError, "only an unsealed"):
                store.repair_unsealed_transition_approval("approval-one", replacement, actor_id="human", actor_kind="human")
        finally:
            directory.cleanup()
