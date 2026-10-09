"""Recovery observations preserve lifecycle authority and scheduler capacity."""

from contextlib import closing, contextmanager
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.execution_recovery import reconcile_codex_run, unresolved_codex_runs
from tasktra.operator_cockpit import capture_operator_cockpit
from tasktra.overview import _overview_in_transaction, format_overview, orchestration_overview
from tasktra.scheduling import preview_schedule
from tasktra.state import StateError
from tasktra import overview as overview_module
from tests import test_codex_runs_capacity as capacity_fixture


NOW = capacity_fixture.NOW


@contextmanager
def fixture():
    case = capacity_fixture.CodexCapacityTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def iso(at):
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


def observation(run, *, kind="completed", at=NOW):
    name = "/root/" + run["requested_task_name"]
    return {
        "kind": "tasktra.codex-host-tree-observation", "version": 1,
        "source": "collaboration.list_agents", "captured_at": iso(at),
        "parent_canonical_name": "/root", "observed_agent_names": [name],
        "target": {"canonical_name": name, "agent_id": None,
                   "status": {"kind": kind, "source_shape": "completed-object" if kind == "completed" else "running-string"}},
    }


def reconcile(case, run, *, kind="completed", at=NOW):
    return reconcile_codex_run(
        case.store, run_id=run["run_id"], observer_id="recovery-operator",
        observation=observation(run, kind=kind, at=at),
        result_bytes=b"Completed fixture work.\r\n" if kind == "completed" else None, at=at,
    )


def public_view(case, *, at=NOW, goal_id="goal-one"):
    with patch("tasktra.overview._now", return_value=iso(at)):
        return orchestration_overview(case.store, goal_id=goal_id)


def lifecycle_rows(store):
    with store._readonly_connection() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in (
                ("goals", "id"), ("work_units", "id"), ("work_attempts", "id"),
                ("budgets", "goal_id"), ("runtime_control", "id"), ("transition_approvals", "id"),
            )
        }


