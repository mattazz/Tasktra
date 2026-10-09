"""Schema and historical-receipt compatibility for execution recovery."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.compiler import load_catalog
from tasktra.config import ProjectConfig
from tasktra.execution_recovery import reconcile_codex_run, unresolved_codex_runs
from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tests import test_codex_runs_migration as schema13_migration
from tests import test_execution_recovery_runtime as recovery_runtime
from tests import test_intervention_migration as schema12_migration


_CAPTURED_AT = "2031-01-01T00:00:00Z"


def _schema_objects(path: Path) -> tuple[int, list[tuple[str, str, str | None, str | None]]]:
    connection = sqlite3.connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
        return version, objects
    finally:
        connection.close()


def _observation(run: dict, *, agent_id: str | None = None) -> dict:
    name = "/root/" + run["requested_task_name"]
    return {
        "kind": "tasktra.codex-host-tree-observation",
        "version": 1,
        "source": "collaboration.list_agents",
        "captured_at": _CAPTURED_AT,
        "parent_canonical_name": "/root",
        "observed_agent_names": [name],
        "target": {
            "canonical_name": name,
            "agent_id": agent_id,
            "status": {"kind": "running", "source_shape": "running-string"},
        },
    }


def _extract_schema12_client(destination: Path) -> Path:
    """Validate and unpack the existing historical client fixture only."""
    case = schema12_migration.InterventionMigrationTests()
    archive, manifest = case.fixture_archive, case.fixture_manifest
    fixture = json.loads(manifest.read_text(encoding="utf-8"))
    if fixture["archive"] != archive.name:
        raise AssertionError("schema12 fixture archive name does not match its manifest")
    if sha256(archive.read_bytes()).hexdigest() != fixture["archive_sha256"]:
        raise AssertionError("schema12 fixture archive digest does not match its manifest")
    if archive.stat().st_size != fixture["archive_bytes"]:
        raise AssertionError("schema12 fixture archive size does not match its manifest")
    files = fixture["files"]
    if sha256(json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest() != fixture["baseline_provenance"]["file_manifest_sha256"]:
        raise AssertionError("schema12 fixture file manifest has the wrong provenance digest")
    with ZipFile(archive) as zip_file:
        if [entry.filename for entry in zip_file.infolist()] != [item["path"] for item in files]:
            raise AssertionError("schema12 fixture archive order does not match its manifest")
        for entry in zip_file.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise AssertionError("schema12 fixture contains an unsafe member")
        for item in files:
            contents = zip_file.read(item["path"])
            if len(contents) != item["bytes"]:
                raise AssertionError("schema12 fixture member size does not match its manifest")
            if sha256(contents).hexdigest() != item["sha256"]:
                raise AssertionError("schema12 fixture member digest does not match its manifest")
        zip_file.extractall(destination)
    source = destination
    if "SCHEMA_VERSION = 12" not in (source / "tasktra/state.py").read_text(encoding="utf-8"):
        raise AssertionError("schema12 historical source has the wrong schema version")
    return source


class ExecutionRecoveryCompatibilityTests(unittest.TestCase):
    def test_clean_schema14_observations_preserve_version_and_sqlite_objects(self):
        with recovery_runtime.fixture() as case:
            before = _schema_objects(case.store.path)
            self.assertEqual(before[0], SCHEMA_VERSION)

            run = recovery_runtime.prepare(case, "compat-clean")
            page = unresolved_codex_runs(case.store, at=_CAPTURED_AT)
            self.assertEqual(page["items"][0]["reason"], "prepared-unobserved")

            reconcile_codex_run(
                case.store, run["run_id"], "observer", _observation(run), at=_CAPTURED_AT,
            )
            completed = _observation(run)
            completed["target"]["status"] = {"kind": "completed", "source_shape": "completed-object"}
            reconcile_codex_run(
                case.store, run["run_id"], "observer", completed, b"completed", at="2031-01-02T00:00:00Z",
            )

            self.assertEqual(_schema_objects(case.store.path), before)

    def test_schema12_and_schema13_entrypoints_require_migration_without_mutation(self):
        cases = (
            (12, schema12_migration.InterventionMigrationTests(), _extract_schema12_client, "_schema12_ledger"),
            (13, schema13_migration.CodexRunMigrationTests(), lambda destination: schema13_migration.CodexRunMigrationTests()._extract_schema13_client(destination), "_schema13_ledger"),
        )
        for version, fixture_case, extractor, ledger_name in cases:
            with self.subTest(schema=version), TemporaryDirectory() as fixture_directory, TemporaryDirectory() as directory:
                source = extractor(Path(fixture_directory))
                path = Path(directory) / "state.sqlite"
                facts = getattr(fixture_case, ledger_name)(source, path)
                before_bytes, before_shape = path.read_bytes(), _schema_objects(path)
                store = StateStore(path)

                with self.assertRaisesRegex(StateError, "requires migration"):
                    unresolved_codex_runs(store, at=_CAPTURED_AT)
                with self.assertRaisesRegex(StateError, "requires migration"):
                    reconcile_codex_run(store, "run-one", "observer", _observation({"requested_task_name": "worker"}), at=_CAPTURED_AT)
                self.assertEqual(path.read_bytes(), before_bytes)
                self.assertEqual(_schema_objects(path), before_shape)

                evidence = store.migrate_with_evidence()
                self.assertEqual((evidence["before_schema"], evidence["after_schema"]), (version, SCHEMA_VERSION))
                migrated_shape = _schema_objects(path)
                self.assertEqual(migrated_shape[0], SCHEMA_VERSION)
                self.assertEqual(unresolved_codex_runs(store, at=_CAPTURED_AT), {"items": [], "next_after_run_id": None})
                self._assert_migrated_receipt_history(store, facts)
                self.assertEqual(_schema_objects(path), migrated_shape)

    def _assert_migrated_receipt_history(self, store, facts):
        writer = AutonomyStore(store.path)
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        if writer.get_work_unit("unit-one")["status"] == "leased":
            writer.finish_attempt(attempt_id=facts["attempt_id"], performer_id="worker",
                lease_token=facts["lease_token"], outcome="blocked", tokens_consumed=None,
                at=now + timedelta(seconds=1))
        writer.create_work_unit(goal_id="goal-one", work_unit_id="migrated-receipts", title="Migrated receipts",
                                scope={"paths": ["src"], "exclusions": []})
        writer.record_transition_approval(
            goal_id="goal-one", work_unit_id="migrated-receipts", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=facts["digest"], approver_id="history-steward", approver_kind="steward",
            performer_id="worker", valid_until=now + timedelta(days=1), at=now + timedelta(seconds=2),
        )
        token = "migrated-history-fixture-token-" + "x" * 32
        claim = writer.claim_next_work(
            goal_id="goal-one", performer_id="worker", envelope_sha256=facts["digest"], lease_token=token,
            repository="fixture", revision="revision", branch="main", workspace="workspace",
            lease_seconds=10, at=now + timedelta(seconds=2),
        )
        self.assertEqual(claim["work_unit_id"], "migrated-receipts")
        request = {
            "kind": "tasktra.routing-request", "version": 1, "task_id": "migrated-history",
            "source": {"goal_id": "goal-one", "work_unit_id": "migrated-receipts"},
            "objective": "Inspect migrated history.", "primary_signal": "inspect", "signals": ["inspect"],
            "constraints": [], "verified_facts": [], "evidence_refs": [],
        }
        run = writer.prepare_codex_run(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=token,
            catalog=load_catalog(recovery_runtime.ROOT / "catalog"), config=ProjectConfig(name="History", concurrency_limit=1),
            routing_request=request, idempotency_key="migrated-history", at=now + timedelta(seconds=2),
        )
        self.assertEqual(unresolved_codex_runs(store, at=_CAPTURED_AT)["items"][0]["reason"], "prepared-unobserved")
        # Exercise the public StateStore interface as well as migrated history.
        started = reconcile_codex_run(store, run["run_id"], "history-observer", _observation(run), at=_CAPTURED_AT)
        self.assertEqual(unresolved_codex_runs(store, at=_CAPTURED_AT)["items"][0]["reason"], "started-unterminated")
        complete = _observation(run)
        complete["target"]["status"] = {"kind": "completed", "source_shape": "completed-object"}
        finished = reconcile_codex_run(store, run["run_id"], "history-finisher", complete, b"Historical completion.", at=_CAPTURED_AT)
        self.assertEqual(finished["recorded_attribution"]["start"], started["recorded_attribution"]["start"])
        self.assertEqual(unresolved_codex_runs(store, at=_CAPTURED_AT)["items"], [])
        self.assertTrue(writer.verify_audit()["ok"])

    def test_historical_nonnull_start_id_stays_visible_and_adapter_refuses_it(self):
        with recovery_runtime.fixture() as case:
            run = recovery_runtime.prepare(case, "compat-legacy-agent")
            prepared = unresolved_codex_runs(case.store, at=_CAPTURED_AT)
            self.assertEqual(prepared["items"][0]["reason"], "prepared-unobserved")
            name = "/root/" + run["requested_task_name"]
            case.store.record_codex_start(
                run_id=run["run_id"], observer_id="legacy-observer", host_canonical_name=name,
                host_agent_id="legacy-agent", at=_CAPTURED_AT,
            )
            page = unresolved_codex_runs(case.store, at=_CAPTURED_AT)
            self.assertEqual(page["items"][0]["reason"], "started-unterminated")
            self.assertEqual(page["items"][0]["actual"], {"canonical_name": name, "agent_id": "legacy-agent", "model": None, "reasoning_effort": None})

            before = case.store.path.read_bytes()
            connection = sqlite3.connect(case.store.path)
            try:
                start_before = connection.execute(
                    "SELECT observed_by,recorded_at FROM codex_run_starts WHERE run_id=?", (run["run_id"],)
                ).fetchone()
            finally:
                connection.close()
            with self.assertRaisesRegex(AutonomyError, "agent id must be null"):
                reconcile_codex_run(
                    case.store, run["run_id"], "observer", _observation(run, agent_id="legacy-agent"), at=_CAPTURED_AT,
                )
            self.assertEqual(case.store.path.read_bytes(), before)
            connection = sqlite3.connect(case.store.path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT observed_by,recorded_at FROM codex_run_starts WHERE run_id=?", (run["run_id"],)
                    ).fetchone(),
                    start_before,
                )
            finally:
                connection.close()

            case.store.record_codex_finish(
                run_id=run["run_id"], observer_id="legacy-finisher", outcome="completed",
                result_status="observed", result_sha256="0" * 64, usage_status="unavailable",
                at="2031-01-02T00:00:00Z",
            )
            finished = case.store.get_codex_run(run["run_id"])
            self.assertEqual(finished["state"], "finished")
            self.assertEqual(finished["actual"]["agent_id"], "legacy-agent")
            self.assertEqual(finished["result"]["observed_by"], "legacy-finisher")


if __name__ == "__main__":
    unittest.main()
