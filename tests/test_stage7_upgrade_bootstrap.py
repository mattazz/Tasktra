"""Regression coverage for the authority-preserving self-host upgrade bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.state import SCHEMA_VERSION, StateError, StateStore, _now


def envelope() -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "upgrade-goal",
        "outcome": "Upgrade the local harness", "motivation": "Self-host regression.",
        "author_id": "owner", "acceptance_criteria": [{"id": "done", "statement": "Upgraded."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "local-effect"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
        "quality_requirements": [],
        "budgets": {"tokens": 100, "attempts": 2, "elapsed_seconds": 600, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


class UpgradeBootstrapTests(unittest.TestCase):
    def _schema_eight_store(self, root: Path) -> tuple[AutonomyStore, str]:
        store = AutonomyStore(root / "tasktra.sqlite")
        store.migrate()
        store.create_goal(
            goal_id="upgrade-goal", title="Upgrade", description="Upgrade",
            acceptance=["Upgraded."],
        )
        contract = envelope()
        digest = authority_envelope_sha256(contract)
        now = datetime.now(timezone.utc)
        store.define_goal_contract("upgrade-goal", contract, actor_id="owner", at=now)
        store.record_transition_approval(
            goal_id="upgrade-goal", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="owner",
            valid_until=now + timedelta(hours=1), at=now,
        )
        store.activate_goal("upgrade-goal", actor_id="owner", envelope_sha256=digest, at=now)
        connection = sqlite3.connect(store.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DROP TABLE schedule_resume_idempotency")
            connection.execute("ALTER TABLE transition_approvals DROP COLUMN provenance")
            connection.execute("PRAGMA user_version = 8")
            StateStore._seal_current_state_in_transaction(
                connection, _now(), existing_only=True,
            )
            connection.commit()
        finally:
            connection.close()
        return store, digest

    def test_exact_steward_approval_and_effect_can_bootstrap_schema_eight(self) -> None:
        with TemporaryDirectory() as directory:
            store, digest = self._schema_eight_store(Path(directory))
            approval = store.record_transition_approval(
                goal_id="upgrade-goal", action="local-effect", effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=digest, approver_id="goal-steward",
                performer_id="coordinator", approver_kind="steward",
                authority_clause="exact reviewed upgrade plan",
                evidence=["upgrade-plan-reviewed"],
                valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
                approval_id="upgrade-local-effect",
            )
            self.assertEqual(approval["id"], "upgrade-local-effect")
            intent = store.prepare_effect(
                idempotency_key="upgrade-once", goal_id="upgrade-goal", work_unit_id=None,
                effect_class=LOCAL_REVERSIBLE_WRITE, operation="local-effect",
                request={"action": "upgrade-apply", "plan_sha256": "a" * 64},
                envelope_sha256=digest, performer_id="coordinator",
            )
            self.assertEqual(intent["status"], "pending")

            migration = store.migrate_with_evidence()

            self.assertEqual((migration["before_schema"], migration["after_schema"]), (8, SCHEMA_VERSION))
            self.assertTrue(Path(str(migration["backup_path"])).is_file())
            self.assertTrue(store.verify_audit()["ok"])

    def test_bridge_denies_broader_approval_and_denies_effect_without_approval(self) -> None:
        with TemporaryDirectory() as directory:
            store, digest = self._schema_eight_store(Path(directory))
            with self.assertRaisesRegex(StateError, "only permits"):
                store.record_transition_approval(
                    goal_id="upgrade-goal", action="local-effect", effect="deployment",
                    envelope_sha256=digest, approver_id="goal-steward",
                    performer_id="coordinator", approver_kind="steward",
                    valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            with self.assertRaisesRegex(AutonomyError, "no current approval"):
                store.prepare_effect(
                    idempotency_key="upgrade-denied", goal_id="upgrade-goal", work_unit_id=None,
                    effect_class=LOCAL_REVERSIBLE_WRITE, operation="local-effect",
                    request={"action": "upgrade-apply", "plan_sha256": "b" * 64},
                    envelope_sha256=digest, performer_id="coordinator",
                )
            connection = sqlite3.connect(store.path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertEqual(connection.execute("SELECT count(*) FROM effect_intents").fetchone()[0], 0)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
