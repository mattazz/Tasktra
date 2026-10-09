from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from tasktra.autonomy import AutonomyError
from tasktra.compiler import load_catalog
from tasktra.config import load_project_config
from tests.test_codex_runs_cli import CodexRunCliTests


class CodexRunRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = CodexRunCliTests(); self.f.setUp()
        self.store, self.token, self.attempt = self.f.store, self.f.token, self.f.claim["attempt_id"]
        self.request = json.loads(self.f.request.read_text(encoding="utf-8"))
        self.catalog = load_catalog(Path(__file__).resolve().parents[1] / "catalog")
        self.config = load_project_config(self.f.root)

    def tearDown(self) -> None: self.f.tearDown()

    def prepare(self, key: str) -> dict[str, object]:
        return self.store.prepare_codex_run(attempt_id=self.attempt, performer_id="worker", lease_token=self.token,
            catalog=self.catalog, config=self.config, routing_request=self.request, idempotency_key=key)

    def test_retry_open_slot_identity_and_sequential_accounting(self) -> None:
        first = self.prepare("first-key")
        self.assertEqual(self.prepare("first-key")["launch_directive"], "reconcile-only")
        with self.assertRaises(AutonomyError): self.prepare("second-key")
        run, name = str(first["run_id"]), str(first["requested_task_name"])
        self.store.record_codex_start(run_id=run, observer_id="worker", host_canonical_name=name)
        with self.assertRaises(AutonomyError): self.store.record_codex_start(run_id=run, observer_id="worker", host_canonical_name="/root/other")
        self.store.record_codex_finish(run_id=run, observer_id="worker", outcome="failed", result_status="unavailable", result_sha256=None, usage_status="measured", input_tokens=3, output_tokens=5)
        second = self.prepare("second-key")
        with self.assertRaises(AutonomyError):
            self.store.finish_attempt(attempt_id=self.attempt, performer_id="worker", lease_token=self.token, outcome="transient", tokens_consumed=8, accounting_source="host-measured")
        self.store.record_codex_start(run_id=str(second["run_id"]), observer_id="worker", host_canonical_name=str(second["requested_task_name"]))
        self.store.record_codex_finish(run_id=str(second["run_id"]), observer_id="worker", outcome="failed", result_status="unavailable", result_sha256=None, usage_status="measured", input_tokens=1, output_tokens=2)
        done = self.store.finish_attempt(attempt_id=self.attempt, performer_id="worker", lease_token=self.token, outcome="transient", tokens_consumed=None)
        self.assertEqual(done["token_accounting_source"], "unavailable")

    def test_immutable_receipt_rejects_direct_tamper(self) -> None:
        run = self.prepare("tamper-key")
        connection = sqlite3.connect(self.store.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE codex_run_preparations SET role='tampered' WHERE id=?", (run["run_id"],))
        finally:
            connection.close()

    def test_expiry_with_terminal_host_receipt_requires_review(self) -> None:
        run = self.prepare("expiry-key")
        self.store.record_codex_start(run_id=str(run["run_id"]), observer_id="worker", host_canonical_name=str(run["requested_task_name"]))
        self.store.record_codex_finish(run_id=str(run["run_id"]), observer_id="worker", outcome="failed", result_status="unavailable", result_sha256=None, usage_status="unavailable")
        self.store.recover_expired_leases(at="2099-01-01T00:00:00Z")
        unit = self.store.get_work_unit("unit-one")
        self.assertEqual(unit["status"], "blocked")
        with self.store._readonly_connection() as connection:
            attempt = connection.execute("SELECT outcome_json FROM work_attempts WHERE id=?", (self.attempt,)).fetchone()
        self.assertEqual(json.loads(attempt["outcome_json"])["reason"], "host-execution-requires-review")

    def test_observer_token_is_not_persistable(self) -> None:
        run = self.prepare("observer-key")
        with self.assertRaises(AutonomyError):
            self.store.record_codex_start(run_id=str(run["run_id"]), observer_id=self.token,
                                          host_canonical_name=str(run["requested_task_name"]))

    def test_mixed_sequential_usage_is_not_accounting_complete(self) -> None:
        first = self.prepare("mixed-first")
        self.store.record_codex_start(run_id=str(first["run_id"]), observer_id="worker", host_canonical_name=str(first["requested_task_name"]))
        self.store.record_codex_finish(run_id=str(first["run_id"]), observer_id="worker", outcome="failed", result_status="unavailable", result_sha256=None, usage_status="measured", input_tokens=1, output_tokens=1)
        second = self.prepare("mixed-second")
        self.store.record_codex_start(run_id=str(second["run_id"]), observer_id="worker", host_canonical_name=str(second["requested_task_name"]))
        self.store.record_codex_finish(run_id=str(second["run_id"]), observer_id="worker", outcome="failed", result_status="unavailable", result_sha256=None, usage_status="unavailable")
        shown = self.store.get_codex_run(str(first["run_id"]))
        self.assertFalse(shown["attempt"]["accounting_complete"])
        self.assertEqual(shown["result"]["outcome"], "failed")
