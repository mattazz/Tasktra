"""Real historical clients exercise both colliding migration histories."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tasktra.schema_history import KNOWN_SCHEMAS, schema_signature
from tests.historical_clients import extract_client, run_client

BASIC = """
import sys
from tasktra.state import StateStore
s=StateStore(sys.argv[1]);s.migrate()
s.create_goal(goal_id='history',title='Historical',description='Historical',acceptance=['Done.'])
s.create_work_unit(goal_id='history',work_unit_id='unit',title='Preserved')
"""

REMOTE_COMPLETION = """
import sys
from datetime import timedelta
from tasktra.autonomy import AutonomyStore,LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tests.test_stage3_autonomy import envelope,NOW,deterministic_review_workflow,deterministic_completion_evidence
s=AutonomyStore(sys.argv[1]);contract=envelope()
contract['allowed_actions'].append('verify-implementation-deterministic-review')
contract['budgets'].update(tokens=15,attempts=3)
s.create_goal(goal_id='goal-1',title='Policy',description='Policy',acceptance=['Done.'])
digest=authority_envelope_sha256(contract)
s.define_goal_contract('goal-1',contract,actor_id='owner',at=NOW)
for action,actor,kind in [('goal-activate','owner','human'),('work-claim','worker','steward'),('work-complete','worker','steward')]:
 s.record_transition_approval(goal_id='goal-1',action=action,effect=LOCAL_REVERSIBLE_WRITE,envelope_sha256=digest,approver_id='approver',approver_kind=kind,performer_id=actor,valid_until=NOW+timedelta(days=1),at=NOW)
s.activate_goal('goal-1',actor_id='owner',envelope_sha256=digest,at=NOW)
s.create_work_unit(goal_id='goal-1',work_unit_id='short',title='Short',scope={'paths':['.'],'exclusions':[]},verification_policy='implementation-deterministic-review',acceptance_checks=[['python','-m','unittest']])
claim=s.claim_next_work(goal_id='goal-1',performer_id='worker',envelope_sha256=digest,repository='repo',revision='rev',branch='main',workspace='work',at=NOW)
s.finish_attempt(attempt_id=claim['attempt_id'],performer_id='worker',lease_token=claim['lease_token'],outcome='success',workflow=deterministic_review_workflow('short'),outcome_evidence=deterministic_completion_evidence(),at=NOW)
s.create_work_unit(goal_id='goal-1',work_unit_id='debt',title='Debt',scope={'paths':['.'],'exclusions':[]})
claim=s.claim_next_work(goal_id='goal-1',performer_id='worker',envelope_sha256=digest,token_reservation=15,repository='repo',revision='rev',branch='main',workspace='work',at=NOW)
s.finish_attempt(attempt_id=claim['attempt_id'],performer_id='worker',lease_token=claim['lease_token'],outcome='exhausted',tokens_consumed=16,observed_token_overrun=True,observed_usage_evidence={'source':'coordinator-attested','execution_ids':['observed-execution'],'total_tokens':16},at=NOW)
"""

LOCAL_RECEIPTS = """
import sqlite3,sys
from tests.test_codex_runs_runtime import CodexRunRuntimeTests
f=CodexRunRuntimeTests();f.setUp()
try:
 run=f.prepare('historical-receipt')
 f.store.record_codex_start(run_id=run['run_id'],observer_id='worker',host_canonical_name=run['requested_task_name'])
 f.store.record_codex_finish(run_id=run['run_id'],observer_id='worker',outcome='completed',result_status='observed',result_sha256='a'*64,usage_status='measured',input_tokens=3,output_tokens=5)
 f.store.drain_goal('goal-one',actor_id='owner')
 source=sqlite3.connect(f.store.path);destination=sqlite3.connect(sys.argv[1])
 try:source.backup(destination)
 finally:destination.close();source.close()
