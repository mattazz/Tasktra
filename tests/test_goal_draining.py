from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tasktra.providers import OperationDescriptor
from tests.approval_helpers import v3_approval_kwargs
from tests.runtime_schema_helpers import peel_schema13_interventions


NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)


def envelope() -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
        "outcome": "Drain safely", "motivation": "Test draining.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "goal-resume", "work-claim", "work-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 20, "attempts": 4, "elapsed_seconds": 120, "concurrency": 2},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


class GoalDrainingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.store = AutonomyStore(self.path)
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        self.envelope = envelope()
        self.digest = authority_envelope_sha256(self.envelope)
        self.store.define_goal_contract("goal-one", self.envelope, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-resume", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        for unit_id in ("unit-one", "unit-two"):
            self.store.create_work_unit(goal_id="goal-one", work_unit_id=unit_id, title=unit_id,
                                        scope={"paths": ["src"], "exclusions": []})

    def tearDown(self) -> None:
        self.directory.cleanup()

    def claim(self, *, lease_seconds: int = 30) -> dict:
        result = self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest,
                                            lease_seconds=lease_seconds, repository="repo", revision="rev", branch="main",
                                            workspace="work", at=NOW)
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_preview_is_read_only_and_empty_apply_pauses(self) -> None:
        before = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        preview = self.store.preview_goal_drain("goal-one", at=NOW)
        after = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(preview["read_only"])
        self.assertEqual((preview["status_before"], preview["status_after"], preview["stored_leases"]), ("active", "paused", 0))
        applied = self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.assertEqual((applied["status_before"], applied["status_after"], applied["stored_leases"]), ("active", "paused", 0))
        before_retry = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        retry = self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        after_retry = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        self.assertEqual((retry["status_before"], retry["status_after"]), ("paused", "paused"))
        self.assertEqual(before_retry, after_retry)

    def test_multiple_leases_drain_finish_and_recovery_pause_on_last(self) -> None:
        first = self.claim(lease_seconds=1)
        second = self.claim(lease_seconds=30)
        observed = self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.assertEqual((observed["status_after"], observed["stored_leases"], observed["live_leases"]), ("draining", 2, 2))
        with self.assertRaisesRegex(AutonomyError, "not active"):
            self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest,
                                       repository="repo", revision="rev", branch="main", workspace="work", at=NOW)
        before_budget = self.store.budget_summary("goal-one")
        heartbeat = self.store.heartbeat(attempt_id=second["attempt_id"], performer_id="worker", lease_token=second["lease_token"], at=NOW)
        self.assertGreater(heartbeat["lease_expires_at"], second["lease_expires_at"])
        self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], before_budget["reserved_tokens"])
        self.store.finish_attempt(attempt_id=second["attempt_id"], performer_id="worker", lease_token=second["lease_token"],
                                  outcome="blocked", at=NOW)
        self.assertEqual(self.store.get_goal("goal-one")["status"], "draining")
        self.store.recover_expired_leases(at=NOW + timedelta(seconds=1))
        self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")
        self.store.resume_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW + timedelta(seconds=2))
        self.assertEqual(self.store.get_goal("goal-one")["status"], "active")
        self.assertEqual(self.store.get_work_unit(first["work_unit_id"])["current_attempt_id"], None)

    def test_resume_during_drain_preserves_the_live_lease(self) -> None:
        claim = self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        before_retry = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        retry = self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        after_retry = {item.name: item.read_bytes() for item in self.path.parent.iterdir() if item.is_file()}
        self.assertEqual((retry["status_before"], retry["status_after"], retry["stored_leases"]), ("draining", "draining", 1))
        self.assertEqual(before_retry, after_retry)
        self.store.resume_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        self.assertEqual(self.store.get_goal("goal-one")["status"], "active")
        heartbeat = self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW)
        self.assertEqual(heartbeat["attempt_id"], claim["attempt_id"])

    def test_non_success_finish_variants_finalize_the_last_draining_lease(self) -> None:
        for index, outcome in enumerate(("transient", "permanent", "exhausted", "blocked", "approval-required")):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(outcome=outcome):
                claim = self.claim(lease_seconds=30)
                self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
                self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                          outcome=outcome, at=NOW)
                self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")

    def test_immediate_controls_cancel_draining_leases_with_accounting(self) -> None:
        claim = self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.store.pause_goal("goal-one", actor_id="owner", at=NOW)
        self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")
        self.assertEqual(self.store.get_work_unit(claim["work_unit_id"])["current_attempt_id"], None)
        self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], 0)

    def test_stop_and_emergency_stop_cancel_draining_leases(self) -> None:
        claim = self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.store.stop_goal("goal-one", actor_id="owner", at=NOW)
        self.assertEqual(self.store.get_goal("goal-one")["status"], "stopped")
        self.assertEqual(self.store.get_work_unit(claim["work_unit_id"])["current_attempt_id"], None)
        self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], 0)

    def test_attestation_accepts_a_live_drain_and_rejects_an_empty_drain(self) -> None:
        self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.assertEqual(self.store.attest_ledger(actor_id="human")["actor_id"], "human")
        self.store.pause_goal("goal-one", actor_id="owner", at=NOW)
        with self.store._connection() as connection:
            self.store._prepare_write(connection, allow_unsealed=True)
            connection.execute("UPDATE goals SET status='draining' WHERE id='goal-one'")
            self.store._append_event_in_transaction(connection, "goal.draining", goal_id="goal-one", payload={"actor_id": "owner"})
        with self.assertRaisesRegex(StateError, "draining goal without a stored lease"):
            self.store.attest_ledger(actor_id="human")

    def test_paused_goal_with_a_held_lease_is_rejected_as_invalid(self) -> None:
        self.claim(lease_seconds=30)
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE goals SET status='paused' WHERE id='goal-one'")
            self.store._append_event_in_transaction(connection, "goal.paused", goal_id="goal-one", payload={"actor_id": "owner"})
        with self.assertRaisesRegex(StateError, "paused goal holds a stored lease"):
            self.store.drain_goal("goal-one", actor_id="owner", at=NOW)

    def test_emergency_stop_cancels_a_draining_lease(self) -> None:
        claim = self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        self.assertTrue(self.store.set_emergency_stop(actor_id="owner", reason="test", at=NOW))
        self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")
        self.assertEqual(self.store.get_work_unit(claim["work_unit_id"])["current_attempt_id"], None)
        self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], 0)

    def test_live_provider_lease_continues_during_drain_but_unleased_effect_is_denied(self) -> None:
        scope = {
            "provider": "github", "host": "github.com", "container": "acme/widgets",
            "resource_kind": "issue", "resource": "12", "ref": None,
        }
        descriptor = OperationDescriptor(
            "github", "external-communication", capability="issue-comment", action="remote-comment",
            resource_scope=scope,
        )
        contract = dict(self.envelope)
        contract.update({
            "version": 2, "allowed_actions": ["goal-activate", "goal-resume", "work-claim", "remote-comment"],
            "allowed_effects": [LOCAL_REVERSIBLE_WRITE, "external-communication"], "resource_scopes": [scope],
        })
        self.store = AutonomyStore(self.path)
        # The v1 test setup cannot be redefined while active; use a second, isolated v2 store.
        with TemporaryDirectory() as directory:
            store = AutonomyStore(Path(directory) / "provider.sqlite")
            store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            digest = authority_envelope_sha256(contract)
            store.define_goal_contract("goal-one", contract, actor_id="owner", at=NOW)
            expiry = NOW + timedelta(days=1)
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
            store.activate_goal("goal-one", actor_id="owner", envelope_sha256=digest, at=NOW)
            store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
            store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="human", performer_id="worker", valid_until=expiry, at=NOW)
            approval = v3_approval_kwargs(approval_id="drain-provider-v3", goal_id="goal-one", work_unit_id="unit-one",
                                           action="remote-comment", effect="external-communication", scope={"paths": ["."], "exclusions": []},
                                           resource_scope=scope, envelope_sha256=digest, approver_id="human", performer_id="worker",
                                           valid_until=expiry, attested_at=NOW)
            store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="remote-comment", effect="external-communication",
                                             envelope_sha256=digest, approver_id="human", performer_id="worker", resource_scope=scope,
                                             valid_until=expiry, at=NOW, **approval)
            claim = store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=digest,
                                          repository="repo", revision="rev", branch="main", workspace="work", at=NOW)
            assert claim is not None
            store.drain_goal("goal-one", actor_id="owner", at=NOW)
            prepared = store.prepare_provider_effect(idempotency_key="provider-drain", goal_id="goal-one", work_unit_id="unit-one",
                                                     operation_descriptor=descriptor, request={"body": "Approved"}, envelope_sha256=digest,
                                                     performer_id="worker", work_attempt_id=claim["attempt_id"], lease_token=claim["lease_token"], at=NOW)
            self.assertEqual(prepared["status"], "pending")
            with self.assertRaisesRegex(AutonomyError, "not active"):
                store.prepare_effect(idempotency_key="generic-drain", goal_id="goal-one", work_unit_id=None,
                                     effect_class=LOCAL_REVERSIBLE_WRITE, operation="remote-comment", request={"x": 1},
                                     envelope_sha256=digest, performer_id="worker", at=NOW)

    def test_expired_stored_leases_remain_visible_until_recovery_and_reports_page_without_secrets(self) -> None:
        self.claim(lease_seconds=1)
        second = self.claim(lease_seconds=30)
        self.store.drain_goal("goal-one", actor_id="owner", at=NOW)
        report = self.store.preview_goal_drain("goal-one", limit=1, offset=0, at=NOW + timedelta(seconds=2))
        self.assertEqual((report["stored_leases"], report["live_leases"], report["expired_leases"]), (2, 1, 1))
        self.assertEqual(len(report["attempts"]), 1)
        self.assertNotIn("lease_token", report["attempts"][0])
        self.assertEqual(report["next_offset"], 1)
        second_page = self.store.preview_goal_drain("goal-one", limit=1, offset=1, at=NOW + timedelta(seconds=2))
        self.assertEqual(second_page["lease_set_sha256"], report["lease_set_sha256"])
        with self.assertRaisesRegex(StateError, "limit"):
            self.store.preview_goal_drain("goal-one", limit=101, at=NOW)
        self.store.recover_expired_leases(at=NOW + timedelta(seconds=2))
        self.assertEqual(self.store.get_goal("goal-one")["status"], "draining")
        self.store.finish_attempt(attempt_id=second["attempt_id"], performer_id="worker", lease_token=second["lease_token"], outcome="blocked", at=NOW + timedelta(seconds=2))
        self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")

    def test_schema_eleven_migrates_legacy_goals_and_rejects_invalid_status(self) -> None:
        store = StateStore(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            peel_schema13_interventions(connection, target_version=11)
            connection.execute("PRAGMA user_version = 11")
            StateStore._seal_current_state_in_transaction(connection, NOW.isoformat().replace("+00:00", "Z"), existing_only=True)
            connection.commit()
        finally:
            connection.close()
        evidence = store.migrate_with_evidence()
        self.assertEqual(evidence["after_schema"], SCHEMA_VERSION)
        self.assertEqual(store.get_goal("goal-one")["status"], "active")
        backup = Path(str(evidence["backup_path"]))
        self.assertTrue(backup.is_file())
        self.assertEqual(evidence["backup_sha256"], sha256(backup.read_bytes()).hexdigest())
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            peel_schema13_interventions(connection, target_version=11)
            connection.execute("UPDATE goals SET status='draining' WHERE id='goal-one'")
            connection.execute("PRAGMA user_version = 11")
            with self.assertRaisesRegex(StateError, "invalid schema11 goal status"):
                StateStore._migrate(connection)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
