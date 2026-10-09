"""Rollback and independent-store convergence checks for recovery."""
from __future__ import annotations

from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyStore
from tasktra.execution_recovery import reconcile_codex_run
from tasktra.state import StateStore
from tests.test_execution_recovery_runtime import fixture, observation, prepare


class ExecutionRecoveryAtomicityTests(unittest.TestCase):
    def test_insert_and_seal_failures_rollback_complete_reconciliation(self):
        for table in ("codex_run_starts", "codex_run_finishes", "authority_seals"):
            with self.subTest(table=table), fixture() as case:
                run = prepare(case, "recovery-trigger-" + str(("codex_run_starts", "codex_run_finishes", "authority_seals").index(table)))
                with case.store._connection() as connection:
                    connection.execute("CREATE TRIGGER recovery_fail AFTER INSERT ON " + table + " BEGIN SELECT RAISE(ABORT, 'recovery injected'); END")
                before = sha256(case.store.path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(Exception, "recovery injected"):
                    reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"done")
                self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)
                shown = case.store.get_codex_run(run["run_id"])
                self.assertEqual((shown["state"], shown["result"]["sha256"]), ("prepared", None))

    def test_seal_method_failure_rolls_back_before_commit(self):
        with fixture() as case:
            run = prepare(case, "recovery-seal-method")
            before = sha256(case.store.path.read_bytes()).hexdigest()
            with patch.object(StateStore, "_seal_current_state_in_transaction", side_effect=RuntimeError("before commit")):
                with self.assertRaisesRegex(RuntimeError, "before commit"):
                    reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"done")
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)

    def test_failure_after_seals_before_commit_rolls_back(self):
        with fixture() as case:
            run = prepare(case, "recovery-after-seals")
            before = case.store.path.read_bytes()
            original = StateStore._seal_current_state_in_transaction

            def fail_after_seals(connection, timestamp):
                original(connection, timestamp)
                raise RuntimeError("after seals before commit")

            with patch.object(StateStore, "_seal_current_state_in_transaction", side_effect=fail_after_seals):
                with self.assertRaisesRegex(RuntimeError, "after seals before commit"):
                    reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"done")
            self.assertEqual(case.store.path.read_bytes(), before)
            self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], "prepared")

    def test_append_failure_rolls_back_started_and_finished_receipts(self):
        for failure_position in (1, 2):
            with self.subTest(failure_position=failure_position), fixture() as case:
                run = prepare(case, "recovery-atomic")
                before = sha256(case.store.path.read_bytes()).hexdigest()
                original = case.store._append
                calls = []

                def fail_append(connection, event_type, **kwargs):
                    calls.append(event_type)
                    original(connection, event_type, **kwargs)
                    if len(calls) == failure_position:
                        raise RuntimeError("after receipt audit")

                with patch.object(case.store, "_append", side_effect=fail_append):
                    with self.assertRaisesRegex(RuntimeError, "after receipt audit"):
                        reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"done")
                self.assertEqual(len(calls), failure_position)
                self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)
                self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], "prepared")

    def test_two_stores_exact_completed_converge(self):
        with fixture() as case:
            run = prepare(case, "recovery-race")
            other = AutonomyStore(case.store.path)
            first = reconcile_codex_run(case.store, run["run_id"], "one", observation(run, "completed"), b"done")
            second = reconcile_codex_run(other, run["run_id"], "two", observation(run, "completed"), b"done")
            self.assertEqual((first["state"], second["mutation"]), ("finished", "none"))

    def test_two_stores_concurrent_exact_completed_converge(self):
        with fixture() as case:
            run = prepare(case, "recovery-concurrent-race")
            barrier = Barrier(2)
            def call(actor):
                store = AutonomyStore(case.store.path)
                barrier.wait()
                return reconcile_codex_run(store, run["run_id"], actor, observation(run, "completed"), b"done")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(call, ("one", "two")))
            self.assertEqual({item["state"] for item in results}, {"finished"})
            self.assertEqual(sorted(item["mutation"] for item in results), ["applied", "none"])
            shown = case.store.get_codex_run(run["run_id"])
            self.assertEqual((shown["state"], shown["result"]["sha256"]), ("finished", sha256(b"done").hexdigest()))
