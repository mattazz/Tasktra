"""Drain behavior at scheduler and concurrent writer boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.scheduling import preview_schedule
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints


class GatedStore(AutonomyStore):
    """Hold the first writer after SQLite has serialized its transaction."""

    first_thread = None

    def _prepare_write(self, connection):
        super()._prepare_write(connection)
        if get_ident() == self.first_thread:
            self.first_thread = None
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("Concurrent writer test did not release its gate")


class GoalDrainingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stage3.AutonomyTests()
        self.fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(self.fixture.tearDown)
        self.store = GatedStore(self.fixture.store.path)
        self.digest = self.fixture.digest
        for action in ("work-claim", "work-complete"):
            self.store.record_transition_approval(
                goal_id="goal-1", action=action, effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=self.digest, approver_id="steward", approver_kind="steward",
                performer_id="worker", valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
            )

    def claim(self):
        return self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work",
            token_reservation=3, lease_seconds=30, at=stage3.NOW,
        )

    def drain(self):
        return self.store.drain_goal("goal-1", actor_id="operator", at=stage3.NOW)

    def finish(self, claim):
        return self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="success", workflow=checkpoints.complete_workflow("goal-1", claim["work_unit_id"]),
            at=stage3.NOW + timedelta(seconds=1),
        )

    def ordered_writers(self, first, second):
        self.store.entered, self.store.release = Event(), Event()
        attempted = Event()

        def run_first():
            self.store.first_thread = get_ident()
            return first()

        def run_second():
            attempted.set()
            try:
                return second()
            except AutonomyError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            earlier = pool.submit(run_first)
            try:
                self.assertTrue(self.store.entered.wait(5), "First writer never acquired transaction")
                later = pool.submit(run_second)
                self.assertTrue(attempted.wait(5), "Second writer never started")
            finally:
                self.store.release.set()
            return earlier.result(timeout=10), later.result(timeout=10)

    def test_claim_then_drain_preserves_the_new_lease(self):
        claim, drained = self.ordered_writers(self.claim, self.drain)
        self.assertEqual(drained["status_after"], "draining")
        self.assertEqual(drained["stored_leases"], 1)
        self.assertEqual(self.store.get_work_unit("unit-1")["current_attempt_id"], claim["attempt_id"])
        self.finish(claim)
        self.assertEqual(self.store.get_goal("goal-1")["status"], "paused")
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_drain_then_claim_prevents_a_new_attempt(self):
        drained, rejected = self.ordered_writers(self.drain, self.claim)
        self.assertEqual(drained["status_after"], "paused")
        self.assertIsInstance(rejected, AutonomyError)
        self.assertIsNone(self.store.get_work_unit("unit-1")["current_attempt_id"])
        self.assertEqual(self.store.get_work_unit("unit-1")["attempt_count"], 0)
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_finish_then_drain_pauses_without_replacing_completion(self):
        claim = self.claim()
        self.ordered_writers(lambda: self.finish(claim), self.drain)
        self.assertEqual(self.store.get_goal("goal-1")["status"], "paused")
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "complete")
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_drain_then_finish_allows_verified_completion(self):
        claim = self.claim()
        _, finished = self.ordered_writers(self.drain, lambda: self.finish(claim))
        self.assertNotIsInstance(finished, Exception)
        self.assertEqual(self.store.get_goal("goal-1")["status"], "paused")
        self.assertEqual(self.store.get_work_unit("unit-1")["status"], "complete")
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_scheduling_preview_remains_read_only_but_execution_is_blocked(self):
        self.claim()
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-2", title="Next", scope={"paths": ["src"], "exclusions": []})
        self.drain()
        before = self.store.path.read_bytes()
        schedule = preview_schedule(
            self.store, goal_id="goal-1", work_unit_id="unit-2", envelope_sha256=self.digest,
            checkpoint_id=None, cadence="manual", notification_intent="none", performer_id="worker",
            repository="repo", revision="abc", branch="main", workspace="work", lease_seconds=30, token_reservation=3,
        )
        with self.assertRaisesRegex(AutonomyError, "not active"):
            self.store.claim_scheduled_work(invocation=schedule["invocation"], lease_token="test-secret-" * 4, at=stage3.NOW)
        report = self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, at=stage3.NOW)
        self.assertIsNone(report["selected_work_unit_id"])
        self.assertIn("goal.intake_draining", report["goal"]["reason_codes"])
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
