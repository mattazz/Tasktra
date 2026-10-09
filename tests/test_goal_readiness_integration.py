"""Cross-component guarantees for the read-only goal readiness report."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import random
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.dependency_impact import dependency_impact
from tasktra.goal_readiness import goal_readiness
from tasktra import goal_readiness as readiness_module
from tasktra.state import StateError, StateStore
from tests import test_codex_runs_capacity as capacity_fixture
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints
from tests import test_intervention_views as intervention_fixture
from tests.test_execution_recovery_integration import reconcile


class GoalReadinessIntegrationTests(unittest.TestCase):
    @staticmethod
    def durable_files(path):
        return {str(item): item.read_bytes() for item in (path, Path(str(path) + "-wal")) if item.exists()}

    def simple_store(self, directory):
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(goal_id="goal-one", title="Readiness", description="Read-only fixture")
        store.create_work_unit(goal_id="goal-one", work_unit_id="parent", title="Parent")
        store.create_work_unit(goal_id="goal-one", work_unit_id="child", title="Child", prerequisite_ids=("parent",))
        return store

    def test_wal_reads_are_byte_stable_and_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            with closing(sqlite3.connect(store.path)) as keeper:
                self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                keeper.execute("PRAGMA wal_autocheckpoint=0")
                keeper.execute("BEGIN")
                keeper.execute("SELECT count(*) FROM goals").fetchone()
                store.create_work_unit(goal_id="goal-one", work_unit_id="independent", title="Independent")
                self.assertGreater(Path(str(store.path) + "-wal").stat().st_size, 0)
                before = self.durable_files(store.path)
                audit_before = store.verify_audit()
                first = goal_readiness(store, goal_id="goal-one", limit=2)
                second = goal_readiness(StateStore(store.path), goal_id="goal-one", limit=2)
                self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
                self.assertEqual(self.durable_files(store.path), before)
                self.assertEqual(store.verify_audit(), audit_before)
                for arguments in ({"goal_id": "absent"}, {"goal_id": "goal-one", "limit": False}):
                    with self.subTest(arguments=arguments):
                        with self.assertRaises(StateError):
                            goal_readiness(store, **arguments)
                        self.assertEqual(self.durable_files(store.path), before)

    def test_audit_seal_graph_and_old_schema_rejection_do_not_write(self):
        mutations = (
            "DROP TRIGGER audit_events_no_update; UPDATE audit_events SET payload='{}' WHERE sequence=1",
            "DROP TRIGGER authority_seals_no_update; UPDATE authority_seals SET row_hash='tampered' WHERE table_name='work_units'",
            "INSERT INTO work_unit_dependencies VALUES('parent','child')",
            "PRAGMA user_version=13",
        )
        for statement in mutations:
            with self.subTest(statement=statement), TemporaryDirectory() as directory:
                store = self.simple_store(directory)
                with closing(sqlite3.connect(store.path)) as connection:
                    connection.executescript(statement)
                    connection.commit()
                before = self.durable_files(store.path)
                with self.assertRaises(StateError):
                    goal_readiness(store, goal_id="goal-one")
                self.assertEqual(self.durable_files(store.path), before)

    def test_random_graph_frontiers_match_dependencies_and_anchored_impact(self):
        for seed in range(3):
            with self.subTest(seed=seed), TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / "state.sqlite")
                store.create_goal(goal_id="goal-one", title="Graph", description="Parity")
                generator = random.Random(seed)
                identifiers = [f"unit-{number:02d}" for number in range(16)]
                edges = {}
                for index, identifier in enumerate(identifiers):
                    prerequisites = [earlier for earlier in identifiers[:index] if generator.random() < 0.18]
                    edges[identifier] = prerequisites
                    store.create_work_unit(goal_id="goal-one", work_unit_id=identifier, title=identifier,
                                           prerequisite_ids=prerequisites)
                completed = {identifier for identifier in identifiers if generator.random() < 0.2}
                remaining = set(identifiers) - completed
                with store._connection() as connection:
                    store._prepare_write(connection)
                    for identifier in identifiers:
                        has_incomplete_child = any(identifier in edges[child] for child in remaining)
                        status = "complete" if identifier in completed else "planned" if has_incomplete_child else "blocked"
                        connection.execute("UPDATE work_units SET status=? WHERE id=?", (status, identifier))
                report = goal_readiness(store, goal_id="goal-one", limit=100)
                rows = {row["work_unit_id"]: row for name in ("ready_frontier", "blocking_frontier")
                        for row in report["frontiers"][name]["items"]}
                self.assertEqual(set(rows), remaining)
                dependency_rows = store.work_dependencies("goal-one", limit=100)["units"]
                for expected in dependency_rows:
                    identifier = expected["work_unit_id"]
                    if identifier in completed:
                        continue
                    actual = rows[identifier]
                    self.assertEqual(actual["structural_ready"], expected["ready"])
                    self.assertEqual(actual["incomplete_direct_prerequisite_ids"],
                                     [item["id"] for item in expected["prerequisites"] if item["status"] != "complete"])
                    impact = dependency_impact(store, goal_id="goal-one", work_unit_id=identifier)
                    self.assertEqual(actual["direct_prerequisite_gates_cleared_if_completed"],
                                     impact["summary"]["direct_prerequisite_gates_cleared_if_completed"])
                    remaining_gates = sum(
                        identifier in edges[child] and all(parent == identifier or parent in completed for parent in edges[child])
                        for child in remaining
                    )
                    self.assertEqual(actual["remaining_direct_prerequisite_gates_cleared_if_completed"], remaining_gates)
                self.assertEqual(sum(report["summary"]["by_category"].values()), len(identifiers))

    def test_zero_token_remaining_does_not_invent_a_selection_rejection(self):
        fixture = stage3.AutonomyTests()
        fixture._initialize({"tokens": 0, "attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(fixture.tearDown)
        report = goal_readiness(fixture.store, goal_id="goal-1")
        budget = report["operational_gates"]["budget"]
        self.assertEqual(budget["tokens"]["remaining"], 0)
        self.assertIn("tokens", budget["fully_allocated_dimensions"])
        self.assertNotIn("budget.tokens_exhausted", budget["selection_reason_codes"])
        zero = fixture.store.explain_next_work(goal_id="goal-1", performer_id="worker",
            envelope_sha256=fixture.digest, token_reservation=0, at=stage3.NOW)
        positive = fixture.store.explain_next_work(goal_id="goal-1", performer_id="worker",
            envelope_sha256=fixture.digest, token_reservation=1, at=stage3.NOW)
        self.assertNotIn("budget.tokens_exhausted", zero["goal"]["reason_codes"])
        self.assertIn("budget.tokens_exhausted", positive["goal"]["reason_codes"])
        self.assertFalse(report["claimability_evaluated"])

    def test_expired_lease_and_detached_run_capacity_match_existing_gates(self):
        fixture = capacity_fixture.CodexCapacityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.add_unit("unit-one")
        fixture.add_unit("unit-two")
        claim = fixture.claim()
        run = fixture.prepare(claim, "readiness-capacity")
        report = goal_readiness(fixture.store, goal_id="goal-one")
        capacity = report["operational_gates"]["capacity"]
        self.assertEqual((capacity["stored_leased_attempts"], capacity["detached_unresolved_runs"]), (1, 0))
        self.assertEqual(report["operational_gates"]["budget"]["elapsed_ms"]["reserved_held"], 30000)
        after_expiry = capacity_fixture.NOW + timedelta(seconds=31)
        explained = fixture.explain(after_expiry)
        self.assertEqual(capacity["effective_occupancy"], explained["goal"]["effective_occupancy"])
        self.assertIn("budget.concurrency_exhausted", capacity["selection_reason_codes"])
        fixture.store.recover_expired_leases(at=after_expiry)
        detached = goal_readiness(fixture.store, goal_id="goal-one")["operational_gates"]
        self.assertEqual((detached["capacity"]["stored_leased_attempts"], detached["capacity"]["detached_unresolved_runs"]), (0, 1))
        self.assertEqual(detached["budget"]["elapsed_ms"]["reserved_held"], 0)
        self.assertEqual(detached["unresolved_runs"]["counts"]["unresolved"], 1)
        reconcile(fixture, run, at=after_expiry + timedelta(seconds=1))
        terminal = goal_readiness(fixture.store, goal_id="goal-one")["operational_gates"]
        self.assertEqual(terminal["capacity"]["effective_occupancy"], 0)
        self.assertEqual(terminal["unresolved_runs"]["counts"]["unresolved"], 0)

    def test_completion_during_read_cannot_mix_graph_and_capacity_snapshots(self):
        fixture = stage3.AutonomyTests()
        fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(fixture.tearDown)
        store = fixture.store
        store.create_work_unit(goal_id="goal-1", work_unit_id="dependent", title="Dependent",
                              scope={"paths": ["src/tasktra"], "exclusions": []},
                              prerequisite_ids=("unit-1",))
        store.record_transition_approval(goal_id="goal-1", action="work-complete", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=fixture.digest, approver_id="completion-steward", approver_kind="steward",
            performer_id="worker", valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW)
        claim = store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest,
            lease_seconds=10, repository="fixture", revision="revision", branch="main", workspace="workspace", at=stage3.NOW)
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        reached, release = threading.Event(), threading.Event()
        reports, failures = [], []
        original = StateStore._work_dependency_graph_in_transaction

        def controlled(connection, goal_id, **kwargs):
            graph = original(connection, goal_id, **kwargs)
            if threading.current_thread().name == "readiness-reader":
                reached.set()
                if not release.wait(timeout=15):
                    raise TimeoutError("readiness reader barrier expired")
            return graph

        def read():
            try:
                reports.append(goal_readiness(store, goal_id="goal-1"))
            except BaseException as error:
                failures.append(error)

        with patch.object(StateStore, "_work_dependency_graph_in_transaction", staticmethod(controlled)):
            thread = threading.Thread(target=read, name="readiness-reader")
            thread.start()
            try:
                self.assertTrue(reached.wait(timeout=15))
                AutonomyStore(store.path).finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker",
                    lease_token=claim["lease_token"], outcome="success", workflow=checkpoints.complete_workflow("goal-1", "unit-1"),
                    at=stage3.NOW + timedelta(seconds=1))
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(reports[0]["summary"]["by_category"]["leased"], 1)
        self.assertEqual(reports[0]["operational_gates"]["capacity"]["stored_leased_attempts"], 1)
        self.assertEqual(reports[0]["remaining_structure"]["maximum_structural_depth"], 2)
        after = goal_readiness(store, goal_id="goal-1")
        self.assertEqual(after["summary"]["by_category"]["complete"], 1)
        self.assertEqual(after["operational_gates"]["capacity"]["stored_leased_attempts"], 0)
        self.assertEqual(after["remaining_structure"]["maximum_structural_depth"], 1)

    def test_later_checkpoint_stays_structurally_ready_but_has_its_own_gate(self):
        fixture = checkpoints.CheckpointTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        report = goal_readiness(fixture.store, goal_id="goal-one")
        ready = {row["work_unit_id"]: row for row in report["frontiers"]["ready_frontier"]["items"]}
        self.assertEqual(set(ready), {"unit-first", "unit-second"})
        self.assertTrue(all(row["structural_ready"] for row in ready.values()))
        self.assertEqual(ready["unit-first"]["operational_reason_codes"], [])
        self.assertEqual(ready["unit-second"]["operational_reason_codes"], ["candidate.checkpoint_not_current"])
        self.assertEqual(report["operational_gates"]["checkpoints"]["current_checkpoint_id"], "first")
        fixture.finish_success(fixture.claim())
        after = goal_readiness(fixture.store, goal_id="goal-one")
        self.assertEqual(after["operational_gates"]["checkpoints"]["current_checkpoint_id"], "second")
        self.assertEqual(after["remaining_structure"]["checkpoints"][0]["remaining_total"], 0)
        self.assertEqual(after["frontiers"]["ready_frontier"]["items"][0]["operational_reason_codes"], [])
        with closing(sqlite3.connect(fixture.store.path)) as connection:
            connection.execute("UPDATE goal_checkpoints SET status='reached' WHERE checkpoint_id='second'")
            connection.commit()
        before = self.durable_files(fixture.store.path)
        with self.assertRaises(StateError):
            goal_readiness(fixture.store, goal_id="goal-one")
        self.assertEqual(self.durable_files(fixture.store.path), before)

    def test_execution_receipt_corruption_is_rejected_before_capacity_projection(self):
        fixture = capacity_fixture.CodexCapacityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.add_unit("unit-one")
        fixture.prepare(fixture.claim(), "readiness-tamper")
        with closing(sqlite3.connect(fixture.path)) as connection:
            connection.execute("DROP TRIGGER codex_run_preparations_no_update")
            connection.execute("UPDATE codex_run_preparations SET role='tampered'")
            connection.commit()
        before = self.durable_files(fixture.path)
        with self.assertRaises(StateError):
            goal_readiness(fixture.store, goal_id="goal-one")
        self.assertEqual(self.durable_files(fixture.path), before)

    def test_intervention_dispositions_are_attention_without_private_payloads(self):
        fixture = intervention_fixture.InterventionViewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        request = fixture.yielded("blocked-one")
        previous = None
        for disposition in ("open", "answered", "declined", "cancelled"):
            with self.subTest(disposition=disposition):
                if disposition != "open":
                    previous = fixture.answer(request, "reply-" + disposition, disposition=disposition, previous=previous)
                before = self.durable_files(fixture.store.path)
                report = goal_readiness(fixture.store, goal_id="goal-1")
                gate = report["operational_gates"]["interventions"]
                self.assertEqual(gate["counts"][disposition], 1)
                self.assertEqual(gate["attention_reason_codes"], ["intervention." + disposition])
                self.assertFalse(report["claimability_evaluated"])
                self.assertEqual(self.durable_files(fixture.store.path), before)
                encoded = json.dumps(report)
                for private in (request["prompt"], "private_fixture_name", "Full private answer"):
                    self.assertNotIn(private, encoded)

    def test_goal_dependency_is_separate_from_empty_ready_frontier(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            for identifier in ("goal-one", "upstream"):
                store.create_goal(goal_id=identifier, title=identifier, description="Dependencies", acceptance=["Done."])
            contract = checkpoints.authority("goal-one", dependencies=["upstream"])
            store.define_goal_contract("goal-one", contract, actor_id="owner", at=checkpoints.NOW)
            report = goal_readiness(store, goal_id="goal-one")
            self.assertEqual(report["operational_gates"]["goal_dependencies"], {
                "dependencies_total": 1, "incomplete_total": 1, "incomplete_ids": ["upstream"],
                "selection_reason_codes": ["goal.dependencies_incomplete"],
            })
            self.assertEqual(report["frontiers"]["ready_frontier"]["total"], 0)
            self.assertTrue(report["operational_gates"]["authority_contract"]["present"])

    def test_lifecycle_and_emergency_gates_remain_separate_from_graph_structure(self):
        fixture = stage3.AutonomyTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.store.claim_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=fixture.digest,
            lease_seconds=10, repository="fixture", revision="revision", branch="main", workspace="workspace", at=stage3.NOW)
        active = goal_readiness(fixture.store, goal_id="goal-1")
        self.assertEqual(active["operational_gates"]["goal_lifecycle"]["selection_reason_codes"], [])
        fixture.store.drain_goal("goal-1", actor_id="operator", at=stage3.NOW)
        draining = goal_readiness(fixture.store, goal_id="goal-1")
        self.assertEqual(draining["operational_gates"]["goal_lifecycle"], {
            "status": "draining", "selection_reason_codes": ["goal.intake_draining"],
        })
        for state, operation in (("paused", fixture.store.pause_goal), ("stopped", fixture.store.stop_goal)):
            operation("goal-1", actor_id="operator", at=stage3.NOW + timedelta(seconds=1))
            report = goal_readiness(fixture.store, goal_id="goal-1")
            self.assertEqual(report["operational_gates"]["goal_lifecycle"], {
                "status": state, "selection_reason_codes": ["goal.lifecycle_not_active"],
            })
            self.assertEqual(report["summary"]["structural_ready_incomplete_total"], 1)
        fixture.store.set_emergency_stop(actor_id="operator", reason="Private incident details.", at=stage3.NOW + timedelta(seconds=2))
        emergency = goal_readiness(fixture.store, goal_id="goal-1")
        self.assertEqual(emergency["operational_gates"]["emergency_stop"], {
            "active": True, "selection_reason_codes": ["runtime.emergency_stopped"],
        })
        self.assertNotIn("Private incident details", json.dumps(emergency))

    def test_ten_thousand_units_have_bounded_output_and_measured_cost(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(goal_id="goal-one", title="Large graph", description="10000-unit scale check")
            store.create_work_unit(goal_id="goal-one", work_unit_id="chain-0000", title="Chain")
            with closing(sqlite3.connect(store.path)) as connection:
                template = connection.execute("SELECT goal_id,status,scope,created_at,updated_at FROM work_units WHERE id='chain-0000'").fetchone()
                names = [f"chain-{index:04d}" for index in range(1, 6000)] + ["fan-root"] + [f"fan-{index:04d}" for index in range(3999)]
                connection.executemany("INSERT INTO work_units(id,title,goal_id,status,scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                       [(name, name, *template) for name in names])
                edges = [(f"chain-{index:04d}", f"chain-{index-1:04d}") for index in range(1, 6000)]
                edges.extend((f"fan-{index:04d}", "fan-root") for index in range(3999))
                connection.executemany("INSERT INTO work_unit_dependencies VALUES(?,?)", edges)
                connection.commit()
            store.attest_ledger(actor_id="fixture-steward")
            before = self.durable_files(store.path)
            tracemalloc.start()
            started = time.perf_counter()
            report = goal_readiness(store, goal_id="goal-one", limit=3)
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            with store._connection(write=False) as connection:
                graph = store._work_dependency_graph_in_transaction(connection, "goal-one")
            tracemalloc.start()
            synthesis_started = time.perf_counter()
            synthesis = readiness_module._synthesize("goal-one", graph, limit=3, offset=0, checkpoint_summaries=[])
            synthesis_elapsed = time.perf_counter() - synthesis_started
            _, synthesis_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(synthesis, (report["summary"], report["remaining_structure"], report["frontiers"]))
            self.assertEqual(report["summary"]["units_total"], 10000)
            self.assertEqual(report["remaining_structure"]["maximum_structural_depth"], 6000)
            self.assertEqual(report["remaining_structure"]["edges_total"], 9998)
            self.assertEqual(report["remaining_structure"]["wave_count"], 6000)
            self.assertEqual(report["frontiers"]["ready_frontier"]["total"], 2)
            self.assertEqual(report["frontiers"]["blocking_frontier"]["total"], 6000)
            row_count = sum(len(report["frontiers"][key]["items"]) for key in ("ready_frontier", "blocking_frontier"))
            row_count += len(report["remaining_structure"]["waves"]["items"])
            self.assertLessEqual(row_count, 9)
            self.assertEqual(report["frontiers"]["blocking_frontier"]["items"][0]["work_unit_id"], "fan-root")
            self.assertEqual(self.durable_files(store.path), before)
            print(json.dumps({"readiness_scale": {"units": 10000, "edges": 9998, "public_call_seconds": round(elapsed, 4),
                                                  "python_peak_bytes": peak, "returned_rows": row_count,
                                                  "synthesis_seconds": round(synthesis_elapsed, 4),
                                                  "synthesis_python_peak_bytes": synthesis_peak,
                                                  "includes_full_ledger_verification": True}}, sort_keys=True))

    def test_synthesis_comparisons_are_bounded_and_drilldowns_follow_paging(self):
        class CountedIdentifier(str):
            comparisons = 0
            __hash__ = str.__hash__

            def __eq__(self, other):
                type(self).comparisons += 1
                return str.__eq__(self, other)

        count = 2000
        names = [CountedIdentifier(f"unit-{index:04d}") for index in range(count)]
        graph = {
            name: {"work_unit_id": name, "status": "planned", "checkpoint_id": None,
                   "ready": index == 0,
                   "prerequisites": [] if index == 0 else [{"id": names[index - 1], "status": "planned", "checkpoint_id": None}]}
            for index, name in enumerate(names)
        }
        original = readiness_module._drilldowns
        with patch.object(readiness_module, "_drilldowns", wraps=original) as drilldowns:
            summary, structure, frontiers = readiness_module._synthesize(
                "goal-one", graph, limit=3, offset=0, checkpoint_summaries=[])
        self.assertEqual(summary["units_total"], count)
        self.assertEqual(structure["maximum_structural_depth"], count)
        returned = sum(len(frontiers[name]["items"]) for name in ("ready_frontier", "blocking_frontier"))
        self.assertLessEqual(drilldowns.call_count, returned)
        self.assertLessEqual(CountedIdentifier.comparisons, count * 50,
                             "Repeated linear membership scans make synthesis quadratic")


if __name__ == "__main__":
    unittest.main()
