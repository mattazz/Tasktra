"""Actual two-store races and capacity retained by detached host workers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.compiler import load_catalog
from tasktra.config import ProjectConfig
from tasktra.scheduling import preview_schedule
from tests.test_stage3_autonomy import envelope


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class CodexCapacityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.store = AutonomyStore(self.path)
        self.catalog = load_catalog(ROOT / "catalog")
        self.config = ProjectConfig(name="host capacity", concurrency_limit=1)
        self.token = "capacity-fixture-lease-" + "x" * 32
        self.digests = {}
        self.seed_goal("goal-one")

    def tearDown(self):
        self.directory.cleanup()

    def seed_goal(self, goal_id):
        contract = envelope()
        contract["goal_id"] = goal_id
        contract["budgets"].update(tokens=None, attempts=10, elapsed_seconds=600, concurrency=1)
        digest = authority_envelope_sha256(contract)
        self.digests[goal_id] = digest
        self.store.create_goal(goal_id=goal_id, title="Fixture", description="Capacity fixture", acceptance=["Done."])
        self.store.define_goal_contract(goal_id, contract, actor_id="owner", at=NOW)
        self.store.record_transition_approval(
            goal_id=goal_id, action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="owner",
            valid_until=NOW + timedelta(days=1), at=NOW,
        )
        self.store.activate_goal(goal_id, actor_id="owner", envelope_sha256=digest, at=NOW)

    def add_unit(self, unit_id, goal_id="goal-one"):
        self.store.create_work_unit(goal_id=goal_id, work_unit_id=unit_id, title="Worker", scope={"paths": ["src"], "exclusions": []})
        self.store.record_transition_approval(
            goal_id=goal_id, work_unit_id=unit_id, action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digests[goal_id], approver_id="steward", approver_kind="steward",
            performer_id="worker", valid_until=NOW + timedelta(days=1), at=NOW,
        )

    def claim(self, store=None, goal_id="goal-one", at=NOW):
        return (store or self.store).claim_next_work(
            goal_id=goal_id, performer_id="worker", envelope_sha256=self.digests[goal_id],
            lease_token=self.token, lease_seconds=30, repository="fixture", revision="revision",
            branch="main", workspace="workspace", at=at,
        )

    def prepare(self, claim, key, store=None):
        request = {
            "kind": "tasktra.routing-request", "version": 1, "task_id": "capacity-probe",
            "source": {"goal_id": claim["goal_id"], "work_unit_id": claim["work_unit_id"]},
            "objective": "Inspect one bounded fixture.", "primary_signal": "inspect", "signals": ["inspect"],
            "constraints": [], "verified_facts": [], "evidence_refs": [],
        }
        return (store or self.store).prepare_codex_run(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=self.token,
            catalog=self.catalog, config=self.config, routing_request=request, idempotency_key=key, at=NOW,
        )

    def explain(self, at=NOW):
        return self.store.explain_next_work(goal_id="goal-one", performer_id="worker",
            envelope_sha256=self.digests["goal-one"], lease_seconds=30, at=at)

    def race(self, operations):
        barrier = Barrier(2)

        def run(operation):
            other = AutonomyStore(self.path)
            barrier.wait(timeout=5)
            try:
                return operation(other)
            except AutonomyError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, operation) for operation in operations]
            return [future.result(timeout=15) for future in futures]

    def test_global_capacity_is_atomic_across_goals_and_store_connections(self):
        self.add_unit("unit-one")
        first = self.claim()
        self.seed_goal("goal-two")
        self.add_unit("unit-two", "goal-two")
        second = self.claim(goal_id="goal-two")
        results = self.race([
            lambda store: self.prepare(first, "first", store),
            lambda store: self.prepare(second, "second", store),
        ])
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, AutonomyError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["launch_directive"], "invoke-once-now")
        self.assertEqual(len(failures), 1)
        self.assertIn("capacity", str(failures[0]))
        self.assertEqual(len(self.store.list_codex_runs()["items"]), 1)
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_concurrent_exact_prepare_grants_only_one_launch(self):
        self.add_unit("unit-one")
        claim = self.claim()
        results = self.race([lambda store: self.prepare(claim, "same", store)] * 2)
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(sorted(result["launch_directive"] for result in results), ["invoke-once-now", "reconcile-only"])
        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        self.assertEqual(len(self.store.list_codex_runs()["items"]), 1)

    def test_detached_worker_holds_goal_slot_until_late_terminal_receipt(self):
        self.add_unit("unit-one")
        self.add_unit("unit-two")
        claim = self.claim()
        run = self.prepare(claim, "detached")
        live = self.explain()["goal"]
        self.assertEqual((live["active_leases"], live["effective_occupancy"]), (1, 1))
        self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=self.token,
                                  outcome="blocked", tokens_consumed=None, at=NOW + timedelta(seconds=1))
        detached = self.explain(NOW + timedelta(seconds=2))
        self.assertEqual((detached["goal"]["active_leases"], detached["goal"]["effective_occupancy"]), (0, 1))
        self.assertIsNone(detached["selected_work_unit_id"])
        other = AutonomyStore(self.path)
        self.assertIsNone(self.claim(other, at=NOW + timedelta(seconds=2)))
        invocation = preview_schedule(
            self.store, goal_id="goal-one", work_unit_id="unit-two", envelope_sha256=self.digests["goal-one"],
            checkpoint_id=None, cadence="manual fixture", notification_intent="none", performer_id="worker",
            repository="fixture", revision="revision", branch="main", workspace="workspace", lease_seconds=30,
            token_reservation=0,
        )["invocation"]
        with self.assertRaisesRegex(AutonomyError, "budget|concurrency"):
            other.claim_scheduled_work(invocation=invocation, lease_token=self.token, at=NOW + timedelta(seconds=2))
        other.record_codex_start(run_id=run["run_id"], observer_id="worker",
                                 host_canonical_name="/root/" + run["requested_task_name"], at=NOW + timedelta(seconds=3))
        other.record_codex_finish(run_id=run["run_id"], observer_id="worker", outcome="completed",
            result_status="observed", result_sha256=sha256(b"finished").hexdigest(), usage_status="unavailable",
            at=NOW + timedelta(seconds=4))
        available = self.explain(NOW + timedelta(seconds=5))
        self.assertEqual((available["goal"]["active_leases"], available["goal"]["effective_occupancy"]), (0, 0))
        self.assertEqual(available["selected_work_unit_id"], "unit-two")
        resumed = other.claim_scheduled_work(invocation=invocation, lease_token=self.token, at=NOW + timedelta(seconds=5))
        self.assertEqual(resumed["work_unit_id"], "unit-two")
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_concurrent_terminal_observations_preserve_first_result(self):
        self.add_unit("unit-one")
        run = self.prepare(self.claim(), "terminal")
        self.store.record_codex_start(run_id=run["run_id"], observer_id="worker",
                                     host_canonical_name="/root/" + run["requested_task_name"], at=NOW)

        def observe(store, payload):
            return store.record_codex_finish(run_id=run["run_id"], observer_id="worker", outcome="completed",
                result_status="observed", result_sha256=sha256(payload).hexdigest(), usage_status="unavailable", at=NOW)

        results = self.race([lambda store: observe(store, b"first"), lambda store: observe(store, b"second")])
        winner = [result for result in results if isinstance(result, dict)]
        loser = [result for result in results if isinstance(result, AutonomyError)]
        self.assertEqual((len(winner), len(loser)), (1, 1))
        self.assertIn("idempotency_conflict", str(loser[0]))
        self.assertEqual(self.store.get_codex_run(run["run_id"])["result"]["sha256"], winner[0]["result"]["sha256"])
        self.assertTrue(self.store.verify_audit()["ok"])
