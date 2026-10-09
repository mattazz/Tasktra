"""Behavior across work prerequisites, selection, completion, and scheduling."""

import copy
from datetime import timedelta
import unittest

from tasktra.autonomy import AutonomyError, LOCAL_REVERSIBLE_WRITE
from tasktra.scheduling import SchedulerError, _idempotency_key, preview_schedule
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints


class DependencyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stage3.AutonomyTests()
        self.fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(self.fixture.tearDown)
        self.store, self.digest = self.fixture.store, self.fixture.digest
        for action in ("work-claim", "work-complete"):
            self.store.record_transition_approval(
                goal_id="goal-1", action=action, effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=self.digest, approver_id="graph-steward",
                approver_kind="steward", performer_id="worker",
                valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
            )

    def unit(self, identifier, prerequisites):
        return self.store.create_work_unit(
            goal_id="goal-1", title=identifier, work_unit_id=identifier,
            scope={"paths": ["src/tasktra"], "exclusions": []},
            prerequisite_ids=prerequisites,
        )

    def explain(self, seconds=0, **extra):
        return self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            at=stage3.NOW + timedelta(seconds=seconds), lease_seconds=10, **extra,
        )

    def claim(self, seconds=0):
        return self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            at=stage3.NOW + timedelta(seconds=seconds), lease_seconds=10,
            repository="repo", revision="revision", branch="main", workspace="workspace",
        )

    def finish(self, claim, seconds):
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="success", workflow=checkpoints.complete_workflow("goal-1", claim["work_unit_id"]),
            at=stage3.NOW + timedelta(seconds=seconds),
        )

    def test_fan_out_and_fan_in_follow_verified_completion(self):
        self.unit("unit-2b", ["unit-1"])
        self.unit("unit-2c", ["unit-1"])
        self.unit("unit-3d", ["unit-2b", "unit-2c"])
        initial = self.explain()
        self.assertEqual(initial["selected_work_unit_id"], "unit-1")
        self.assertTrue(all("candidate.prerequisites_incomplete" in unit["reason_codes"] for unit in initial["candidates"][1:]))
        parent = self.claim()
        self.assertEqual(parent["work_unit_id"], initial["selected_work_unit_id"])
        self.finish(parent, 1)
        first_branch = self.claim(1)
        second_branch = self.claim(1)
        self.assertEqual({first_branch["work_unit_id"], second_branch["work_unit_id"]}, {"unit-2b", "unit-2c"})
        self.finish(first_branch, 2)
        waiting = self.explain(2, work_unit_id="unit-3d")
        self.assertIsNone(waiting["selected_work_unit_id"])
        self.assertIn("candidate.prerequisites_incomplete", waiting["candidates"][0]["reason_codes"])
        self.finish(second_branch, 2)
        ready = self.explain(2, limit=1)
        self.assertEqual(ready["selected_work_unit_id"], "unit-3d")
        joined = self.claim(2)
        self.assertEqual(joined["work_unit_id"], ready["selected_work_unit_id"])
        self.finish(joined, 3)
        self.assertTrue(self.store.verify_audit()["ok"])

    def schedule(self, unit):
        return preview_schedule(
            self.store, goal_id="goal-1", work_unit_id=unit,
            envelope_sha256=self.digest, checkpoint_id=None, cadence="manual",
            notification_intent="none", performer_id="worker", repository="repo",
            revision="revision", branch="main", workspace="workspace",
            lease_seconds=10, token_reservation=0,
        )

    def test_schedule_preview_and_exact_resume_both_reject_blocked_unit(self):
        self.unit("unit-blocked", ["unit-1"])
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(SchedulerError, "prerequisites are incomplete"):
            self.schedule("unit-blocked")
        # Invocation data is untrusted and can bypass the preview UI. Even a
        # correctly shaped/hash-bound invocation must recheck prerequisites.
        invocation = copy.deepcopy(self.schedule("unit-1")["invocation"])
        invocation["work_unit_id"] = "unit-blocked"
        invocation["idempotency_key"] = _idempotency_key(invocation)
        with self.assertRaisesRegex(AutonomyError, "prerequisites are incomplete"):
            self.store.claim_scheduled_work(invocation=invocation, lease_token="test-secret-" * 4, at=stage3.NOW)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNone(self.store.get_work_unit("unit-blocked")["current_attempt_id"])
        parent = self.claim()
        self.finish(parent, 1)
        accepted = self.store.claim_scheduled_work(
            invocation=self.schedule("unit-blocked")["invocation"],
            lease_token="another-test-secret-" * 3, at=stage3.NOW + timedelta(seconds=1),
        )
        self.assertEqual(accepted["work_unit_id"], "unit-blocked")

    def test_failed_prerequisite_does_not_release_dependent_work(self):
        self.unit("unit-dependent", ["unit-1"])
        parent = self.claim()
        self.store.finish_attempt(
            attempt_id=parent["attempt_id"], performer_id="worker",
            lease_token=parent["lease_token"], outcome="permanent", at=stage3.NOW,
        )
        result = self.explain(work_unit_id="unit-dependent")
        self.assertIsNone(result["selected_work_unit_id"])
        self.assertIn("candidate.prerequisites_incomplete", result["candidates"][0]["reason_codes"])
        self.assertIsNone(self.claim())

    def test_exhaustion_prefix_is_preserved_even_with_blocked_prerequisites(self):
        self.unit("a-exhausted", ["unit-1"])
        self.unit("zz-exhausted", ["unit-1"])
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE work_units SET attempt_count=10 WHERE id IN ('a-exhausted','zz-exhausted')")
        before = self.store.path.read_bytes()
        preview = self.explain()
        self.assertEqual(preview["selected_work_unit_id"], "unit-1")
        self.assertEqual(self.store.path.read_bytes(), before)
        claim = self.claim()
        self.assertEqual(claim["work_unit_id"], "unit-1")
        self.assertEqual(self.store.get_work_unit("a-exhausted")["status"], "exhausted")
        self.assertEqual(self.store.get_work_unit("zz-exhausted")["status"], "planned")


if __name__ == "__main__":
    unittest.main()
