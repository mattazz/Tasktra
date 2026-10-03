from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import (
    AUTHORITY_ENVELOPE_KIND,
    AUTHORITY_ENVELOPE_VERSION,
    authority_envelope_sha256,
    transition_approval_subject_sha256,
)
from tasktra.handoffs import HANDOFF_KIND, HANDOFF_VERSION
from tasktra.state import StateError, StateStore
from tasktra.workflow import accept_handoff, new_workflow, workflow_completion_token


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def envelope():
    return {
        "kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION,
        "goal_id": "goal-1", "outcome": "Bounded autonomous work.", "motivation": "Test leases.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "goal-resume", "work-claim", "work-complete", "work-requeue", "effect-write", "effect-recovery-resolve", "goal-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": ["Test."],
        "budgets": {"tokens": 20, "attempts": 2, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": ["Stop."], "escalation_conditions": ["Escalate."],
    }


def completed_workflow():
    source = {"goal_id": "goal-1", "work_unit_id": "unit-1"}
    state = new_workflow(source)
    for role in ("implementer", "tester", "reviewer"):
        handoff = {
            "kind": HANDOFF_KIND, "version": HANDOFF_VERSION, "handoff_id": f"{role}-result",
            "source": source, "producer": {"role": role, "actor_id": f"{role}-one"},
            "human_summary": "Completed bounded work.", "status": {"state": "completed", "summary": "Passed."},
            "verified_facts": [{"statement": "Check passed.", "evidence_ids": ["check"]}], "inferences": [], "changed_paths": [],
            "validation_results": [{"name": "check", "outcome": "passed", "detail": "Passed.", "evidence_ids": ["check"]}],
            "evidence_refs": [{"id": "check", "kind": "command", "locator": "python -m unittest", "summary": "Check."}],
            "blockers": [], "downstream_brief": {"objective": "Continue.", "context": [], "constraints": [], "recommended_next_steps": []},
            "requested_actions": [],
        }
        state = accept_handoff(state, handoff)
    return state


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self._initialize()

    def _initialize(self, budget_overrides=None):
        self.directory = TemporaryDirectory()
        self.store = AutonomyStore(f"{self.directory.name}/state.sqlite")
        self.store.create_goal(goal_id="goal-1", title="Goal", description="Goal", acceptance=["Done."])
        self.envelope = envelope()
        self.envelope["budgets"].update(budget_overrides or {})
        self.digest = authority_envelope_sha256(self.envelope)
        self.store.define_goal_contract("goal-1", self.envelope, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-1", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.activate_goal("goal-1", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-1", title="Unit", scope={"paths": ["src/tasktra"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-1", work_unit_id="unit-1", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)

    def tearDown(self):
        self.directory.cleanup()

    def test_claim_is_bounded_and_stale_token_cannot_heartbeat(self):
        claim = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           token_reservation=5, lease_seconds=10, repository="repo", revision="abc", branch="main", workspace="work", at=NOW)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["context"], {"repository": "repo", "revision": "abc", "branch": "main", "workspace": "work"})
        self.assertIsNone(self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, repository="repo", revision="abc", branch="main", workspace="work", at=NOW))
        with self.assertRaisesRegex(AutonomyError, "token"):
            self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token="wrong", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "owner"):
            self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="intruder", lease_token=claim["lease_token"], at=NOW)
        with self.assertRaisesRegex(AutonomyError, "owner"):
            self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="intruder", lease_token=claim["lease_token"], outcome="blocked", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "expired"):
            self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW + timedelta(seconds=10))

    def test_validate_attempt_binds_host_to_current_claim_and_authority(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        validated = self.store.validate_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            envelope_sha256=self.digest, at=NOW,
        )
        self.assertEqual((validated["goal_id"], validated["work_unit_id"], validated["verification_policy"]),
                         ("goal-1", "unit-1", "implementation-review"))
        with self.store._connection(write=False) as connection:
            approval_id = connection.execute(
                "SELECT id FROM transition_approvals WHERE action='work-claim' AND performer_id='worker'"
            ).fetchone()[0]
        self.store.revoke_transition_approval(approval_id, actor_id="human", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "no current approval"):
            self.store.validate_attempt(
                attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                envelope_sha256=self.digest, at=NOW,
            )

    def test_contract_narrowing_cannot_strand_existing_planned_scope(self):
        self.directory.cleanup()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = AutonomyStore(f"{directory.name}/state.sqlite")
        store.create_goal(goal_id="goal-1", title="Goal", description="Goal", acceptance=["Done."])
        original = envelope()
        store.define_goal_contract("goal-1", original, actor_id="owner", at=NOW)
        store.create_work_unit(goal_id="goal-1", work_unit_id="src-unit", title="Source", scope={"paths": ["src"], "exclusions": []})
        narrowed = envelope()
        narrowed["scope"] = {"paths": ["docs"], "exclusions": []}
        with self.assertRaisesRegex(StateError, "strand an existing work unit scope"):
            store.define_goal_contract("goal-1", narrowed, actor_id="owner", at=NOW)

    def test_contract_replacement_cannot_revoke_existing_direct_policy(self):
        self.directory.cleanup()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = AutonomyStore(f"{directory.name}/state.sqlite")
        store.create_goal(goal_id="goal-1", title="Goal", description="Goal", acceptance=["Done."])
        authorized = envelope()
        authorized["allowed_actions"].append("verify-deterministic-direct")
        store.define_goal_contract("goal-1", authorized, actor_id="owner", at=NOW)
        store.create_work_unit(
            goal_id="goal-1", work_unit_id="direct-unit", title="Direct", scope={"paths": ["src"], "exclusions": []},
            verification_policy="deterministic-direct",
        )
        replacement = envelope()
        with self.assertRaisesRegex(StateError, "revoke an existing work unit verification policy"):
            store.define_goal_contract("goal-1", replacement, actor_id="owner", at=NOW)

    def test_caller_supplied_claim_token_is_never_returned(self):
        token = "s" * 32
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_token=token, repository="repo", revision="abc", branch="main",
            workspace="work", at=NOW,
        )
        self.assertIsNotNone(claim)
        self.assertNotIn("lease_token", claim)
        heartbeat = self.store.heartbeat(
            attempt_id=claim["attempt_id"], performer_id="worker",
            lease_token=token, at=NOW,
        )
        self.assertEqual(heartbeat["attempt_id"], claim["attempt_id"])

    def test_expired_lease_recovers_without_new_authority(self):
        claim = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           lease_seconds=1, repository="repo", revision="abc", branch="main", workspace="work", at=NOW)
        self.assertEqual(self.store.recover_expired_leases(at=NOW + timedelta(seconds=1)), [claim["attempt_id"]])
        unit = self.store.get_work_unit("unit-1")
        self.assertEqual((unit["status"], unit["current_attempt_id"]), ("retry-wait", None))
        with self.store._connection(write=False) as connection:
            attempt = connection.execute("SELECT ended_at,tokens_consumed,outcome_json FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
        self.assertEqual(attempt["ended_at"], "2030-01-01T00:00:01Z")
        self.assertEqual(attempt["tokens_consumed"], 0)
        self.assertEqual(json.loads(attempt["outcome_json"]), {"recovered": True})

    def test_unmeasured_reservations_are_charged_on_expiry_pause_and_emergency_stop(self):
        def attempt_for(claim):
            with self.store._connection(write=False) as connection:
                return connection.execute(
                    "SELECT tokens_reserved,tokens_consumed,outcome_json FROM work_attempts WHERE id=?",
                    (claim["attempt_id"],),
                ).fetchone()

        expired = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=5, lease_seconds=1, repository="repo", revision="expired", branch="main", workspace="work", at=NOW,
        )
        assert expired is not None
        self.store.recover_expired_leases(at=NOW + timedelta(seconds=1))
        attempt = attempt_for(expired)
        self.assertEqual((attempt["tokens_reserved"], attempt["tokens_consumed"]), (5, 5))
        self.assertEqual(json.loads(attempt["outcome_json"])["unmeasured_usage"], {
            "source": "reservation-charge", "state": "unmeasured", "charged_tokens": 5, "reason": "lease-expired",
        })
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_tokens"], 5)

        self.directory.cleanup()
        self._initialize()
        paused = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=6, repository="repo", revision="paused", branch="main", workspace="work", at=NOW,
        )
        assert paused is not None
        self.store.pause_goal("goal-1", actor_id="safety", at=NOW)
        attempt = attempt_for(paused)
        self.assertEqual((attempt["tokens_reserved"], attempt["tokens_consumed"]), (6, 6))
        self.assertEqual(json.loads(attempt["outcome_json"])["unmeasured_usage"]["reason"], "goal-paused")
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_tokens"], 6)

        self.directory.cleanup()
        self._initialize()
        stopped = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=7, repository="repo", revision="stopped", branch="main", workspace="work", at=NOW,
        )
        assert stopped is not None
        self.store.set_emergency_stop(actor_id="safety", reason="test", at=NOW)
        attempt = attempt_for(stopped)
        self.assertEqual((attempt["tokens_reserved"], attempt["tokens_consumed"]), (7, 7))
        self.assertEqual(json.loads(attempt["outcome_json"])["unmeasured_usage"]["reason"], "emergency-stop")
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_tokens"], 7)

    def test_effect_intent_replay_is_stable_and_conflicts_fail(self):
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-1", action="effect-write", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        first = self.store.prepare_effect(idempotency_key="effect-1", goal_id="goal-1", work_unit_id=None,
                                          effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/a"},
                                          envelope_sha256=self.digest, performer_id="worker", at=NOW)
        replay = self.store.prepare_effect(idempotency_key="effect-1", goal_id="goal-1", work_unit_id=None,
                                           effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/a"},
                                           envelope_sha256=self.digest, performer_id="worker", at=NOW)
        self.assertEqual(first["request_sha256"], replay["request_sha256"])
        self.assertEqual(replay["status"], "reconciliation-required")
        with self.assertRaisesRegex(AutonomyError, "conflicts"):
            self.store.prepare_effect(idempotency_key="effect-1", goal_id="goal-1", work_unit_id=None,
                                      effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/b"},
                                      envelope_sha256=self.digest, performer_id="worker", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "65536-byte"):
            self.store.prepare_effect(idempotency_key="effect-large", goal_id="goal-1", work_unit_id=None,
                                      effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"data": "x" * 65_536},
                                      envelope_sha256=self.digest, performer_id="worker", at=NOW)
        receipt = self.store.record_effect_receipt(idempotency_key="effect-1", outcome="applied", evidence={"ok": True}, performer_id="worker", after_sha256="a" * 64, at=NOW)
        self.assertEqual(receipt["outcome"], "applied")
        self.assertEqual(self.store.prepare_effect(idempotency_key="effect-1", goal_id="goal-1", work_unit_id=None,
                                                   effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/a"},
                                                   envelope_sha256=self.digest, performer_id="worker", at=NOW)["receipt"]["id"], receipt["id"])
        with self.assertRaisesRegex(AutonomyError, "SHA-256"):
            self.store.record_effect_receipt(idempotency_key="effect-1", outcome="applied", evidence={"ok": True}, performer_id="worker", after_sha256="bad", at=NOW)
        second = self.store.prepare_effect(idempotency_key="effect-2", goal_id="goal-1", work_unit_id=None,
                                           effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/c"},
                                           envelope_sha256=self.digest, performer_id="worker", at=NOW)
        self.assertEqual(second["status"], "pending")
        with self.assertRaisesRegex(AutonomyError, "authorized intent performer"):
            self.store.record_effect_receipt(idempotency_key="effect-2", outcome="applied", evidence={"ok": True}, performer_id="intruder", at=NOW)

    def test_indeterminate_effect_stays_outstanding_until_human_resolution(self):
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-1", action="effect-write", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.prepare_effect(idempotency_key="uncertain", goal_id="goal-1", work_unit_id=None,
                                  effect_class=LOCAL_REVERSIBLE_WRITE, operation="effect-write", request={"path": "src/a"},
                                  envelope_sha256=self.digest, performer_id="worker", at=NOW)
        self.store.record_effect_receipt(idempotency_key="uncertain", outcome="indeterminate", evidence={"probe": "timed out"}, performer_id="worker", at=NOW)
        self.assertEqual(self.store.inspect_effect("uncertain")["status"], "recovery-required")
        with self.assertRaisesRegex(AutonomyError, "no current approval"):
            self.store.resolve_effect_recovery(idempotency_key="uncertain", resolution="applied", evidence={"manual": True},
                                               performer_id="recovery-worker", envelope_sha256=self.digest, at=NOW)
        self.store.record_transition_approval(goal_id="goal-1", action="effect-recovery-resolve", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="recovery-worker", valid_until=expiry, at=NOW)
        resolved = self.store.resolve_effect_recovery(idempotency_key="uncertain", resolution="applied", evidence={"manual": True},
                                                      performer_id="recovery-worker", envelope_sha256=self.digest, at=NOW)
        self.assertEqual(resolved["status"], "received")

    def test_narrowed_approval_scope_cannot_cover_outside_or_excluded_work(self):
        expiry = NOW + timedelta(days=1)
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-docs", title="Docs", scope={"paths": ["docs"], "exclusions": []})
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-private", title="Private", scope={"paths": ["src/private"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-1", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="narrow-worker",
                                              scope={"paths": ["src"], "exclusions": ["src/private"]}, valid_until=expiry, at=NOW)
        with self.store._connection() as connection:
            self.store._authorize(connection, goal_id="goal-1", work_unit_id="unit-1", action="work-claim", envelope_sha256=self.digest,
                                  performer_id="narrow-worker", effect=LOCAL_REVERSIBLE_WRITE, timestamp="2030-01-01T00:00:00Z")
            for work_unit_id in ("unit-docs", "unit-private"):
                with self.subTest(work_unit_id=work_unit_id):
                    with self.assertRaisesRegex(AutonomyError, "no current approval"):
                        self.store._authorize(connection, goal_id="goal-1", work_unit_id=work_unit_id, action="work-claim", envelope_sha256=self.digest,
                                              performer_id="narrow-worker", effect=LOCAL_REVERSIBLE_WRITE, timestamp="2030-01-01T00:00:00Z")

    def test_empty_work_unit_scope_is_not_claimable(self):
        with self.assertRaisesRegex(StateError, "explicit closed scope"):
            self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-empty", title="Empty", scope={})

    def test_steward_cannot_record_final_acceptance_evidence(self):
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-1", action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="owner", valid_until=expiry, at=NOW)
        with self.assertRaisesRegex(AutonomyError, "no current approval"):
            self.store.record_acceptance_evidence(goal_id="goal-1", criterion_id="done", evidence={"proof": True},
                                                  performer_id="owner", envelope_sha256=self.digest, at=NOW)

    def test_blocked_work_requires_evidenced_authorized_requeue(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker",
            lease_token=claim["lease_token"], outcome="approval-required", at=NOW,
        )
        with self.assertRaisesRegex(AutonomyError, "nonempty"):
            self.store.requeue_work(
                work_unit_id="unit-1", performer_id="worker",
                envelope_sha256=self.digest, evidence={}, at=NOW,
            )
        with self.assertRaisesRegex(AutonomyError, "no current approval"):
            self.store.requeue_work(
                work_unit_id="unit-1", performer_id="worker",
                envelope_sha256=self.digest, evidence={"approval": "pending"}, at=NOW,
            )
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="unit-1", action="work-requeue",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=NOW + timedelta(days=1), at=NOW,
        )
        requeued = self.store.requeue_work(
            work_unit_id="unit-1", performer_id="worker", envelope_sha256=self.digest,
            evidence={"approval": "granted", "reference": "decision-one"}, at=NOW,
        )
        self.assertEqual(requeued["status"], "eligible")
        self.assertEqual(len(requeued["requeue_evidence_sha256"]), 64)
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_permanent_failure_requires_authority_before_requeue(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="permanent", at=NOW,
        )
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "failed")
        self.assertEqual(self.store.goal_execution_health("goal-1")["goal_status"], "active")
        with self.assertRaisesRegex(AutonomyError, "no current approval"):
            self.store.requeue_work(work_unit_id="unit-1", performer_id="worker", envelope_sha256=self.digest,
                                    evidence={"cause": "fixed"}, at=NOW)
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="unit-1", action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=NOW + timedelta(days=1), at=NOW,
        )
        self.assertEqual(self.store.requeue_work(
            work_unit_id="unit-1", performer_id="worker", envelope_sha256=self.digest,
            evidence={"cause": "fixed"}, at=NOW,
        )["status"], "eligible")

    def test_concurrent_leases_reserve_one_second_elapsed_budget_globally(self):
        self.directory.cleanup()
        self._initialize({"concurrency": 2, "elapsed_seconds": 1})
        expiry = NOW + timedelta(days=1)
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-two", title="Second", scope={"paths": ["src/two"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-1", work_unit_id="unit-two", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        first = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           lease_seconds=1, repository="repo", revision="one", branch="main", workspace="one", at=NOW)
        self.assertIsNotNone(first)
        with self.assertRaisesRegex(AutonomyError, "elapsed budget"):
            self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                       lease_seconds=1, repository="repo", revision="two", branch="main", workspace="two", at=NOW)

    def test_direct_budget_consumption_counts_live_token_reservations(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=18, repository="repo", revision="one",
            branch="main", workspace="work", at=NOW,
        )
        self.assertIsNotNone(claim)
        with self.assertRaisesRegex(StateError, "Budget would be exceeded"):
            self.store.consume_budget("goal-1", 3)

    def test_coordinator_attested_token_overrun_settles_debt_without_weakening_normal_finish(self):
        self.directory.cleanup()
        self._initialize({"tokens": 15})
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=15, repository="repo", revision="one", branch="main", workspace="work", at=NOW,
        )
        assert claim is not None
        with self.assertRaisesRegex(AutonomyError, "exceeds the reservation"):
            self.store.finish_attempt(
                attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                outcome="exhausted", tokens_consumed=16, at=NOW,
            )
        with self.assertRaisesRegex(AutonomyError, "total_tokens must equal"):
            self.store.finish_attempt(
                attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                outcome="exhausted", tokens_consumed=16, observed_token_overrun=True,
                observed_usage_evidence={"source": "coordinator-attested", "execution_ids": ["execution-one"], "total_tokens": 15},
                at=NOW,
            )
        settled = self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="exhausted", tokens_consumed=16, observed_token_overrun=True,
            observed_usage_evidence={"source": "coordinator-attested", "execution_ids": ["execution-one"], "total_tokens": 16},
            outcome_evidence={"reason": "coordinator attested CLI accounting"}, at=NOW,
        )
        self.assertEqual(settled["outcome"], "exhausted")
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "exhausted")
        self.assertEqual(self.store.budget_summary("goal-1")["remaining_tokens"], -1)
        with self.assertRaisesRegex(AutonomyError, "token budget would be exceeded"):
            self.store.claim_next_work(
                goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                token_reservation=0, repository="repo", revision="two", branch="main", workspace="work", at=NOW,
            )
        with self.store._connection(write=False) as connection:
            StateStore._validate_current_state_for_attestation(connection)

        # A direct counter edit cannot reuse evidence bound to a different
        # measured total to make a larger debt attestable.
        with self.store._connection() as connection:
            connection.execute("UPDATE work_attempts SET tokens_consumed=17 WHERE id=?", (claim["attempt_id"],))
            connection.execute("UPDATE budgets SET consumed_tokens=17 WHERE goal_id='goal-1'")
        with self.store._connection(write=False) as connection:
            with self.assertRaisesRegex(StateError, "invalid token-overrun evidence"):
                StateStore._validate_current_state_for_attestation(connection)

    def test_paused_reservation_history_does_not_mask_a_later_evidenced_overrun(self):
        self.directory.cleanup()
        self._initialize({"tokens": 17})
        first = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=2, repository="repo", revision="paused", branch="main", workspace="work", at=NOW,
        )
        assert first is not None
        self.store.pause_goal("goal-1", actor_id="safety", at=NOW)
        with self.store._connection(write=False) as connection:
            paused_attempt = connection.execute(
                "SELECT tokens_reserved,tokens_consumed FROM work_attempts WHERE id=?", (first["attempt_id"],)
            ).fetchone()
        self.assertEqual((paused_attempt["tokens_reserved"], paused_attempt["tokens_consumed"]), (2, 2))
        self.store.record_transition_approval(
            goal_id="goal-1", action="goal-resume", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=NOW + timedelta(days=1), at=NOW,
        )
        self.store.resume_goal("goal-1", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        second = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=15, repository="repo", revision="overrun", branch="main", workspace="work", at=NOW,
        )
        assert second is not None
        self.store.finish_attempt(
            attempt_id=second["attempt_id"], performer_id="worker", lease_token=second["lease_token"],
            outcome="exhausted", tokens_consumed=16, observed_token_overrun=True,
            observed_usage_evidence={"source": "coordinator-attested", "execution_ids": ["execution-two"], "total_tokens": 16},
            at=NOW,
        )
        with self.store._connection(write=False) as connection:
            StateStore._validate_current_state_for_attestation(connection)

    def test_heartbeat_cannot_extend_past_other_live_lease_reservation(self):
        self.directory.cleanup()
        self._initialize({"concurrency": 2, "elapsed_seconds": 4})
        expiry = NOW + timedelta(days=1)
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-two", title="Second", scope={"paths": ["src/two"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-1", work_unit_id="unit-two", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        first = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           lease_seconds=2, repository="repo", revision="one", branch="main", workspace="one", at=NOW)
        second = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                            lease_seconds=2, repository="repo", revision="two", branch="main", workspace="two", at=NOW)
        self.assertIsNotNone(second)
        heartbeat = self.store.heartbeat(attempt_id=first["attempt_id"], performer_id="worker", lease_token=first["lease_token"],
                                         lease_seconds=3, at=NOW + timedelta(seconds=1))
        self.assertEqual(heartbeat["lease_expires_at"], "2030-01-01T00:00:02Z")

    def test_pause_and_emergency_stop_cap_elapsed_at_lease_expiry(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=1, repository="repo", revision="one", branch="main",
            workspace="work", at=NOW,
        )
        self.store.pause_goal("goal-1", actor_id="safety", at=NOW + timedelta(seconds=10))
        paused_elapsed = self.store.budget_summary("goal-1")["consumed_elapsed_ms"]
        self.assertGreater(paused_elapsed, 0)
        self.assertLessEqual(paused_elapsed, 1000)

        self.directory.cleanup()
        self._initialize()
        self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=1, repository="repo", revision="one", branch="main",
            workspace="work", at=NOW,
        )
        self.store.set_emergency_stop(
            actor_id="safety", reason="test", at=NOW + timedelta(seconds=10)
        )
        stopped_elapsed = self.store.budget_summary("goal-1")["consumed_elapsed_ms"]
        self.assertGreater(stopped_elapsed, 0)
        self.assertLessEqual(stopped_elapsed, 1000)

    def test_transient_outcome_exhausts_after_goal_global_attempt_budget_is_reserved(self):
        self.directory.cleanup()
        self._initialize({"concurrency": 2, "attempts": 2})
        expiry = NOW + timedelta(days=1)
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-two", title="Second", scope={"paths": ["src/two"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-1", work_unit_id="unit-two", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        first = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           lease_seconds=1, repository="repo", revision="one", branch="main", workspace="one", at=NOW)
        self.assertIsNotNone(self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                                        lease_seconds=1, repository="repo", revision="two", branch="main", workspace="two", at=NOW))
        result = self.store.finish_attempt(attempt_id=first["attempt_id"], performer_id="worker", lease_token=first["lease_token"], outcome="transient", at=NOW)
        self.assertEqual(result["outcome"], "exhausted")

    def test_finished_workflow_evidence_and_acceptance_complete_goal(self):
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-1", work_unit_id="unit-1", action="work-complete", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-1", action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        claim = self.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
                                           repository="repo", revision="abc", branch="main", workspace="work", at=NOW)
        workflow = completed_workflow()
        result = self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                           outcome="success", workflow=workflow, completion_token=workflow_completion_token(workflow), at=NOW)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "complete")
        with self.store._connection(write=False) as connection:
            self.assertEqual(connection.execute("SELECT ended_at FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()[0], "2030-01-01T00:00:00Z")
        recorded = self.store.record_acceptance_evidence(
            goal_id="goal-1", criterion_id="done", evidence={"workflow": "complete"},
            performer_id="owner", envelope_sha256=self.digest, at=NOW,
        )
        binding = {
            "kind": "acceptance-evidence", "id": "done",
            "sha256": sha256(recorded["evidence_json"].encode("utf-8")).hexdigest(),
        }
        approval = {
            "kind": "tasktra.transition-approval", "version": 3,
            "approval_id": "goal-final-v3", "goal_id": "goal-1", "work_unit_id": None,
            "action": "goal-complete", "effect": LOCAL_REVERSIBLE_WRITE,
            "scope": {"paths": ["."], "exclusions": []}, "resource_scope": None,
            "envelope_sha256": self.digest, "decision": "approved",
            "approver": {"kind": "human", "id": "human"}, "performer_id": "owner",
            "authority_clause": "Final human ceremony binds every acceptance fact.",
            "evidence": [binding],
            "provenance": {"kind": "local-human-ceremony", "attester_id": "human",
                           "subject_sha256": "0" * 64, "attested_at": "2030-01-01T00:00:00Z"},
            "valid_until": "2030-01-02T00:00:00Z", "revoked_at": None,
        }
        approval["provenance"]["subject_sha256"] = transition_approval_subject_sha256(approval)
        self.store.record_transition_approval(
            goal_id="goal-1", action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=expiry, at=NOW, approval_id="goal-final-v3",
            authority_clause="Final human ceremony binds every acceptance fact.",
            evidence=[binding], provenance=approval["provenance"],
        )
        self.assertEqual(self.store.complete_goal(goal_id="goal-1", performer_id="owner", envelope_sha256=self.digest, at=NOW)["status"], "complete")
