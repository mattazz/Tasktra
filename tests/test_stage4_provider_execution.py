from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyError, AutonomyStore
from tasktra.provider_execution import ProviderEffectExecutor
from tasktra.providers import FakeProvider, OperationDescriptor, ProviderError, ProviderHealth, ProviderRegistry, ProviderResult
from tasktra.state import StateStore
from tests.approval_helpers import v3_approval_kwargs


SCOPE = {
    "provider": "github", "host": "github.com", "container": "acme/widgets",
    "resource_kind": "issue", "resource": "12", "ref": None,
}
DESCRIPTOR = OperationDescriptor(
    "github", "external-communication", capability="issue-comment", action="remote-comment",
    resource_scope=SCOPE,
)
def envelope():
    return {
        "kind": "tasktra.authority-envelope", "version": 2, "goal_id": "goal-one",
        "outcome": "Send one approved comment", "motivation": "Executor test", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done"}],
        "scope": {"paths": ["."], "exclusions": []}, "resource_scopes": [SCOPE],
        "allowed_actions": ["goal-activate", "work-claim", "remote-comment"],
        "allowed_effects": ["local-reversible-write", "external-communication"],
        "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 20, "attempts": 3, "elapsed_seconds": 600, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


class ProviderEffectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "state.sqlite3"
        StateStore(path).migrate()
        self.store = AutonomyStore(path)
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done"])
        self.contract = envelope()
        self.digest = authority_envelope_sha256(self.contract)
        self.store.define_goal_contract("goal-one", self.contract, actor_id="owner", at=self.now)
        expiry = self.now + timedelta(minutes=10)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=self.now)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=self.now)
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="worker", valid_until=expiry, at=self.now)
        provider_kwargs = v3_approval_kwargs(
            approval_id="provider-execution-v3", goal_id="goal-one", work_unit_id="unit-one",
            action="remote-comment", effect="external-communication",
            scope={"paths": ["."], "exclusions": []}, resource_scope=SCOPE,
            envelope_sha256=self.digest, approver_id="human", performer_id="worker",
            valid_until=expiry, attested_at=self.now,
        )
        self.approval = self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="remote-comment", effect="external-communication", envelope_sha256=self.digest, approver_id="human", performer_id="worker", resource_scope=SCOPE, valid_until=expiry, at=self.now, **provider_kwargs)
        self.claim = self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest, lease_seconds=600, repository="repo", revision="abc", branch="main", workspace="work", at=self.now)
        self.fake = FakeProvider(ProviderHealth("github", "available", "GitHub is available"))
        self.registry = ProviderRegistry()
        self.registry.register_provider("github", discovery=self.fake, operations={DESCRIPTOR: self.fake})
        self.executor = ProviderEffectExecutor(self.store, self.registry)

    def tearDown(self):
        self.directory.cleanup()

    def _prepare(self, key):
        return self.store.prepare_provider_effect(
            idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one",
            operation_descriptor=DESCRIPTOR, request={"body": "Approved"},
            envelope_sha256=self.digest, performer_id="worker", work_attempt_id=self.claim["attempt_id"],
            lease_token=self.claim["lease_token"], at=self.now,
        )

    def test_registry_has_no_public_write_bypass(self):
        self.assertFalse(hasattr(self.registry, "execute"))

    def test_denied_authority_never_invokes_the_adapter(self):
        self._prepare("denied-comment")
        self.store.revoke_transition_approval(self.approval["id"], actor_id="human", at=self.now)
        with self.assertRaisesRegex(AutonomyError, "no current .*approval"):
            self.executor.execute(idempotency_key="denied-comment", operation_descriptor=DESCRIPTOR,
                                  performer_id="worker", lease_token=self.claim["lease_token"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.store.inspect_effect("denied-comment")["status"], "pending")

    def test_executor_records_succeeded_and_indeterminate_receipts(self):
        self._prepare("succeeded-comment")
        completed = self.executor.execute(idempotency_key="succeeded-comment", operation_descriptor=DESCRIPTOR,
                                          performer_id="worker", lease_token=self.claim["lease_token"])
        self.assertEqual(completed["outcome"], "succeeded")
        self.assertEqual(self.store.inspect_effect("succeeded-comment")["status"], "succeeded")
        self.assertEqual([call[0] for call in self.fake.calls], ["effect"])

        self.fake.effect_result = ProviderResult("indeterminate", "GitHub timeout")
        self._prepare("indeterminate-comment")
        unresolved = self.executor.execute(idempotency_key="indeterminate-comment", operation_descriptor=DESCRIPTOR,
                                           performer_id="worker", lease_token=self.claim["lease_token"])
        self.assertEqual(unresolved["outcome"], "indeterminate")
        self.assertEqual(self.store.inspect_effect("indeterminate-comment")["status"], "indeterminate")

    def test_adapter_provider_error_after_dispatch_is_indeterminate(self):
        class ThrowsAfterEffect(FakeProvider):
            def execute(self, descriptor, scope, request, idempotency_key):
                self.calls.append(("effect", {"idempotency_key": idempotency_key}))
                raise ProviderError("the adapter cannot prove rejection")

        provider = ThrowsAfterEffect(ProviderHealth("github", "available", "configured"))
        registry = ProviderRegistry()
        registry.register_provider("github", discovery=provider, operations={DESCRIPTOR: provider})
        executor = ProviderEffectExecutor(self.store, registry)
        self._prepare("provider-error-comment")

        result = executor.execute(
            idempotency_key="provider-error-comment",
            operation_descriptor=DESCRIPTOR,
            performer_id="worker",
            lease_token=self.claim["lease_token"],
        )

        self.assertEqual(result["outcome"], "indeterminate")
        self.assertEqual(self.store.inspect_effect("provider-error-comment")["status"], "indeterminate")
        self.assertEqual(len(provider.calls), 1)

    def test_executor_returns_stale_dispatch_recovery_without_calling_adapter(self):
        self._prepare("stale-executor-comment")
        self.store.begin_provider_effect_dispatch(
            idempotency_key="stale-executor-comment",
            operation_descriptor=DESCRIPTOR,
            performer_id="worker",
            lease_token=self.claim["lease_token"],
            at=self.now,
        )

        result = self.executor.execute(
            idempotency_key="stale-executor-comment",
            operation_descriptor=DESCRIPTOR,
            performer_id="worker",
            lease_token=self.claim["lease_token"],
            at=self.now + timedelta(minutes=11),
        )

        self.assertEqual(result["outcome"], "indeterminate")
        self.assertIsNone(result["effect_attempt"])
        self.assertEqual(self.store.inspect_effect("stale-executor-comment")["status"], "indeterminate")
        self.assertEqual(self.fake.calls, [])

    def test_adapter_confirmed_absence_is_the_only_executor_retry_path(self):
        self.fake.effect_result = ProviderResult("indeterminate", "GitHub timeout")
        self._prepare("retry-comment")
        self.executor.execute(idempotency_key="retry-comment", operation_descriptor=DESCRIPTOR,
                              performer_id="worker", lease_token=self.claim["lease_token"])
        self.fake.effect_result = ProviderResult("absent", "Exact idempotency marker is absent")
        reconciled = self.executor.reconcile(idempotency_key="retry-comment", operation_descriptor=DESCRIPTOR,
                                             performer_id="worker")
        self.assertEqual(reconciled["resolution"], "absent")
        self.assertEqual(self.store.inspect_effect("retry-comment")["status"], "reconciled")
        retried = self.store.retry_provider_effect(idempotency_key="retry-comment", performer_id="worker",
                                                   work_attempt_id=self.claim["attempt_id"],
                                                   lease_token=self.claim["lease_token"])
        self.assertEqual(retried["status"], "pending")

    def test_indeterminate_reconciliation_does_not_unlock_retry(self):
        self.fake.effect_result = ProviderResult("indeterminate", "GitHub timeout")
        self._prepare("indeterminate-reconcile")
        self.executor.execute(idempotency_key="indeterminate-reconcile", operation_descriptor=DESCRIPTOR,
                              performer_id="worker", lease_token=self.claim["lease_token"])
        report = self.executor.reconcile(idempotency_key="indeterminate-reconcile", operation_descriptor=DESCRIPTOR,
                                         performer_id="worker")
        self.assertEqual(report["resolution"], "indeterminate")
        with self.assertRaisesRegex(AutonomyError, "absent"):
            self.store.retry_provider_effect(idempotency_key="indeterminate-reconcile", performer_id="worker",
                                             work_attempt_id=self.claim["attempt_id"],
                                             lease_token=self.claim["lease_token"])

    def test_reconciliation_cannot_race_an_active_dispatch(self):
        self._prepare("active-dispatch")
        self.store.begin_provider_effect_dispatch(
            idempotency_key="active-dispatch", operation_descriptor=DESCRIPTOR,
            performer_id="worker", lease_token=self.claim["lease_token"], at=self.now,
        )
        self.fake.effect_result = ProviderResult("absent", "marker not found")

        with self.assertRaisesRegex(AutonomyError, "indeterminate"):
            self.executor.reconcile(
                idempotency_key="active-dispatch", operation_descriptor=DESCRIPTOR,
                performer_id="worker",
            )

        self.assertEqual(self.store.inspect_effect("active-dispatch")["status"], "executing")
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
