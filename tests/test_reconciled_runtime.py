"""Behavior spanning the dependency/drain and supervisor/accounting lineages."""
from datetime import timedelta
import unittest

from tasktra.autonomy import AutonomyError, LOCAL_REVERSIBLE_WRITE
from tests import test_stage3_autonomy as stage3


class ReconciledRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stage3.AutonomyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.digest = self.fixture.digest

    def claim(self, unit=None, **kwargs):
        return self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="rev", branch="main", workspace="work",
            work_unit_id=unit, at=stage3.NOW, **kwargs,
        )

    def test_targeted_claim_cannot_select_another_unit_or_bypass_dependencies(self):
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="dependent", title="Dependent",
            scope={"paths": ["src"], "exclusions": []}, prerequisite_ids=["unit-1"],
        )
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="dependent", action="work-claim",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
        )
        self.assertIsNone(self.claim("dependent"))
        self.assertIsNone(self.claim("missing"))
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "planned")
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="unit-1", action="work-complete",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
        )
        first = self.claim("unit-1")
        self.store.finish_attempt(
            attempt_id=first["attempt_id"], performer_id="worker", lease_token=first["lease_token"],
            outcome="success", workflow=stage3.completed_workflow(), at=stage3.NOW,
        )
        self.assertEqual(self.claim("dependent")["work_unit_id"], "dependent")

    def test_supervisor_validation_continues_during_drain_until_settlement(self):
        claim = self.claim("unit-1")
        self.store.drain_goal("goal-1", actor_id="owner", at=stage3.NOW)
        verified = self.store.validate_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            envelope_sha256=self.digest, at=stage3.NOW,
        )
        self.assertEqual(verified["verification_policy"], "implementation-review")
        with self.assertRaisesRegex(AutonomyError, "not active"):
            self.claim("unit-1")
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="blocked", at=stage3.NOW,
        )
        self.assertEqual(self.store.get_goal("goal-1")["status"], "paused")

    def test_overrun_attestation_cannot_be_relabelled_as_host_measurement(self):
        claim = self.claim("unit-1", token_reservation=5)
        args = dict(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="exhausted", tokens_consumed=6, observed_token_overrun=True,
            observed_usage_evidence={"source": "coordinator-attested", "execution_ids": ["execution-one"], "total_tokens": 6},
            at=stage3.NOW,
        )
        with self.assertRaisesRegex(AutonomyError, "host-measured accounting requires"):
            self.store.finish_attempt(**args, accounting_source="host-measured")
        result = self.store.finish_attempt(**args)
        self.assertEqual(result["token_accounting_source"], "caller-declared")
        self.assertEqual(result["outcome"], "exhausted")

    def test_expiry_charges_reservation_without_claiming_measurement(self):
        claim = self.claim("unit-1", token_reservation=5, lease_seconds=1)
        self.store.recover_expired_leases(at=stage3.NOW + timedelta(seconds=2))
        with self.store._connection(write=False) as connection:
            row = connection.execute("SELECT * FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
        self.assertEqual(row["tokens_consumed"], 5)
        self.assertEqual(row["token_accounting_source"], "unavailable")
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_tokens"], 5)

    def test_exact_measured_receipts_can_coexist_with_overrun_attestation(self):
        from tests.test_codex_runs_runtime import CodexRunRuntimeTests

        fixture = CodexRunRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        run = fixture.prepare("measured-overrun")
        fixture.store.record_codex_start(
            run_id=run["run_id"], observer_id="worker", host_canonical_name=run["requested_task_name"],
        )
        fixture.store.record_codex_finish(
            run_id=run["run_id"], observer_id="worker", outcome="completed", result_status="observed",
            result_sha256="a" * 64, usage_status="measured", input_tokens=3, output_tokens=5,
        )
        result = fixture.store.finish_attempt(
            attempt_id=fixture.attempt, performer_id="worker", lease_token=fixture.token,
            outcome="exhausted", tokens_consumed=8, accounting_source="host-measured",
            observed_token_overrun=True,
            observed_usage_evidence={"source": "coordinator-attested", "execution_ids": ["execution-one"], "total_tokens": 8},
        )
        self.assertEqual((result["token_accounting_source"], result["tokens_consumed"]), ("host-measured", 8))
        self.assertTrue(fixture.store.verify_audit()["ok"])

    def test_emergency_stop_attestation_follows_global_and_goal_event_order(self):
        self.store.create_goal(goal_id="untouched", title="Untouched", description="Planned")
        claim = self.claim("unit-1", token_reservation=5)
        self.store.set_emergency_stop(actor_id="owner", reason="Stop", at=stage3.NOW)
        self.assertTrue(self.store.verify_audit()["ok"])
        self.assertEqual(self.store.get_goal("untouched")["status"], "planned")
        with self.store._connection(write=False) as connection:
            attempt = connection.execute("SELECT tokens_consumed,token_accounting_source FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
        self.assertEqual(tuple(attempt), (5, "unavailable"))
        self.store.clear_emergency_stop(actor_id="owner", approver_kind="human", at=stage3.NOW)
        self.assertEqual(self.store.get_goal("goal-1")["status"], "paused")
        self.assertTrue(self.store.verify_audit()["ok"])
        self.store.record_transition_approval(
            goal_id="goal-1", action="goal-resume", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
        )
        self.store.resume_goal("goal-1", actor_id="owner", envelope_sha256=self.digest, at=stage3.NOW)
        self.assertTrue(self.store.verify_audit()["ok"])
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE goals SET status='paused' WHERE id='goal-1'")
        audit = self.store.verify_audit()
        self.assertFalse(audit["ok"])
        self.assertTrue(any("matching lifecycle evidence" in issue for issue in audit["issues"]))

    def test_immediate_pause_and_stop_charge_without_leaving_pending_measurement(self):
        for action in ("pause", "stop"):
            with self.subTest(action=action):
                fixture = stage3.AutonomyTests()
                fixture.setUp()
                self.addCleanup(fixture.tearDown)
                claim = fixture.store.claim_next_work(
                    goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest,
                    repository="repo", revision="rev", branch="main", workspace="work",
                    token_reservation=5, at=stage3.NOW,
                )
                getattr(fixture.store, action + "_goal")("goal-1", actor_id="owner", at=stage3.NOW)
                with fixture.store._connection(write=False) as connection:
                    row = connection.execute("SELECT tokens_consumed,token_accounting_source FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
                self.assertEqual(tuple(row), (5, "unavailable"))
                self.assertTrue(fixture.store.verify_audit()["ok"])
