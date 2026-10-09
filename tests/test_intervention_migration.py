import sqlite3
import json
import os
from hashlib import sha256
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from tasktra.autonomy import AutonomyStore
from tasktra.interventions import intervention_inbox, intervention_request_sha256
from tasktra.state import SCHEMA_VERSION, StateError, StateStore, _AUTHORITATIVE_TABLE_KEYS, _authority_row_hash, _encode
from tests.runtime_schema_helpers import peel_schema13_interventions


class InterventionMigrationTests(unittest.TestCase):
    fixture_archive = Path(__file__).resolve().parent / "fixtures/tasktra-schema12-client.zip"
    fixture_manifest = Path(__file__).resolve().parent / "fixtures/tasktra-schema12-client.manifest.json"

    @staticmethod
    def _request(attempt_id: str) -> dict:
        return {
            "kind": "tasktra.intervention-request", "version": 1, "request_id": "request-one",
            "source": {"goal_id": "goal-one", "work_unit_id": "unit-one", "attempt_id": attempt_id},
            "producer": {"actor_id": "worker"}, "outcome_class": "blocked", "prompt": "Choose one option.",
            "rationale": "A decision is needed.", "impact": "Work remains blocked.",
            "requires_human_approval": False, "evidence_refs": [],
        }

    def _schema12_client(self, source: Path, code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source)
        completed = subprocess.run([sys.executable, "-c", code, *arguments], text=True, capture_output=True,
                                   env=environment)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed

    def _schema12_ledger(self, source: Path, path: Path) -> dict:
        script = '''
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
store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=now)
lease_token = "lease-token-" * 4
claim = store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=digest, lease_token=lease_token, repository="repo", revision="rev", branch="main", workspace="work", at=now)
print(json.dumps({"attempt_id": claim["attempt_id"], "digest": digest, "lease_token": lease_token}))
'''
        return json.loads(self._schema12_client(source, script, str(path)).stdout)

    def _schema12_database(self, path: Path) -> None:
        store = StateStore(path)
        store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        connection = sqlite3.connect(path)
        try:
            peel_schema13_interventions(connection, target_version=12)
            connection.execute("PRAGMA user_version=12")
            connection.commit()
        finally:
            connection.close()

    def test_schema12_requires_explicit_migration_then_preserves_existing_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            self._schema12_database(path)
            store = StateStore(path)
            with self.assertRaisesRegex(StateError, "requires migration"):
                store.get_goal("goal-one")
            evidence = store.migrate_with_evidence()
            self.assertEqual((evidence["before_schema"], evidence["after_schema"]), (12, SCHEMA_VERSION))
            self.assertTrue(Path(str(evidence["backup_path"])).is_file())
            self.assertEqual(store.get_goal("goal-one")["id"], "goal-one")
            with store._connection(write=False) as connection:
                self.assertIn("current_intervention_id", {row[1] for row in connection.execute("PRAGMA table_info(work_units)")})
                self.assertEqual(connection.execute("SELECT count(*) FROM intervention_requests").fetchone()[0], 0)
            self.assertTrue(store.verify_audit()["ok"], store.verify_audit())

    def _assert_schema12_shadow_refused(self, statement: str, message: str, object_name: str) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            self._schema12_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(statement)
                connection.commit()
                before = (
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    connection.execute("PRAGMA schema_version").fetchone()[0],
                    connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (object_name,)).fetchone()[0],
                )
                with self.assertRaisesRegex(StateError, "schema12 does not match a complete known lineage structure"):
                    StateStore._migrate(connection)
                after = (
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    connection.execute("PRAGMA schema_version").fetchone()[0],
                    connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (object_name,)).fetchone()[0],
                )
                self.assertEqual(after, before)
            finally:
                connection.close()

    def test_schema12_shadow_v13_objects_fail_closed(self):
        for statement, message, object_name in (
            ("CREATE TABLE intervention_requests (id TEXT PRIMARY KEY)", "pre-existing intervention table: intervention_requests", "intervention_requests"),
            ("CREATE INDEX work_unit_dependencies_prerequisite ON work_unit_dependencies(work_unit_id)", "pre-existing intervention index: work_unit_dependencies_prerequisite", "work_unit_dependencies_prerequisite"),
            ("ALTER TABLE work_units ADD COLUMN current_intervention_id TEXT", "pre-existing current intervention pointer", "work_units"),
        ):
            with self.subTest(statement=statement):
                self._assert_schema12_shadow_refused(statement, message, object_name)

    def test_real_schema12_client_and_schema13_client_refuse_each_other_until_migration(self):
        self.assertTrue(self.fixture_archive.is_file())
        self.assertTrue(self.fixture_manifest.is_file())
        fixture = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
        self.assertEqual(fixture["archive"], self.fixture_archive.name)
        self.assertEqual(sha256(self.fixture_archive.read_bytes()).hexdigest(), fixture["archive_sha256"])
        self.assertEqual(self.fixture_archive.stat().st_size, fixture["archive_bytes"])
        expected_files = fixture["files"]
        self.assertEqual(
            sha256(json.dumps(expected_files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
            fixture["baseline_provenance"]["file_manifest_sha256"],
        )
        expected_names = [entry["path"] for entry in expected_files]
        self.assertEqual(len(expected_names), len(set(expected_names)))
        with TemporaryDirectory() as fixture_directory, TemporaryDirectory() as directory:
            with ZipFile(self.fixture_archive) as archive:
                names = [entry.filename for entry in archive.infolist()]
                self.assertEqual(names, expected_names)
                for name in names:
                    path = PurePosixPath(name)
                    self.assertFalse(path.is_absolute())
                    self.assertNotIn("..", path.parts)
                    self.assertNotIn("\\", name)
                for entry in expected_files:
                    contents = archive.read(entry["path"])
                    self.assertEqual(len(contents), entry["bytes"])
                    self.assertEqual(sha256(contents).hexdigest(), entry["sha256"])
                archive.extractall(fixture_directory)
            fixture_source = Path(fixture_directory)
            self.assertIn("SCHEMA_VERSION = 12", (fixture_source / "tasktra/state.py").read_text(encoding="utf-8"))
            path = Path(directory) / "state.sqlite"
            facts = self._schema12_ledger(fixture_source, path)
            before_counts = self._counts(path)
            before_rows = self._historical_rows(path)
            request = self._request(facts["attempt_id"])
            response = {
                "kind": "tasktra.intervention-response", "version": 1, "response_id": "response-one",
                "request": {"request_id": "request-one", "request_sha256": intervention_request_sha256(request)},
                "expected_current_response": None, "responder": {"kind": "human", "actor_id": "operator"},
                "disposition": "answered", "answer": "Proceed.", "rationale": "Approved.", "evidence_refs": [],
            }
            isolated = AutonomyStore(path)
            before = path.read_bytes()
            for operation in (
                lambda: isolated.yield_for_intervention(attempt_id=facts["attempt_id"], performer_id="worker", lease_token=facts["lease_token"], request=request),
                lambda: isolated.record_intervention_response(response=response, responder_id="operator", responder_kind="human"),
                lambda: intervention_inbox(isolated),
                lambda: isolated.create_work_unit(goal_id="goal-one", work_unit_id="unit-two", title="Later", scope={"paths": ["src"], "exclusions": []}),
                lambda: isolated.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=facts["digest"], lease_token="x" * 32, repository="repo", revision="rev", branch="main", workspace="work"),
                lambda: isolated.heartbeat(attempt_id=facts["attempt_id"], performer_id="worker", lease_token=facts["lease_token"]),
                lambda: isolated.finish_attempt(attempt_id=facts["attempt_id"], performer_id="worker", lease_token=facts["lease_token"], outcome="blocked"),
                lambda: isolated.recover_expired_leases(),
                lambda: isolated.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=facts["digest"], evidence={"decision": "safe"}),
            ):
                with self.assertRaisesRegex(StateError, "requires.*migration"):
                    operation()
            self.assertEqual(before, path.read_bytes())
            migrated = isolated.migrate_with_evidence()
            self.assertEqual((migrated["before_schema"], migrated["after_schema"]), (12, SCHEMA_VERSION))
            self.assertTrue(Path(str(migrated["backup_path"])).is_file())
            self.assertEqual(before_counts, self._counts(path))
            self.assertEqual(before_rows, self._historical_rows(path))
            self.assertTrue(isolated.verify_audit()["ok"], isolated.verify_audit())
            primary_probe = '''
import json, sys
from tasktra.autonomy import AutonomyStore
path, attempt_id, digest, lease_token = sys.argv[1:]
store = AutonomyStore(path)
operations = {
 "claim": lambda: store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=digest, repository="repo", revision="rev", branch="main", workspace="work"),
 "heartbeat": lambda: store.heartbeat(attempt_id=attempt_id, performer_id="worker", lease_token=lease_token),
 "finish": lambda: store.finish_attempt(attempt_id=attempt_id, performer_id="worker", lease_token=lease_token, outcome="blocked"),
 "recover": lambda: store.recover_expired_leases(),
 "requeue": lambda: store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=digest, evidence={"decision":"safe"}),
}
result = {}
for name, operation in operations.items():
 try:
  operation(); result[name] = "accepted"
 except Exception as error:
  result[name] = str(error)
print(json.dumps(result, sort_keys=True))
'''
            migrated_bytes = path.read_bytes()
            rejection = json.loads(self._schema12_client(fixture_source, primary_probe, str(path), facts["attempt_id"], facts["digest"], facts["lease_token"]).stdout)
            self.assertTrue(all("requires explicit migration to 12" in value for value in rejection.values()), rejection)
            self.assertEqual(migrated_bytes, path.read_bytes())
            fresh = Path(directory) / "fresh.sqlite"
            StateStore(fresh).create_goal(goal_id="fresh-goal", title="Fresh", description="Fresh", acceptance=["Done."])
            self.assertEqual(self._structure(path), self._structure(fresh))

    def test_streamed_manifest_hash_matches_historical_materialized_order_with_unicode_and_compound_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            with store._connection() as connection:
                connection.execute("INSERT INTO goals(id,title,description,status,priority,authority,acceptance,budget_tokens,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   ("goal-é", "é", "é", "planned", 0, "{}", "[]", None, "t", "t"))
                for unit_id in ("unit-é", "unit-z"):
                    connection.execute("INSERT INTO work_units(id,goal_id,title,status,scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                       (unit_id, "goal-é", unit_id, "planned", "{}", "t", "t"))
                connection.execute("INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)", ("unit-z", "unit-é"))
                materialized = []
                for table, keys in _AUTHORITATIVE_TABLE_KEYS.items():
                    for row in connection.execute(f"SELECT * FROM {table}"):
                        values = [str(dict(row)[key]) for key in keys]
                        row_id = values[0] if len(values) == 1 else _encode(values)
                        materialized.append({"table": table, "row_id": row_id, "row_hash": _authority_row_hash(table, row)})
                materialized.sort(key=lambda item: (item["table"], item["row_id"]))
                historical = sha256(_encode(materialized).encode("utf-8")).hexdigest()
                self.assertEqual(store._state_manifest_hash(connection), historical)

    def test_missing_authoritative_table_remains_a_bounded_state_error_during_streamed_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE intervention_responses")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StateError, "missing an authoritative table"):
                store.create_goal(goal_id="goal-two", title="Later", description="Later", acceptance=["Done."])

    @staticmethod
    def _counts(path: Path) -> dict[str, int]:
        names = ("goals", "work_units", "work_attempts", "transition_approvals", "audit_events")
        connection = sqlite3.connect(path)
        try:
            return {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in names}
        finally:
            connection.close()

    @staticmethod
    def _historical_rows(path: Path) -> dict[str, list[dict]]:
        tables = {
            "goals": "id", "work_units": "id", "work_attempts": "id",
            "transition_approvals": "id", "audit_events": "sequence",
        }
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            result: dict[str, list[dict]] = {}
            for table, order in tables.items():
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
                if table == "work_units":
                    for row in rows:
                        row.pop("current_intervention_id", None)
                        # Schema 15 supplies a conservative policy for every
                        # historical work unit.  Compare the published v12
                        # columns here; policy defaults are asserted by the
                        # schema-15 migration tests.
                        row.pop("verification_policy", None)
                        row.pop("acceptance_checks", None)
                if table == "workflow_evidence":
                    for row in rows:
                        row.pop("completion_evidence_json", None)
                if table == "work_attempts":
                    for row in rows:
                        row.pop("token_accounting_source", None)
                result[table] = rows
            return result
        finally:
            connection.close()

    @staticmethod
    def _structure(path: Path) -> list[tuple[str, str, str | None]]:
        connection = sqlite3.connect(path)
        try:
            return [tuple(row) for row in connection.execute(
                "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
