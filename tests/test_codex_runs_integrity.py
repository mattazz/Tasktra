"""Integrity regressions for immutable Codex host-run receipts."""
from __future__ import annotations

from contextlib import closing
import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tasktra.state import StateError, StateStore, _now
from tasktra.autonomy import AutonomyError
from tasktra.compiler import load_catalog
from tasktra.config import load_project_config
from tasktra.scheduling import preview_schedule
from tests import test_codex_runs_cli as cli_fixtures
from tests import test_codex_runs_capacity as capacity_fixtures

NOW = capacity_fixtures.NOW


class CodexRunIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = cli_fixtures.CodexRunCliTests(); self.fixture.setUp()
        self.store, self.token = self.fixture.store, self.fixture.token
        self.attempt = self.fixture.claim["attempt_id"]
        self.catalog = load_catalog(Path(__file__).resolve().parents[1] / "catalog")
        self.config = load_project_config(self.fixture.root)
        self.request = json.loads(self.fixture.request.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def prepare(self, key: str = "integrity-key") -> dict[str, object]:
        return self.store.prepare_codex_run(
            attempt_id=self.attempt, performer_id="worker", lease_token=self.token,
            catalog=self.catalog, config=self.config, routing_request=self.request,
            idempotency_key=key,
        )

    def assert_rejected_without_mutation(self, run_id: str) -> None:
        before = self.store.path.read_bytes()
        self.assertFalse(self.store.verify_audit()["ok"])
        with self.assertRaises(StateError):
            self.store.get_codex_run(run_id)
        with self.assertRaises(StateError):
            self.store.attest_ledger(actor_id="human")
        with self.assertRaises((StateError, AutonomyError)):
            self.store.record_codex_start(run_id=run_id, observer_id="worker", host_canonical_name="codex_invalid")
        self.assertEqual(before, self.store.path.read_bytes())

    def reseal_after_raw_mutation(self, run_id: str, *, preparation: bool, attempt: bool) -> None:
        """Model a retained corrupt database whose current-state seals were recomputed."""
        connection = sqlite3.connect(self.store.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            if preparation:
                connection.execute("DROP TRIGGER codex_run_preparations_no_update")
                connection.execute("UPDATE codex_run_preparations SET repository='forged-repository' WHERE id=?", (run_id,))
            if attempt:
                connection.execute("UPDATE work_attempts SET repository='forged-repository' WHERE id=?", (self.attempt,))
            StateStore._seal_current_state_in_transaction(connection, _now())
            connection.commit()
        finally:
            connection.close()

    def test_resealed_preparation_parent_and_mutually_consistent_edits_fail_closed(self) -> None:
        for index, (change_prep, change_attempt) in enumerate(((True, False), (False, True), (True, True))):
            with self.subTest(preparation=change_prep, attempt=change_attempt):
                if index:
                    self.tearDown(); self.setUp()
                run = self.prepare(f"reseal-{index}")
                self.reseal_after_raw_mutation(str(run["run_id"]), preparation=change_prep, attempt=change_attempt)
                self.assert_rejected_without_mutation(str(run["run_id"]))

    def test_persisted_receipt_semantics_and_audit_order_fail_closed(self) -> None:
        variants = ("host", "timestamp", "finish-contract", "finish-before-start")
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                if index:
                    self.tearDown(); self.setUp()
                run = self.prepare(f"semantic-{index}")
                run_id, name = str(run["run_id"]), str(run["requested_task_name"])
                with self.store._connection() as connection:
                    self.store._prepare_write(connection)
                    prepared_at = connection.execute("SELECT prepared_at FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()[0]
                    start = {
                        "run_id": run_id,
                        "host_canonical_name": "invalid host" if variant == "host" else name,
                        "host_agent_id": None,
                        "observed_by": "worker",
                        "recorded_at": "not-a-time" if variant == "timestamp" else prepared_at,
                    }
                    finish = {"run_id": run_id, "outcome": "completed" if variant == "finish-contract" else "failed",
                              "result_status": "unavailable", "result_sha256": None, "usage_status": "unavailable",
                              "input_tokens": None, "output_tokens": None, "observed_by": "worker", "recorded_at": prepared_at}
                    connection.execute("INSERT INTO codex_run_starts(run_id,host_canonical_name,host_agent_id,observed_by,recorded_at) VALUES(?,?,?,?,?)", tuple(start.values()))
                    if variant in {"finish-contract", "finish-before-start"}:
                        connection.execute("INSERT INTO codex_run_finishes(run_id,outcome,result_status,result_sha256,usage_status,input_tokens,output_tokens,observed_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)", tuple(finish.values()))
                    if variant == "finish-before-start":
                        self.store._append(connection, "codex_run.finished", goal_id="goal-one", work_unit_id="unit-one", payload=finish)
                    self.store._append(connection, "codex_run.started", goal_id="goal-one", work_unit_id="unit-one", payload=start)
                    if variant == "finish-contract":
                        self.store._append(connection, "codex_run.finished", goal_id="goal-one", work_unit_id="unit-one", payload=finish)
                self.assert_rejected_without_mutation(run_id)

    def test_preparation_metadata_cannot_retain_its_bound_lease_token(self) -> None:
        original = type(self.store)._append

        def corrupt_profile(store, connection, event_type, **kwargs):
            if event_type == "codex_run.prepared":
                run_id = kwargs["payload"]["run_id"]
                trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='codex_run_preparations_no_update'").fetchone()[0]
                connection.execute("DROP TRIGGER codex_run_preparations_no_update")
                connection.execute("UPDATE codex_run_preparations SET requested_model=? WHERE id=?", (self.token, run_id))
                connection.execute(trigger)
                row = connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()
                kwargs["payload"]["preparation_row_sha256"] = __import__("tasktra.state", fromlist=["_authority_row_hash"])._authority_row_hash("codex_run_preparations", row)
            return original(connection, event_type, **kwargs)

        with patch.object(type(self.store), "_append", corrupt_profile):
            run = self.prepare("prepared-secret")
        self.assert_rejected_without_mutation(str(run["run_id"]))

    def test_sealed_orphan_receipt_fails_verified_read_and_attestation(self) -> None:
        connection = sqlite3.connect(self.store.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("INSERT INTO codex_run_starts(run_id,host_canonical_name,observed_by,recorded_at) VALUES('orphan-run','/root/orphan','worker','2030-01-01T00:00:00Z')")
            StateStore._seal_current_state_in_transaction(connection, "2030-01-01T00:00:00Z")
            connection.commit()
        finally:
            connection.close()
        before = self.store.path.read_bytes()
        self.assertFalse(self.store.verify_audit()["ok"])
        with self.assertRaises(StateError):
            self.store.get_codex_run("orphan-run")
        with self.assertRaises(StateError):
            self.store.attest_ledger(actor_id="human")
        self.assertEqual(before, self.store.path.read_bytes())

    def test_historical_contract_is_bound_at_prepare_not_current_contract(self) -> None:
        run = self.prepare("historical-contract")
        self.store.append_event("goal.contract_defined", goal_id="goal-one", payload={"actor_id": "owner", "envelope_sha256": "f" * 64})
        self.assertTrue(self.store.verify_audit()["ok"])
        self.assertIsNotNone(self.store.get_codex_run(str(run["run_id"])))

    def test_null_orphan_receipts_fail_with_existing_preparation(self) -> None:
        for index, table in enumerate(("codex_run_starts", "codex_run_finishes")):
            with self.subTest(table=table):
                if index:
                    self.tearDown(); self.setUp()
                run = self.prepare("null-orphan")
                with closing(sqlite3.connect(self.store.path)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    if table == "codex_run_starts":
                        connection.execute(
                            "INSERT INTO codex_run_starts(run_id,host_canonical_name,observed_by,recorded_at) "
                            "VALUES(NULL,'/root/orphan','worker','2030-01-01T00:00:00Z')"
                        )
                    else:
                        connection.execute(
                            "INSERT INTO codex_run_finishes(run_id,outcome,result_status,usage_status,observed_by,recorded_at) "
                            "VALUES(NULL,'interrupted','unavailable','unavailable','worker','2030-01-01T00:00:00Z')"
                        )
                    StateStore._seal_current_state_in_transaction(connection, _now())
                self.assert_rejected_without_mutation(str(run["run_id"]))

    def test_frozen_intervention_verifier_body_is_exactly_preserved(self) -> None:
        fixture = Path(__file__).parent / "fixtures"
        archive = fixture / "tasktra-schema13-client.zip"
        manifest = json.loads((fixture / "tasktra-schema13-client.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["archive_sha256"], sha256(archive.read_bytes()).hexdigest())
        with ZipFile(archive) as source:
            frozen_text = source.read("tasktra/state.py").decode("utf-8")
        current = Path(__file__).resolve().parents[1] / "src" / "tasktra" / "state.py"
        current_text = current.read_text(encoding="utf-8")
        marker = "    def _assert_intervention_audit_bindings_in_transaction(connection: sqlite3.Connection) -> None:\n"
        frozen_start, current_start = frozen_text.index(marker), current_text.index(marker)
        frozen_end = frozen_text.index("    @staticmethod\n    def _seal_current_state_in_transaction", frozen_start)
        current_end = current_text.index("    @staticmethod\n    def _assert_codex_run_integrity_in_transaction", current_start)
        self.assertEqual(frozen_text[frozen_start:frozen_end], current_text[current_start:current_end])



class ScheduledRecoveryPersistenceTests(unittest.TestCase):
    def test_expired_prepared_attempt_is_durable_when_detached_capacity_rejects_schedule(self) -> None:
        fixture = capacity_fixtures.CodexCapacityTests(); fixture.setUp()
        try:
            fixture.add_unit("unit-one")
            fixture.add_unit("unit-two")
            claim = fixture.claim()
            run = fixture.prepare(claim, "scheduled-recovery")
            invocation = preview_schedule(
                fixture.store, goal_id="goal-one", work_unit_id="unit-two", envelope_sha256=fixture.digests["goal-one"],
                checkpoint_id=None, cadence="manual fixture", notification_intent="none", performer_id="worker",
                repository="fixture", revision="revision", branch="main", workspace="workspace", lease_seconds=30,
                token_reservation=0,
            )["invocation"]
            with self.assertRaises(AutonomyError):
                fixture.store.claim_scheduled_work(invocation=invocation, lease_token=fixture.token, at=NOW.replace(second=31))
            unit = fixture.store.get_work_unit("unit-one")
            self.assertEqual(unit["status"], "blocked")
            with fixture.store._readonly_connection() as connection:
                recovered = connection.execute("SELECT status,outcome_json FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
                duplicate = connection.execute("SELECT count(*) FROM work_attempts WHERE work_unit_id='unit-two'").fetchone()[0]
            self.assertEqual(recovered["status"], "expired")
            self.assertEqual(json.loads(recovered["outcome_json"])["reason"], "host-execution-requires-review")
            self.assertEqual(duplicate, 0)
            self.assertTrue(fixture.store.verify_audit()["ok"])
            self.assertIsNotNone(run["run_id"])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
