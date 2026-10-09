import json
import os
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tests.runtime_schema_helpers import peel_schema14_codex_runs


class CodexRunMigrationTests(unittest.TestCase):
    fixture_archive = Path(__file__).resolve().parent / "fixtures/tasktra-schema13-client.zip"
    fixture_manifest = Path(__file__).resolve().parent / "fixtures/tasktra-schema13-client.manifest.json"
    receipt_tables = (
        "codex_run_preparations", "codex_run_starts", "codex_run_finishes",
    )
    receipt_indexes = (
        "codex_run_preparations_attempt_run", "codex_run_preparations_goal_created",
        "codex_run_starts_agent", "codex_run_finishes_recorded",
    )
    receipt_triggers = (
        "codex_run_preparations_no_update", "codex_run_preparations_no_delete",
        "codex_run_starts_no_update", "codex_run_starts_no_delete",
        "codex_run_finishes_no_update", "codex_run_finishes_no_delete",
    )

    @staticmethod
    def _schema13_client(source: Path, code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source)
        completed = subprocess.run(
            [sys.executable, "-c", code, *arguments], text=True, capture_output=True, env=environment,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed

    @staticmethod
    def _request(attempt_id: str) -> dict:
        return {
            "kind": "tasktra.intervention-request", "version": 1, "request_id": "request-one",
            "source": {"goal_id": "goal-one", "work_unit_id": "unit-one", "attempt_id": attempt_id},
            "producer": {"actor_id": "worker"}, "outcome_class": "blocked", "prompt": "Choose one option.",
            "rationale": "A decision is needed.", "impact": "Work remains blocked.",
            "requires_human_approval": False, "evidence_refs": [],
        }

    def _schema13_ledger(self, source: Path, path: Path) -> dict:
        script = r'''
import json, sys
from datetime import datetime, timedelta, timezone
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import AUTHORITY_ENVELOPE_KIND, AUTHORITY_ENVELOPE_VERSION, authority_envelope_sha256
now = datetime(2030, 1, 1, tzinfo=timezone.utc)
store = AutonomyStore(sys.argv[1])
envelope = {"kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION, "goal_id": "goal-one", "outcome": "Bounded.", "motivation": "Test.", "author_id": "owner", "acceptance_criteria": [{"id":"done","statement":"Done."}], "scope":{"paths":["."],"exclusions":[]}, "allowed_actions":["goal-activate","work-claim","work-requeue"], "allowed_effects":[LOCAL_REVERSIBLE_WRITE], "prohibited_actions":[], "quality_requirements":["Test."], "budgets":{"tokens":20,"attempts":3,"elapsed_seconds":60,"concurrency":1}, "dependencies":[], "checkpoints":[], "stop_conditions":["Stop."], "escalation_conditions":["Escalate."]}
digest = authority_envelope_sha256(envelope)
store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
store.define_goal_contract("goal-one", envelope, actor_id="owner", at=now)
expiry = now + timedelta(days=1)
store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry, at=now)
store.activate_goal("goal-one", actor_id="owner", envelope_sha256=digest, at=now)
store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths":["src"],"exclusions":[]})
store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=now)
lease_token = "lease-token-" * 4
claim = store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=digest, lease_token=lease_token, repository="repo", revision="rev", branch="main", workspace="work", at=now)
request = {"kind":"tasktra.intervention-request","version":1,"request_id":"request-one","source":{"goal_id":"goal-one","work_unit_id":"unit-one","attempt_id":claim["attempt_id"]},"producer":{"actor_id":"worker"},"outcome_class":"blocked","prompt":"Choose one option.","rationale":"A decision is needed.","impact":"Work remains blocked.","requires_human_approval":False,"evidence_refs":[]}
store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=lease_token, request=request, at=now + timedelta(seconds=1))
print(json.dumps({"attempt_id": claim["attempt_id"], "digest": digest, "lease_token": lease_token}, sort_keys=True))
'''
        return json.loads(self._schema13_client(source, script, str(path)).stdout)

    @staticmethod
    def _rows(path: Path, table: str, order: str) -> list[dict]:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        finally:
            connection.close()

    def _historical_snapshot(self, path: Path) -> dict[str, list[dict]]:
        snapshot = {
            "goals": self._rows(path, "goals", "id"),
            "work_units": self._rows(path, "work_units", "id"),
            "work_attempts": self._rows(path, "work_attempts", "id"),
            "intervention_requests": self._rows(path, "intervention_requests", "id"),
            "audit_events": self._rows(path, "audit_events", "sequence"),
            "authority_seals": self._rows(path, "authority_seals", "table_name,row_id,version"),
        }
        for row in snapshot["work_units"]:
            # Schema 15 supplies defaults for columns that did not exist in
            # the archived schema-13 client.  Historical-row comparison here
            # intentionally covers only columns that client could persist.
            row.pop("verification_policy", None)
            row.pop("acceptance_checks", None)
        return snapshot

    @staticmethod
    def _structure(path: Path) -> list[tuple[str, str, str | None]]:
        connection = sqlite3.connect(path)
        try:
            return [tuple(row) for row in connection.execute(
                "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )]
        finally:
            connection.close()

    def _extract_schema13_client(self, destination: Path) -> Path:
        self.assertTrue(self.fixture_archive.is_file())
        self.assertTrue(self.fixture_manifest.is_file())
        fixture = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
        self.assertEqual(fixture["archive"], self.fixture_archive.name)
        self.assertEqual(sha256(self.fixture_archive.read_bytes()).hexdigest(), fixture["archive_sha256"])
        self.assertEqual(self.fixture_archive.stat().st_size, fixture["archive_bytes"])
        files = fixture["files"]
        self.assertEqual(
            sha256(json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
            fixture["baseline_provenance"]["file_manifest_sha256"],
        )
        expected_names = [entry["path"] for entry in files]
        self.assertEqual(expected_names, sorted(expected_names))
        self.assertEqual(len(expected_names), len(set(expected_names)))
        with ZipFile(self.fixture_archive) as archive:
            self.assertEqual([entry.filename for entry in archive.infolist()], expected_names)
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertNotIn("\\", entry.filename)
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
            for entry in files:
                contents = archive.read(entry["path"])
                self.assertEqual(len(contents), entry["bytes"])
                self.assertEqual(sha256(contents).hexdigest(), entry["sha256"])
            archive.extractall(destination)
        source = destination
        self.assertIn("SCHEMA_VERSION = 13", (source / "tasktra/state.py").read_text(encoding="utf-8"))
        return source

    def test_clean_bootstrap_creates_current_receipt_shape(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            StateStore(path).create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                objects = {
                    (row[0], row[1]) for row in connection.execute(
                        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertTrue({("table", name) for name in self.receipt_tables}.issubset(objects))
                self.assertTrue({("index", name) for name in self.receipt_indexes}.issubset(objects))
                self.assertTrue({("trigger", name) for name in self.receipt_triggers}.issubset(objects))
                source = {row[1]: row for row in connection.execute("PRAGMA table_info(work_attempts)")}["token_accounting_source"]
                self.assertEqual(source[3], 1)
                self.assertEqual(source[4], "'legacy-unspecified'")
            finally:
                connection.close()

    def test_schema13_to_14_preserves_populated_history_and_backup_is_readable_by_schema13_client(self):
        with TemporaryDirectory() as fixture_directory, TemporaryDirectory() as directory:
            source = self._extract_schema13_client(Path(fixture_directory))
            path = Path(directory) / "state.sqlite"
            facts = self._schema13_ledger(source, path)
            before = self._historical_snapshot(path)
            evidence = StateStore(path).migrate_with_evidence()
            self.assertEqual((evidence["before_schema"], evidence["after_schema"]), (13, SCHEMA_VERSION))
            backup = Path(str(evidence["backup_path"]))
            self.assertTrue(backup.is_file())
            self.assertEqual(sha256(backup.read_bytes()).hexdigest(), evidence["backup_sha256"])
            after = self._historical_snapshot(path)
            self.assertEqual(after["goals"], before["goals"])
            self.assertEqual(after["work_units"], before["work_units"])
            self.assertEqual(after["intervention_requests"], before["intervention_requests"])
            self.assertEqual(after["audit_events"], before["audit_events"])
            # Schema migration appends replacement seals after changing the
            # canonical attempt shape, but never rewrites old seal history.
            for seal in before["authority_seals"]:
                self.assertIn(seal, after["authority_seals"])
            for row in after["work_attempts"]:
                self.assertEqual(row.pop("token_accounting_source"), "legacy-unspecified")
            self.assertEqual(after["work_attempts"], before["work_attempts"])
            self.assertTrue(StateStore(path).verify_audit()["ok"], StateStore(path).verify_audit())
            probe = r'''
import json, sqlite3, sys
from tasktra.state import StateStore
path = sys.argv[1]
store = StateStore(path)
connection = sqlite3.connect(path)
try:
 print(json.dumps({"schema": store.inspect_schema_version(), "goal": store.get_goal("goal-one")["id"], "requests": connection.execute("SELECT count(*) FROM intervention_requests").fetchone()[0], "audit_ok": store.verify_audit()["ok"]}, sort_keys=True))
finally:
 connection.close()
'''
            restored = json.loads(self._schema13_client(source, probe, str(backup)).stdout)
            self.assertEqual(restored, {"schema": 13, "goal": "goal-one", "requests": 1, "audit_ok": True})
            schema14_bytes = path.read_bytes()
            rejection = r'''
import json, sys
from tasktra.autonomy import AutonomyStore
store = AutonomyStore(sys.argv[1])
try:
 store.get_goal("goal-one")
except Exception as error:
 print(json.dumps({"error": str(error)}))
else:
 print(json.dumps({"error": None}))
'''
            result = json.loads(self._schema13_client(source, rejection, str(path)).stdout)
            self.assertIn("requires migration to 13", result["error"])
            self.assertEqual(schema14_bytes, path.read_bytes())
            self.assertEqual(facts["attempt_id"], before["work_attempts"][0]["id"])

    def test_schema13_shadow_receipt_shapes_fail_closed_and_leave_original_and_backup_intact(self):
        shadows = (
            ("CREATE TABLE codex_run_preparations (id TEXT PRIMARY KEY)", "pre-existing codex run table"),
            ("CREATE INDEX codex_run_preparations_attempt_run ON work_attempts(id)", "pre-existing codex run index"),
            ("CREATE TRIGGER codex_run_preparations_no_update BEFORE UPDATE ON work_attempts BEGIN SELECT RAISE(ABORT, 'shadow'); END", "pre-existing codex run trigger"),
        )
        with TemporaryDirectory() as fixture_directory, TemporaryDirectory() as directory:
            source = self._extract_schema13_client(Path(fixture_directory))
            for ordinal, (statement, message) in enumerate(shadows):
                with self.subTest(statement=statement):
                    path = Path(directory) / f"shadow-{ordinal}.sqlite"
                    self._schema13_ledger(source, path)
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()
                    before_bytes = path.read_bytes()
                    before_shape = self._structure(path)
                    with self.assertRaisesRegex(StateError, "schema13 does not match a complete known lineage structure"):
                        StateStore(path).migrate_with_evidence()
                    self.assertEqual(path.read_bytes(), before_bytes)
                    self.assertEqual(self._structure(path), before_shape)
                    # Structural validation runs before backup creation.  A
                    # malformed shadow must therefore leave no snapshot of a
                    # shape that was never eligible for migration.
                    backup = path.with_name(f"{path.name}.v13.bak")
                    self.assertFalse(backup.exists())

    def test_schema13_shadow_accounting_column_is_refused_before_any_ddl(self):
        with TemporaryDirectory() as fixture_directory, TemporaryDirectory() as directory:
            source = self._extract_schema13_client(Path(fixture_directory))
            path = Path(directory) / "shadow-column.sqlite"
            self._schema13_ledger(source, path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("ALTER TABLE work_attempts ADD COLUMN token_accounting_source TEXT")
                connection.commit()
                before = self._structure(path)
                with self.assertRaisesRegex(StateError, "schema13 does not match a complete known lineage structure"):
                    StateStore._migrate(connection, create_backup=False)
                self.assertEqual(self._structure(path), before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 13)
            finally:
                connection.close()

    def test_peeling_schema14_objects_makes_honest_schema13_fixture(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            StateStore(path).create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            connection = sqlite3.connect(path)
            try:
                peel_schema14_codex_runs(connection, target_version=13)
                connection.execute("PRAGMA user_version=13")
                connection.commit()
                names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
                self.assertFalse(set(self.receipt_tables + self.receipt_indexes + self.receipt_triggers) & names)
                self.assertNotIn("token_accounting_source", {row[1] for row in connection.execute("PRAGMA table_info(work_attempts)")})
            finally:
                connection.close()
            StateStore(path).migrate_with_evidence()
            self.assertEqual(StateStore(path).inspect_schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