class ExecutionRecoveryIntegrationTests(unittest.TestCase):
    def prepare(self, case, *, other_unit=True):
        case.add_unit("unit-one")
        if other_unit:
            case.add_unit("unit-two")
        claim = case.claim()
        return claim, case.prepare(claim, "recovery-integration")

    def detach(self, case, claim, *, at=NOW):
        case.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker",
                                  lease_token=case.token, outcome="blocked", tokens_consumed=None, at=at)

    def schedule(self, case):
        return preview_schedule(
            case.store, goal_id="goal-one", work_unit_id="unit-two",
            envelope_sha256=case.digests["goal-one"], checkpoint_id=None,
            cadence="manual fixture", notification_intent="none", performer_id="worker",
            repository="fixture", revision="revision", branch="main", workspace="workspace",
            lease_seconds=30, token_reservation=0,
        )["invocation"]

    def assert_capacity(self, case, *, leased, detached, at):
        report = public_view(case, at=at)
        capacity = report["goal"]["execution_capacity"]
        self.assertEqual(capacity, {
            "active_leases": leased, "detached_unresolved_runs": detached,
            "effective_occupancy": leased + detached, "maximum": 1,
        })
        self.assertEqual(report["aggregates"]["codex_runs"]["detached_capacity"], detached)
        explained = case.explain(at)["goal"]
        self.assertEqual((explained["active_leases"], explained["effective_occupancy"]),
                         (leased, leased + detached))
        return report

    def test_detached_running_then_completed_agrees_with_all_capacity_gates(self):
        with fixture() as case:
            claim, run = self.prepare(case)
            self.detach(case, claim, at=NOW + timedelta(seconds=1))
            invocation = self.schedule(case)
            for stage, second in (("prepared", 2), ("running", 3)):
                with self.subTest(stage=stage):
                    at = NOW + timedelta(seconds=second)
                    if stage == "running":
                        reconcile(case, run, kind="running", at=at)
                    report = self.assert_capacity(case, leased=0, detached=1, at=at)
                    self.assertEqual(report["goal"]["codex_runs"]["unresolved"], 1)
                    self.assertEqual(len(unresolved_codex_runs(case.store, at=at)["items"]), 1)
                    self.assertIsNone(case.claim(AutonomyStore(case.path), at=at))
                    with self.assertRaisesRegex(AutonomyError, "budget|concurrency"):
                        AutonomyStore(case.path).claim_scheduled_work(invocation=invocation, lease_token=case.token, at=at)
            before = lifecycle_rows(case.store)
            reconcile(case, run, at=NOW + timedelta(seconds=4))
            self.assertEqual(lifecycle_rows(case.store), before)
            report = self.assert_capacity(case, leased=0, detached=0, at=NOW + timedelta(seconds=5))
            self.assertEqual(report["goal"]["codex_runs"]["unresolved"], 0)
            self.assertEqual(unresolved_codex_runs(case.store)["items"], [])
            self.assertEqual(case.store.get_work_unit("unit-one")["status"], "blocked")
            self.assertEqual(case.explain(NOW + timedelta(seconds=5))["selected_work_unit_id"], "unit-two")
            selected = AutonomyStore(case.path).claim_scheduled_work(
                invocation=invocation, lease_token=case.token, at=NOW + timedelta(seconds=5))
            self.assertEqual(selected["work_unit_id"], "unit-two")
            self.assertTrue(case.store.verify_audit()["ok"])

    def test_live_parent_completion_keeps_its_lease_slot(self):
        with fixture() as case:
            _, run = self.prepare(case)
            reconcile(case, run, at=NOW + timedelta(seconds=1))
            report = self.assert_capacity(case, leased=1, detached=0, at=NOW + timedelta(seconds=2))
            self.assertEqual(report["goal"]["leases"]["live"], 1)
            self.assertEqual(report["goal"]["codex_runs"]["unresolved"], 0)
            self.assertIsNone(case.claim(at=NOW + timedelta(seconds=2)))

    def test_expiry_alone_does_not_change_capacity_and_completion_never_recovers_parent(self):
        for completion_before_recovery in (False, True):
            with self.subTest(completion_before_recovery=completion_before_recovery), fixture() as case:
                claim, run = self.prepare(case)
                at = NOW + timedelta(seconds=31)
                report = self.assert_capacity(case, leased=1, detached=0, at=at)
                self.assertEqual(report["goal"]["leases"], {"stored_leased": 1, "live": 0, "expired": 1})
                item = unresolved_codex_runs(case.store, at=at)["items"][0]
                self.assertEqual(item["run_id"], run["run_id"])
                self.assertFalse(item["parent"]["is_live"])
                self.assertTrue(item["capacity"]["parent_lease_counted"])
                self.assertFalse(item["capacity"]["detached_goal_slot_retained"])
                if completion_before_recovery:
                    reconcile(case, run, at=at)
                    self.assert_capacity(case, leased=1, detached=0, at=at)
                    self.assertEqual(case.store.get_work_unit("unit-one")["status"], "leased")
                recovered = case.store.recover_expired_leases(at=at)
                self.assertEqual(recovered, [claim["attempt_id"]])
                self.assert_capacity(case, leased=0, detached=0 if completion_before_recovery else 1, at=at)
                self.assertEqual(case.store.get_work_unit("unit-one")["status"], "blocked")
                if not completion_before_recovery:
                    reconcile(case, run, at=at)
                    self.assert_capacity(case, leased=0, detached=0, at=at)

    def test_observations_under_every_control_state_preserve_lifecycle_rows(self):
        for state in ("active", "draining", "paused", "stopped", "emergency-stopped"):
            with self.subTest(state=state), fixture() as case:
                _, run = self.prepare(case, other_unit=False)
                at = NOW + timedelta(seconds=1)
                if state == "draining":
                    case.store.drain_goal("goal-one", actor_id="operator", at=at)
                elif state == "paused":
                    case.store.pause_goal("goal-one", actor_id="operator", at=at)
                elif state == "stopped":
                    case.store.stop_goal("goal-one", actor_id="operator", at=at)
                elif state == "emergency-stopped":
                    case.store.set_emergency_stop(actor_id="operator", reason="Fixture stop", at=at)
                before = lifecycle_rows(case.store)
                reconcile(case, run, kind="running", at=at)
                reconcile(case, run, at=at)
                self.assertEqual(lifecycle_rows(case.store), before)
                self.assertEqual(case.store.get_codex_run(run["run_id"])["result"]["outcome"], "completed")
                self.assertTrue(case.store.verify_audit()["ok"])

    def test_parent_stays_blocked_until_separately_approved_requeue(self):
        with fixture() as case:
            claim, run = self.prepare(case, other_unit=False)
            self.detach(case, claim, at=NOW + timedelta(seconds=1))
            arguments = dict(work_unit_id="unit-one", performer_id="worker",
                             envelope_sha256=case.digests["goal-one"], evidence={"review": "Completed result reviewed."},
                             at=NOW + timedelta(seconds=3))
            with self.assertRaisesRegex(AutonomyError, "all prior Codex runs"):
                case.store.requeue_work(**arguments)
            reconcile(case, run, at=NOW + timedelta(seconds=2))
            with self.assertRaisesRegex(AutonomyError, "approval"):
                case.store.requeue_work(**arguments)
            self.assertEqual(case.store.get_work_unit("unit-one")["status"], "blocked")
            case.store.record_transition_approval(
                goal_id="goal-one", work_unit_id="unit-one", action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=case.digests["goal-one"], approver_id="different-steward", approver_kind="steward",
                performer_id="worker", valid_until=NOW + timedelta(days=1), at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(case.store.requeue_work(**arguments)["status"], "eligible")
            self.assertEqual(case.store.get_work_unit("unit-one")["attempt_count"], 1)

    def test_public_attention_and_text_leave_cockpit_base_contract_unchanged(self):
        with fixture() as case:
            claim, run = self.prepare(case)
            self.detach(case, claim, at=NOW + timedelta(seconds=1))
            report = public_view(case)
            self.assertIn("codex-run-prepared-unobserved", [item["code"] for item in report["goal"]["attention"]])
            self.assertTrue(any(item.get("argv", [])[:3] == ["tasktra", "delegation", "unresolved"]
                                for item in report["goal"]["recommendations"]))
            text = format_overview(report)
            self.assertIn("1 unresolved", text)
            self.assertIn("execution capacity: 1/1, 0 stored leases, 1 detached workers", text)
            with case.store._readonly_connection() as connection:
                connection.create_function("tasktra_lease_elapsed", 2, overview_module._lease_elapsed)
                base = _overview_in_transaction(connection, capture_timestamp=iso(NOW), goal_id="goal-one", limit=20, offset=0)
            self.assertNotIn("codex_runs", base["aggregates"])
            self.assertNotIn("codex_runs", base["goal"])
            self.assertNotIn("execution_capacity", base["goal"])
            cockpit = capture_operator_cockpit(case.store, project_root=case.path.parent,
                                               project_name="Fixture", source_provenance={}, at=NOW)
            self.assertEqual(cockpit["version"], 1)
            self.assertNotIn("codex_runs", cockpit["aggregates"])
            self.assertNotIn("execution_capacity", cockpit["goals"][0])
            reconcile(case, run, kind="running", at=NOW)
            started = public_view(case)
            self.assertIn("codex-run-started-unterminated", [item["code"] for item in started["goal"]["attention"]])

    def test_global_counts_do_not_follow_goal_filter_or_page(self):
        with fixture() as case:
            self.prepare(case, other_unit=False)
            case.seed_goal("goal-two")
            report = public_view(case, goal_id="goal-two")
            self.assertEqual(report["aggregates"]["codex_runs"]["unresolved"], 1)
            self.assertEqual(report["goal"]["codex_runs"]["unresolved"], 0)
            report = orchestration_overview(case.store, limit=1, offset=1)
            self.assertEqual(report["aggregates"]["codex_runs"]["unresolved"], 1)

    def test_overview_rejects_tampering_before_projecting_receipts(self):
        for corrupt in ("receipt", "audit", "seal"):
            with self.subTest(corrupt=corrupt), fixture() as case:
                _, run = self.prepare(case)
                with closing(sqlite3.connect(case.path)) as connection:
                    if corrupt == "receipt":
                        connection.execute("DROP TRIGGER codex_run_preparations_no_update")
                        connection.execute("UPDATE codex_run_preparations SET role='tampered'")
                    elif corrupt == "audit":
                        connection.execute("DROP TRIGGER audit_events_no_update")
                        connection.execute("UPDATE audit_events SET payload='{}' WHERE event_type='codex_run.prepared'")
                    else:
                        connection.execute("DROP TRIGGER IF EXISTS authority_seals_no_update")
                        connection.execute("UPDATE authority_seals SET row_hash=? WHERE table_name='codex_run_preparations'", ("0" * 64,))
                    connection.commit()
                before = case.path.read_bytes()
                with self.assertRaises(StateError):
                    public_view(case)
                self.assertEqual(case.path.read_bytes(), before)
                with self.assertRaises(StateError):
                    unresolved_codex_runs(case.store)
                self.assertEqual(case.path.read_bytes(), before)

    def test_overview_preserves_wal_bytes_and_uses_one_snapshot_during_concurrent_completion(self):
        with fixture() as case:
            claim, run = self.prepare(case)
            self.detach(case, claim, at=NOW + timedelta(seconds=1))
            with closing(sqlite3.connect(case.path)) as keeper:
                self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                keeper.execute("BEGIN")
                keeper.execute("SELECT count(*) FROM goals").fetchone()
                case.store.create_goal(goal_id="wal-write", title="WAL", description="WAL", acceptance=["Done"])
                wal = case.path.with_name(case.path.name + "-wal")
                before = case.path.read_bytes(), wal.read_bytes()
                public_view(case)
                self.assertEqual((case.path.read_bytes(), wal.read_bytes()), before)
                original = overview_module._overview_in_transaction

                def complete_after_verified_snapshot(connection, **kwargs):
                    reconcile(case, run, at=NOW + timedelta(seconds=2))
                    return original(connection, **kwargs)

                with patch.object(overview_module, "_overview_in_transaction", side_effect=complete_after_verified_snapshot):
                    prior = public_view(case)
                self.assertEqual(prior["goal"]["codex_runs"]["unresolved"], 1)
                self.assertEqual(prior["goal"]["execution_capacity"]["detached_unresolved_runs"], 1)
                self.assertEqual(public_view(case)["goal"]["codex_runs"]["unresolved"], 0)


if __name__ == "__main__":
    unittest.main()
