"""Closed-input and privacy contracts for recovery."""
from __future__ import annotations

from hashlib import sha256
from copy import deepcopy
import unittest

from tasktra.autonomy import AutonomyError
from tasktra.execution_recovery import reconcile_codex_run
from tests.test_execution_recovery_runtime import fixture, observation, prepare


class ExecutionRecoveryContractTests(unittest.TestCase):
    def test_absent_unknown_host_states_and_inexact_names_never_write(self):
        with fixture() as case:
            run = prepare(case, "negative-host-matrix")
            valid = observation(run)
            cases = []

            def add(label, edit, result=None):
                value = deepcopy(valid)
                edit(value)
                cases.append((label, value, result))

            add("missing-target", lambda value: value.pop("target"))
            add("null-target", lambda value: value.update(target=None))
            add("host-absence", lambda value: value.update(observed_agent_names=[]))
            add("target-absent-from-tree", lambda value: value.update(observed_agent_names=["/root/unrelated"]))
            add("tool-error", lambda value: value.update(error="private-host-failure"))
            for status in ("waiting", "failed", "interrupted", "needs-attention", "unknown"):
                add(status, lambda value, status=status: value["target"].update(
                    status={"kind": status, "source_shape": status + "-string"}))
            add("arbitrary-string", lambda value: value["target"].update(status="private-host-status"))
            add("arbitrary-object", lambda value: value["target"].update(status={"private": "host-payload"}))
            add("malformed-completed", lambda value: value["target"].update(status={"kind": "completed"}))
            add("completed-missing-text", lambda value: value["target"].update(
                status={"kind": "completed", "source_shape": "completed-object"}))
            add("inconsistent-parent", lambda value: value.update(parent_canonical_name="/another"))
            add("duplicate-full-name", lambda value: value.update(observed_agent_names=value["observed_agent_names"] * 2))
            for label, name in (("leaf-only", run["requested_task_name"]),
                                ("suffix-only", "/" + run["requested_task_name"]),
                                ("wrong-leaf", "/root/different_worker")):
                def replace_name(value, name=name):
                    value["target"]["canonical_name"] = name
                    value["observed_agent_names"] = [name]
                add(label, replace_name)
            before = case.store.path.read_bytes()
            for label, value, result in cases:
                with self.subTest(case=label):
                    with self.assertRaises(AutonomyError) as raised:
                        reconcile_codex_run(case.store, run["run_id"], "observer", value, result)
                    self.assertNotIn("private-host", str(raised.exception))
                    self.assertEqual(case.store.path.read_bytes(), before)
                    self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], "prepared")

    def test_observation_utc_overflow_is_a_safe_error(self):
        with fixture() as case:
            run = prepare(case, "observation-time-overflow")
            before = case.store.path.read_bytes()
            for timestamp in ("0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"):
                with self.subTest(timestamp=timestamp):
                    bad = observation(run)
                    bad["captured_at"] = timestamp
                    with self.assertRaisesRegex(AutonomyError, "captured_at is invalid"):
                        reconcile_codex_run(case.store, run["run_id"], "observer", bad)
                    self.assertEqual(case.store.path.read_bytes(), before)

    def test_direct_nested_observation_is_bounded_and_nonmutating(self):
        with fixture() as case:
            run = prepare(case, "nested-direct-api")
            nested = "private-nested-marker"
            for _ in range(1500):
                nested = {"nested": nested}
            before = case.store.path.read_bytes()
            with self.assertRaises(AutonomyError) as raised:
                reconcile_codex_run(case.store, run["run_id"], "observer", nested)
            self.assertNotIn("private-nested-marker", str(raised.exception))
            self.assertEqual(case.store.path.read_bytes(), before)

    def test_cross_actor_running_retry_preserves_first_start_attribution(self):
        with fixture() as case:
            run = prepare(case, "recovery-cross-actor")
            first = reconcile_codex_run(case.store, run["run_id"], "first", observation(run))
            second = reconcile_codex_run(case.store, run["run_id"], "second", observation(run))
            self.assertEqual((first["actual"]["canonical_name"], second["mutation"], second["idempotent"]),
                             ("/root/" + run["requested_task_name"], "none", True))
            with case.store._readonly_connection() as connection:
                self.assertEqual(connection.execute("SELECT observed_by FROM codex_run_starts WHERE run_id=?", (run["run_id"],)).fetchone()[0], "first")

    def test_rejects_nonnull_agent_id_and_token_without_writes(self):
        with fixture() as case:
            run = prepare(case, "recovery-contract")
            before = sha256(case.store.path.read_bytes()).hexdigest()
            invalid = observation(run); invalid["target"]["agent_id"] = "invented"
            with self.assertRaises(AutonomyError): reconcile_codex_run(case.store, run["run_id"], "observer", invalid)
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)
            leaked = observation(run); leaked["observed_agent_names"] = ["/root/" + case.token]
            with self.assertRaises(AutonomyError): reconcile_codex_run(case.store, run["run_id"], "observer", leaked)
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)

    def test_exact_result_and_stale_running(self):
        with fixture() as case:
            run = prepare(case, "recovery-result")
            raw = b"\r\n"
            finished = reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), raw)
            self.assertEqual(finished["result"]["sha256"], sha256(raw).hexdigest())
            stale = reconcile_codex_run(case.store, run["run_id"], "other", observation(run))
            self.assertTrue(stale["ignored_stale_observation"])
            self.assertEqual(stale["result"]["sha256"], sha256(raw).hexdigest())

    def test_bad_results_and_wrong_name_do_not_write(self):
        with fixture() as case:
            run = prepare(case, "recovery-invalid")
            before = sha256(case.store.path.read_bytes()).hexdigest()
            with self.assertRaises(AutonomyError):
                reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"\xff")
            wrong = observation(run); wrong["target"]["canonical_name"] = "/root/wrong_task"; wrong["observed_agent_names"] = ["/root/wrong_task"]
            with self.assertRaises(AutonomyError): reconcile_codex_run(case.store, run["run_id"], "observer", wrong)
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)

    def test_all_existing_terminal_outcomes_ignore_stale_running_and_conflict_completed(self):
        variants = (("completed", "observed", sha256(b"old").hexdigest(), "unavailable", None, None),
                    ("failed", "unavailable", None, "measured", 2, 3),
                    ("interrupted", "unavailable", None, "unavailable", None, None),
                    ("needs-attention", "unavailable", None, "unavailable", None, None))
        for index, supplied in enumerate(variants):
            with self.subTest(outcome=supplied[0]), fixture() as case:
                run = prepare(case, "recovery-terminal-" + str(index))
                name = "/root/" + run["requested_task_name"]
                case.store.record_codex_start(run_id=run["run_id"], observer_id="direct", host_canonical_name=name)
                case.store.record_codex_finish(run_id=run["run_id"], observer_id="direct", outcome=supplied[0],
                                               result_status=supplied[1], result_sha256=supplied[2], usage_status=supplied[3],
                                               input_tokens=supplied[4], output_tokens=supplied[5])
                stale = reconcile_codex_run(case.store, run["run_id"], "observer", observation(run))
                self.assertTrue(stale["ignored_stale_observation"])
                with self.assertRaisesRegex(AutonomyError, "idempotency_conflict"):
                    reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"new")

    def test_exact_64k_and_overbound_results_are_private(self):
        with fixture() as case:
            run = prepare(case, "recovery-bound")
            payload = ("é" * 32768).encode("utf-8")
            self.assertEqual(len(payload), 65536)
            done = reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), payload)
            self.assertEqual(done["result"]["sha256"], sha256(payload).hexdigest())
            raw = case.store.path.read_bytes()
            self.assertNotIn(payload, raw)
        with fixture() as case:
            run = prepare(case, "recovery-overbound")
            before = sha256(case.store.path.read_bytes()).hexdigest()
            with self.assertRaises(AutonomyError):
                reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), b"x" * 65537)
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)

    def test_closed_types_and_token_fields_fail_without_raw_echo(self):
        for field, value in (("version", 1.0), ("version", True), ("kind", [])):
            with self.subTest(field=field, value=repr(value)), fixture() as case:
                run = prepare(case, "recovery-types-" + field + str(type(value).__name__))
                bad = observation(run); bad[field] = value
                with self.assertRaises(AutonomyError) as raised:
                    reconcile_codex_run(case.store, run["run_id"], "observer", bad)
                self.assertNotIn(repr(value), str(raised.exception))
        # The originating token is rejected from the actor and every
        # observation-string carrier without a durable receipt.
        for carrier in ("observer", "parent", "names", "target"):
            with self.subTest(carrier=carrier), fixture() as case:
                run = prepare(case, "recovery-token-" + carrier)
                token = case.token
                bad = observation(run)
                if carrier == "observer": actor = token
                else:
                    actor = "observer"
                    if carrier == "parent": bad["parent_canonical_name"] = "/" + token
                    if carrier == "names": bad["observed_agent_names"] = ["/" + token]
                    if carrier == "target": bad["target"]["canonical_name"] = "/" + token
                before = sha256(case.store.path.read_bytes()).hexdigest()
                with self.assertRaises(AutonomyError): reconcile_codex_run(case.store, run["run_id"], actor, bad)
                self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)
