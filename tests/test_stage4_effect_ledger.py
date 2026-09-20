from datetime import datetime, timedelta, timezone
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore
from tasktra.authority import authority_envelope_sha256
from tasktra.providers import OperationDescriptor
from tasktra.operations import operational_status
from tasktra.state import SCHEMA_VERSION, StateError, StateStore, _now
from tests.approval_helpers import v3_approval_kwargs


NOW = datetime(2031, 1, 1, tzinfo=timezone.utc)
SCOPE = {"provider": "github", "host": "github.com", "container": "acme", "resource_kind": "issue", "resource": "17", "ref": None}


def envelope():
    return {
        "kind": "tasktra.authority-envelope", "version": 2, "goal_id": "goal-one",
        "outcome": "Bounded provider write", "motivation": "Exercise durable effects", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done"}],
        "scope": {"paths": ["."], "exclusions": []}, "resource_scopes": [SCOPE],
        "allowed_actions": ["goal-activate", "work-claim", "remote-comment"],
        "allowed_effects": ["local-reversible-write", "external-communication"],
        "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 10, "attempts": 2, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


def descriptor():
    return OperationDescriptor(
        "github", "external-communication", capability="issue-comment",
        action="remote-comment", resource_scope=SCOPE,
    )


class ProviderEffectLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = AutonomyStore(f"{self.directory.name}/state.sqlite")
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done"])
        self.contract = envelope()
        self.digest = authority_envelope_sha256(self.contract)
        self.store.define_goal_contract("goal-one", self.contract, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="worker", valid_until=expiry, at=NOW)
        provider_kwargs = v3_approval_kwargs(
            approval_id="provider-ledger-v3", goal_id="goal-one", work_unit_id="unit-one",
            action="remote-comment", effect="external-communication",
            scope={"paths": ["."], "exclusions": []}, resource_scope=SCOPE,
            envelope_sha256=self.digest, approver_id="human", performer_id="worker",
            valid_until=expiry, attested_at=NOW,
        )
        self.provider_approval = self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="remote-comment", effect="external-communication", envelope_sha256=self.digest, approver_id="human", performer_id="worker", resource_scope=SCOPE, valid_until=expiry, at=NOW, **provider_kwargs)
        self.claim = self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest, lease_seconds=30, repository="repo", revision="abc", branch="main", workspace="work", at=NOW)

    def tearDown(self):
        self.directory.cleanup()

    def mark_indeterminate(self, key, attempt, *, at=NOW):
        return self.store.record_provider_effect_receipt(
            idempotency_key=key, effect_attempt_id=attempt["id"], outcome="indeterminate",
            receipt={"reason": "dispatch outcome requires reconciliation"},
            performer_id="worker", at=at,
        )

    def downgrade_provider_history_to_v7(self):
        connection = sqlite3.connect(self.store.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DROP TRIGGER effect_receipt_events_no_update")
            connection.execute(
                "UPDATE effect_receipt_events SET effect_attempt_id=NULL WHERE event_type='reconciliation'"
            )
            connection.execute("CREATE TRIGGER effect_receipt_events_no_update BEFORE UPDATE ON effect_receipt_events BEGIN SELECT RAISE(ABORT, 'effect receipt events are immutable'); END")
            connection.execute("ALTER TABLE effect_intents DROP COLUMN last_reconciliation_event_id")
            connection.execute("PRAGMA user_version=7")
            StateStore._seal_current_state_in_transaction(connection, _now(), existing_only=True)
            connection.commit()
        finally:
            connection.close()

    def test_provider_dispatch_is_single_and_receipts_are_redacted(self):
        operation = descriptor()
        intent = self.store.prepare_provider_effect(idempotency_key="comment-one", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=intent["idempotency_key"], operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "already executing"):
            self.store.begin_provider_effect_dispatch(idempotency_key=intent["idempotency_key"], operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        receipt = self.store.record_provider_effect_receipt(idempotency_key=intent["idempotency_key"], effect_attempt_id=first["id"], outcome="succeeded", receipt={"authorization": "Bearer never-store", "id": "22"}, performer_id="worker", at=NOW)
        self.assertNotIn("never-store", receipt["observation"])
        self.assertEqual(self.store.inspect_effect("comment-one")["status"], "succeeded")
        self.assertEqual(operational_status(self.store)["provider_effects"]["succeeded"], 1)
        self.assertEqual(operational_status(self.store)["provider_effects"]["indeterminate"], 0)
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_stale_dispatch_becomes_indeterminate(self):
        operation = descriptor()
        self.store.prepare_provider_effect(idempotency_key="comment-two", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        self.store.begin_provider_effect_dispatch(idempotency_key="comment-two", operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        stale = self.store.begin_provider_effect_dispatch(idempotency_key="comment-two", operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW + timedelta(seconds=31))
        self.assertEqual(stale["status"], "indeterminate")

    def test_active_executor_wins_reconciliation_race_and_can_record_receipt(self):
        operation = descriptor()
        key = "active-executor-race"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        attempt = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "requires an indeterminate effect"):
            self.store._record_adapter_provider_reconciliation(
                idempotency_key=key, resolution="absent", observation={"checked": True},
                performer_id="worker", at=NOW,
            )
        connection = sqlite3.connect(self.store.path)
        try:
            count = connection.execute(
                "SELECT count(*) FROM effect_receipt_events WHERE intent_key=?", (key,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        self.assertEqual(self.store.inspect_effect(key)["status"], "executing")
        self.store.record_provider_effect_receipt(
            idempotency_key=key, effect_attempt_id=attempt["id"], outcome="succeeded",
            receipt={"id": "executor-result"}, performer_id="worker", at=NOW,
        )
        self.assertEqual(self.store.inspect_effect(key)["status"], "succeeded")

    def test_indeterminate_attempt_rejects_a_second_terminal_receipt(self):
        operation = descriptor()
        key = "duplicate-terminal-receipt"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        attempt = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, attempt)
        with self.assertRaisesRegex(AutonomyError, "already has a terminal receipt"):
            self.store.record_provider_effect_receipt(
                idempotency_key=key, effect_attempt_id=attempt["id"], outcome="succeeded",
                receipt={"id": "late-success"}, performer_id="worker", at=NOW,
            )
        self.assertEqual(self.store.inspect_effect(key)["status"], "indeterminate")
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=NOW)["sealed_row_count"], 0)

    def test_dispatch_rejects_descriptor_different_from_the_prepared_identity(self):
        operation = descriptor()
        self.store.prepare_provider_effect(idempotency_key="descriptor-match", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        different = OperationDescriptor("github", "external-communication", capability="issue-comment", action="different-action", resource_scope=SCOPE)
        with self.assertRaisesRegex(AutonomyError, "descriptor differs"):
            self.store.begin_provider_effect_dispatch(idempotency_key="descriptor-match", operation_descriptor=different, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)

    def test_provider_requests_reject_value_credentials_and_observations_redact_them(self):
        operation = descriptor()
        for name, nested in (
            ("list", ["safe", {"note": "cookie=session=do-not-store"}]),
            ("key", {"Authorization: Bearer do-not-store": "safe"}),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(AutonomyError, "credentials or secrets"):
                self.store.prepare_provider_effect(idempotency_key=f"credential-request-{name}", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"nested": nested}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
            self.assertIsNone(self.store.inspect_effect(f"credential-request-{name}"))
        self.store.prepare_provider_effect(idempotency_key="credential-observation", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        event = self.store.append_provider_effect_receipt_event(idempotency_key="credential-observation", event_type="observation", observation={"nested": ["safe", "cookie=session=do-not-store"], "Authorization: Bearer do-not-store": "safe", "link": "https://user:pass@github.com/acme"}, performer_id="worker", at=NOW)
        self.assertNotIn("do-not-store", event["observation"])
        self.assertNotIn("user:pass", event["observation"])
        self.assertNotIn("Authorization", event["observation"])

    def test_provider_payloads_require_strict_json_containers(self):
        operation = descriptor()
        for name, nested in (("tuple", ("safe",)), ("set", {"safe"}), ("key", {1: "safe"})):
            with self.subTest(name=name), self.assertRaisesRegex(AutonomyError, "JSON"):
                self.store.prepare_provider_effect(idempotency_key=f"strict-json-{name}", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"nested": nested}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        self.store.prepare_provider_effect(idempotency_key="strict-json-observation", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "strict JSON"):
            self.store.append_provider_effect_receipt_event(idempotency_key="strict-json-observation", event_type="observation", observation={"nested": ("safe",)}, performer_id="worker", at=NOW)

    def test_raw_reconciliation_event_cannot_forge_adapter_absence(self):
        operation = descriptor()
        self.store.prepare_provider_effect(idempotency_key="forged-reconciliation", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        self.store.begin_provider_effect_dispatch(idempotency_key="forged-reconciliation", operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "only provider observation"):
            self.store.append_provider_effect_receipt_event(idempotency_key="forged-reconciliation", event_type="reconciliation", observation={"source": "adapter", "resolution": "absent", "observation": {"checked": True}}, performer_id="worker", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "only provider observation"):
            self.store.append_provider_effect_receipt_event(idempotency_key="forged-reconciliation", event_type="receipt", observation={"outcome": "succeeded", "receipt": {}}, performer_id="worker", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "absent reconciled"):
            self.store.retry_provider_effect(idempotency_key="forged-reconciliation", performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)

    def test_rejected_pending_receipt_leaves_no_ghost_event(self):
        operation = descriptor()
        key = "pending-ghost-receipt"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "executing or indeterminate"):
            self.store.record_provider_effect_receipt(
                idempotency_key=key, effect_attempt_id="effect-attempt-never-dispatched",
                outcome="succeeded", receipt={"id": "22"}, performer_id="worker", at=NOW,
            )
        connection = sqlite3.connect(self.store.path)
        try:
            count = connection.execute(
                "SELECT count(*) FROM effect_receipt_events WHERE intent_key=?", (key,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        self.assertEqual(self.store.inspect_effect(key)["status"], "pending")

    def test_delayed_receipt_cannot_terminalize_a_newer_dispatch(self):
        operation = descriptor()
        key = "delayed-old-receipt"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, first)
        self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        second = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "current dispatch attempt"):
            self.store.record_provider_effect_receipt(
                idempotency_key=key, effect_attempt_id=first["id"], outcome="succeeded",
                receipt={"id": "old"}, performer_id="worker", at=NOW,
            )
        self.assertEqual(self.store.inspect_effect(key)["status"], "executing")
        self.store.record_provider_effect_receipt(
            idempotency_key=key, effect_attempt_id=second["id"], outcome="succeeded",
            receipt={"id": "new"}, performer_id="worker", at=NOW,
        )
        self.assertEqual(self.store.inspect_effect(key)["status"], "succeeded")

    def test_historical_absence_cannot_attest_forged_terminal_state_after_retry(self):
        operation = descriptor()
        key = "historical-absence-forged-success"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, first)
        self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "UPDATE effect_intents SET status='succeeded' WHERE idempotency_key=?", (key,)
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "terminal receipt|inconsistent provider receipt|provider-effect binding"):
            self.store.attest_ledger(actor_id="human", at=NOW)

    def test_applied_reconciliation_rejects_later_adapter_absence_without_retry(self):
        operation = descriptor()
        key = "applied-then-absent"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        attempt = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, attempt)
        applied = self.store.reconcile_provider_effect(idempotency_key=key, resolution="applied", observation={"checked": True}, performer_id="worker", at=NOW)
        committed_id = applied["last_reconciliation_event_id"]
        with self.assertRaisesRegex(AutonomyError, "indeterminate"):
            self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": "again"}, performer_id="worker", at=NOW + timedelta(seconds=1))
        current = self.store.inspect_effect(key)
        self.assertEqual(current["last_reconciliation_event_id"], committed_id)
        with self.assertRaisesRegex(AutonomyError, "absent reconciliation"):
            self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW + timedelta(seconds=1))

    def test_manual_reconciliation_cannot_assert_absence(self):
        operation = descriptor()
        self.store.prepare_provider_effect(idempotency_key="manual-absence", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        self.store.begin_provider_effect_dispatch(idempotency_key="manual-absence", operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "manual.*applied or conflict"):
            self.store.reconcile_provider_effect(idempotency_key="manual-absence", resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)

    def test_provider_prepare_requires_approval_path_scope_to_cover_unit(self):
        self.store.revoke_transition_approval(self.provider_approval["id"], actor_id="human", at=NOW)
        narrowed_kwargs = v3_approval_kwargs(
            approval_id="provider-narrowed-v3", goal_id="goal-one", work_unit_id="unit-one",
            action="remote-comment", effect="external-communication",
            scope={"paths": ["docs"], "exclusions": []}, resource_scope=SCOPE,
            envelope_sha256=self.digest, approver_id="human", performer_id="worker",
            valid_until=NOW + timedelta(days=1), attested_at=NOW,
        )
        self.store.record_transition_approval(
            goal_id="goal-one", work_unit_id="unit-one", action="remote-comment",
            effect="external-communication", envelope_sha256=self.digest,
            approver_id="human", performer_id="worker", scope={"paths": ["docs"], "exclusions": []},
            resource_scope=SCOPE, valid_until=NOW + timedelta(days=1), at=NOW,
            **narrowed_kwargs,
        )
        with self.assertRaisesRegex(AutonomyError, "no current .*approval"):
            self.store.prepare_provider_effect(
                idempotency_key="approval-path-denied", goal_id="goal-one", work_unit_id="unit-one",
                operation_descriptor=descriptor(), request={"body": "hello"}, envelope_sha256=self.digest,
                performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW,
            )
        self.assertIsNone(self.store.inspect_effect("approval-path-denied"))

    def test_record_approval_rejects_resource_outside_current_envelope(self):
        outside = dict(SCOPE)
        outside["container"] = "another-owner"
        outside_kwargs = v3_approval_kwargs(
            approval_id="provider-outside-v3", goal_id="goal-one", work_unit_id="unit-one",
            action="remote-comment", effect="external-communication",
            scope={"paths": ["."], "exclusions": []}, resource_scope=outside,
            envelope_sha256=self.digest, approver_id="human", performer_id="worker",
            valid_until=NOW + timedelta(days=1), attested_at=NOW,
        )
        with self.assertRaisesRegex(StateError, "resource scope is outside"):
            self.store.record_transition_approval(
                goal_id="goal-one", work_unit_id="unit-one", action="remote-comment",
                effect="external-communication", envelope_sha256=self.digest,
                approver_id="human", performer_id="worker", resource_scope=outside,
                valid_until=NOW + timedelta(days=1), at=NOW, **outside_kwargs,
            )

    def test_retry_on_new_lease_preserves_historical_effect_attempt_attestation(self):
        operation = descriptor()
        intent = self.store.prepare_provider_effect(idempotency_key="retry-new-lease", goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=intent["idempotency_key"], operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(intent["idempotency_key"], first)
        self.store._record_adapter_provider_reconciliation(idempotency_key=intent["idempotency_key"], resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        self.store.recover_expired_leases(goal_id="goal-one", at=NOW + timedelta(seconds=31))
        replacement = self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest, lease_seconds=30, repository="repo", revision="abc", branch="main", workspace="work", at=NOW + timedelta(seconds=31))
        assert replacement is not None
        self.store.retry_provider_effect(idempotency_key=intent["idempotency_key"], performer_id="worker", work_attempt_id=replacement["attempt_id"], lease_token=replacement["lease_token"], at=NOW + timedelta(seconds=31))
        second = self.store.begin_provider_effect_dispatch(idempotency_key=intent["idempotency_key"], operation_descriptor=operation, performer_id="worker", lease_token=replacement["lease_token"], at=NOW + timedelta(seconds=31))
        self.assertNotEqual(first["work_attempt_id"], second["work_attempt_id"])
        self.assertEqual(self.store.inspect_effect(intent["idempotency_key"])["work_attempt_id"], replacement["attempt_id"])
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=NOW + timedelta(seconds=31))["sealed_row_count"], 0)

    def test_multiple_absent_reconciliation_cycles_attest_per_attempt(self):
        operation = descriptor()
        key = "two-reconciliation-cycles"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, first)
        first_reconciliation = self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"cycle": 1}, performer_id="worker", at=NOW)
        self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        second = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, second)
        second_reconciliation = self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"cycle": 2}, performer_id="worker", at=NOW)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first_reconciliation["last_reconciliation_event_id"], second_reconciliation["last_reconciliation_event_id"])
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=NOW)["sealed_row_count"], 0)

    def test_v7_migration_backfills_unambiguous_reconciliation_pointer(self):
        operation = descriptor()
        key = "migrated-reconciliation"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        attempt = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, attempt)
        reconciled = self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        event_id = reconciled["last_reconciliation_event_id"]
        self.downgrade_provider_history_to_v7()
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.store.inspect_effect(key)["last_reconciliation_event_id"], event_id)
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=NOW)["sealed_row_count"], 0)

    def test_v7_migration_binds_historical_absence_after_retry_is_pending(self):
        operation = descriptor()
        key = "migrated-pending-retry"
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        attempt = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, attempt)
        reconciled = self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        event_id = reconciled["last_reconciliation_event_id"]
        self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW + timedelta(seconds=1))
        self.downgrade_provider_history_to_v7()
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        migrated = self.store.inspect_effect(key)
        self.assertEqual((migrated["status"], migrated["last_reconciliation_event_id"]), ("pending", event_id))
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=NOW + timedelta(seconds=1))["sealed_row_count"], 0)

    def test_v7_migration_binds_historical_absence_after_retry_succeeded(self):
        operation = descriptor()
        key = "migrated-succeeded-retry"
        later = NOW + timedelta(seconds=1)
        self.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation, request={"body": "hello"}, envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=NOW)
        first = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=NOW)
        self.mark_indeterminate(key, first)
        reconciled = self.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker", at=NOW)
        event_id = reconciled["last_reconciliation_event_id"]
        self.store.retry_provider_effect(idempotency_key=key, performer_id="worker", work_attempt_id=self.claim["attempt_id"], lease_token=self.claim["lease_token"], at=later)
        second = self.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=self.claim["lease_token"], at=later)
        self.store.record_provider_effect_receipt(idempotency_key=key, effect_attempt_id=second["id"], outcome="succeeded", receipt={"id": "done"}, performer_id="worker", at=later)
        self.downgrade_provider_history_to_v7()
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        migrated = self.store.inspect_effect(key)
        self.assertEqual((migrated["status"], migrated["last_reconciliation_event_id"]), ("succeeded", event_id))
        self.assertGreater(self.store.attest_ledger(actor_id="human", at=later)["sealed_row_count"], 0)

    def test_attestation_rejects_tampered_v2_approval_resource_scope(self):
        tampered = dict(SCOPE)
        tampered["container"] = "another-owner"
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "UPDATE transition_approvals SET resource_scope=? WHERE action='remote-comment'",
                (json.dumps(tampered, sort_keys=True, separators=(",", ":")),),
            )
            connection.execute("DROP TABLE authority_seals")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(StateError, "provenance does not bind|outside its resource scope"):
            self.store.attest_ledger(actor_id="human", at=NOW)