finally:f.tearDown()
"""

def snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [dict(r) for r in connection.execute(f'SELECT * FROM "{name}"')]
                for name in names if name != 'authority_seals' and not name.startswith('sqlite_')}

class ReconciledSchema15Tests(unittest.TestCase):
    def fixture(self, directory, name, program=BASIC):
        directory = Path(directory)
        source = extract_client(name, directory / 'client')
        path = directory / 'runtime.sqlite'
        run_client(source, program, str(path))
        return path

    def assert_records_preserved(self, before, path):
        after = snapshot(path)
        for name, rows in before.items():
            self.assertEqual(len(rows), len(after[name]), name)
            for original, migrated in zip(rows, after[name]):
                self.assertEqual(original, {key: migrated[key] for key in original}, name)

    def test_real_clients_migrate_preserving_original_records_and_seals(self):
        for name, version in [('common10',10), ('remote11',11), ('remote12',12), ('schema12',12), ('schema13',13), ('local14',14)]:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                path = self.fixture(directory, name)
                before = snapshot(path)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertIn(schema_signature(connection), KNOWN_SCHEMAS[version])
                store = StateStore(path)
                evidence = store.migrate_with_evidence()
                self.assertEqual((evidence['before_schema'], evidence['after_schema']), (version, SCHEMA_VERSION))
                self.assertTrue(Path(evidence['backup_path']).is_file())
                self.assert_records_preserved(before, path)
                self.assertTrue(store.verify_audit()['ok'])
                self.assertIsNone(store.migrate_with_evidence()['backup_path'])
                self.assert_records_preserved(before, path)

    def test_real_remote_policy_completion_and_debt_survive(self):
        with TemporaryDirectory() as directory:
            path = self.fixture(directory, 'remote12', REMOTE_COMPLETION)
            before = snapshot(path)
            store = StateStore(path); store.migrate()
            self.assert_records_preserved(before, path)
            self.assertEqual(store.get_work_unit('short')['verification_policy'], 'implementation-deterministic-review')
            self.assertEqual(store.get_work_unit('short')['status'], 'complete')
            self.assertTrue(store.verify_audit()['ok'])
            with store._connection(write=False) as connection:
                StateStore._validate_current_state_for_attestation(connection)

    def test_real_local_receipts_and_draining_survive(self):
        with TemporaryDirectory() as directory:
            path = self.fixture(directory, 'local14', LOCAL_RECEIPTS)
            before = snapshot(path)
            self.assertTrue(before['codex_run_finishes'])
            store = StateStore(path); store.migrate()
            self.assert_records_preserved(before, path)
            self.assertEqual(store.get_goal('goal-one')['status'], 'draining')
            self.assertTrue(store.verify_audit()['ok'])

    def test_missing_or_changed_schema_objects_reject_before_backup(self):
        for mutation in [
            'DROP TRIGGER audit_events_no_update',
            'DROP TRIGGER audit_events_no_update; CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT 1; END;',
            'DROP INDEX work_unit_dependencies_prerequisite',
            'ALTER TABLE goals ADD COLUMN unexpected TEXT',
            'CREATE TABLE unexpected_table(id TEXT PRIMARY KEY)',
            'CREATE TABLE sqlitexunexpected_table(id TEXT PRIMARY KEY)',
        ]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                path = self.fixture(directory, 'local14')
                with closing(sqlite3.connect(path)) as connection:
                    connection.executescript(mutation); connection.commit()
                original = path.read_bytes()
                with self.assertRaises(StateError):
                    StateStore(path).migrate()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob('runtime.sqlite.v*.bak')), [])

    def test_signature_distinguishes_quoted_default_from_sql_expression(self):
        with closing(sqlite3.connect(':memory:')) as expression, closing(sqlite3.connect(':memory:')) as literal:
            expression.execute('CREATE TABLE sample(created TEXT DEFAULT CURRENT_TIMESTAMP)')
            literal.execute('CREATE TABLE sample(created TEXT DEFAULT "current_timestamp")')
            self.assertNotEqual(schema_signature(expression), schema_signature(literal))

    def test_mixed_remote_schema_rejects_before_backup(self):
        with TemporaryDirectory() as directory:
            path = self.fixture(directory, 'remote12')
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE work_unit_dependencies(work_unit_id TEXT, prerequisite_id TEXT)'); connection.commit()
            with self.assertRaisesRegex(StateError, 'complete known lineage'):
                StateStore(path).migrate()
            self.assertEqual(list(path.parent.glob('runtime.sqlite.v*.bak')), [])

    def test_tampered_row_is_not_resealed_by_migration(self):
        with TemporaryDirectory() as directory:
            path = self.fixture(directory, 'remote12')
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("UPDATE work_units SET title='tampered'"); connection.commit()
            original = sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(StateError, 'tampered|unsealed'):
                StateStore(path).migrate()
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), original)
            self.assertEqual(list(path.parent.glob('runtime.sqlite.v*.bak')), [])

    def test_failed_precommit_journal_preserves_old_schema_and_backup(self):
        with TemporaryDirectory() as directory:
            path = self.fixture(directory, 'local14')
            before = snapshot(path)
            def fail(_):
                raise RuntimeError('journal unavailable')
            with self.assertRaisesRegex(RuntimeError, 'journal unavailable'):
                StateStore(path).migrate_with_evidence(before_commit=fail)
            self.assertEqual(StateStore(path).inspect_schema_version(), 14)
            self.assert_records_preserved(before, path)
            self.assertTrue(list(path.parent.glob('runtime.sqlite.v14*.bak')))

