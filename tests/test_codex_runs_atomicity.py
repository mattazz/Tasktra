"""Failed receipt transitions leave no launch slot or partial host observation."""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError
from tasktra.compiler import load_catalog
from tasktra.config import load_project_config
from tests import test_codex_runs_cli as fixture_module


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fixture():
    case = fixture_module.CodexRunCliTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


class InjectedReceiptFailure(RuntimeError):
    pass


class CodexRunAtomicityTests(unittest.TestCase):
    @staticmethod
    def prepare(case):
        return case.store.prepare_codex_run(
            attempt_id=case.claim["attempt_id"], performer_id="worker", lease_token=case.token,
            catalog=load_catalog(ROOT / "catalog"), config=load_project_config(case.root),
            routing_request=json.loads(case.request.read_text(encoding="utf-8")), idempotency_key="atomic-run",
        )

    @staticmethod
    def start(case, run):
        return case.store.record_codex_start(run_id=run["run_id"], observer_id="worker",
                                            host_canonical_name="/root/" + run["requested_task_name"])

    @staticmethod
    def finish(case, run):
        return case.store.record_codex_finish(
            run_id=run["run_id"], observer_id="worker", outcome="completed", result_status="observed",
            result_sha256=sha256(b"bounded result").hexdigest(), usage_status="unavailable",
        )

    def setup_stage(self, case, stage):
        run = None
        if stage != "prepared":
            run = self.prepare(case)
        if stage == "finished":
            self.start(case, run)
        return run

    def invoke_stage(self, case, stage, run):
        if stage == "prepared":
            return self.prepare(case)
        if stage == "started":
            return self.start(case, run)
        return self.finish(case, run)

    def assert_unchanged(self, case, before, stage, run):
        self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before)
        self.assertTrue(case.store.verify_audit()["ok"])
        if stage == "prepared":
            self.assertEqual(case.store.list_codex_runs(attempt_id=case.claim["attempt_id"])["items"], [])
        else:
            expected = "prepared" if stage == "started" else "started"
            self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], expected)
        self.assertEqual(case.store.get_work_unit("unit-one")["status"], "leased")

    def test_failure_after_receipt_insert_rolls_back_each_transition(self):
        tables = {"prepared": "codex_run_preparations", "started": "codex_run_starts", "finished": "codex_run_finishes"}
        for stage, table in tables.items():
            with self.subTest(stage=stage), fixture() as case:
                run = self.setup_stage(case, stage)
                with case.store._connection() as connection:
                    connection.execute(f"CREATE TRIGGER injected_failure AFTER INSERT ON {table} "
                                       "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END")
                before = sha256(case.store.path.read_bytes()).hexdigest()
                with self.assertRaisesRegex((sqlite3.IntegrityError, AutonomyError), "injected receipt failure"):
                    self.invoke_stage(case, stage, run)
                self.assert_unchanged(case, before, stage, run)

    def test_failure_after_audit_append_rolls_back_each_transition(self):
        for stage in ("prepared", "started", "finished"):
            with self.subTest(stage=stage), fixture() as case:
                run = self.setup_stage(case, stage)
                original = case.store._append
                reached = []

                def fail_after_append(connection, event_type, **kwargs):
                    result = original(connection, event_type, **kwargs)
                    if event_type == "codex_run." + stage:
                        reached.append(event_type)
                        raise InjectedReceiptFailure("after receipt audit")
                    return result

                before = sha256(case.store.path.read_bytes()).hexdigest()
                with patch.object(case.store, "_append", side_effect=fail_after_append):
                    with self.assertRaises(InjectedReceiptFailure):
                        self.invoke_stage(case, stage, run)
                self.assertEqual(reached, ["codex_run." + stage])
                self.assert_unchanged(case, before, stage, run)
