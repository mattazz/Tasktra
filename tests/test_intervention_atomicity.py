"""Failure and retry boundaries for the complete yield transaction."""
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import sqlite3
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError
from tasktra.state import StateStore
from tests import test_intervention_runtime as runtime_fixture


@contextmanager
def leased_fixture():
    fixture = runtime_fixture.InterventionRuntimeTests()
    fixture.setUp()
    try:
        claim = fixture.claim()
        yield fixture, claim, fixture.request(claim)
    finally:
        fixture.tearDown()


class InjectedYieldFailure(RuntimeError):
    pass


class InterventionAtomicityTests(unittest.TestCase):
    def assert_restored(self, fixture, claim, before):
        self.assertEqual(sha256(fixture.store.path.read_bytes()).digest(), before)
        unit = fixture.store.get_work_unit('unit-one')
        self.assertEqual((unit['status'], unit['current_attempt_id']), ('leased', claim['attempt_id']))
        with fixture.store._connection(write=False) as connection:
            self.assertIsNone(connection.execute("SELECT current_intervention_id FROM work_units WHERE id='unit-one'").fetchone()[0])
            self.assertEqual(connection.execute('SELECT count(*) FROM intervention_requests').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT reserved_tokens FROM budgets').fetchone()[0], 5)
        self.assertTrue(fixture.store.verify_audit()['ok'])

    @staticmethod
    def yield_request(fixture, claim, request):
        return fixture.store.yield_for_intervention(
            attempt_id=claim['attempt_id'], performer_id='worker', lease_token=claim['lease_token'],
            request=request, tokens_consumed=2, elapsed_ms=0, at=runtime_fixture.NOW)

    def test_failures_after_each_durable_update_roll_back_the_entire_yield(self):
        stages = {
            'request': 'AFTER INSERT ON intervention_requests',
            'attempt': "AFTER UPDATE ON work_attempts WHEN NEW.status='finished'",
            'budget': 'AFTER UPDATE ON budgets WHEN NEW.consumed_tokens > OLD.consumed_tokens',
            'unit': 'AFTER UPDATE ON work_units WHEN NEW.current_intervention_id IS NOT NULL',
        }
        for stage, trigger in stages.items():
            with self.subTest(stage=stage), leased_fixture() as (fixture, claim, request):
                with fixture.store._connection() as connection:
                    connection.execute(f"CREATE TRIGGER fail_yield {trigger} BEGIN SELECT RAISE(ABORT, 'injected yield failure'); END")
                before = sha256(fixture.store.path.read_bytes()).digest()
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected yield failure'):
                    self.yield_request(fixture, claim, request)
                self.assert_restored(fixture, claim, before)

    def test_failures_after_both_audit_events_roll_back_the_entire_yield(self):
        for event in ('intervention.requested', 'work.finished'):
            with self.subTest(event=event), leased_fixture() as (fixture, claim, request):
                append = fixture.store._append
                reached = []

                def fail_after_append(connection, event_type, **kwargs):
                    result = append(connection, event_type, **kwargs)
                    if event_type == event:
                        reached.append(event_type)
                        raise InjectedYieldFailure('after audit append')
                    return result

                before = sha256(fixture.store.path.read_bytes()).digest()
                with patch.object(fixture.store, '_append', side_effect=fail_after_append):
                    with self.assertRaises(InjectedYieldFailure):
                        self.yield_request(fixture, claim, request)
                self.assertEqual(reached, [event])
                self.assert_restored(fixture, claim, before)

    def test_failure_after_drain_finalization_rolls_back_goal_and_yield(self):
        with leased_fixture() as (fixture, claim, request):
            fixture.store.drain_goal('goal-one', actor_id='owner', at=runtime_fixture.NOW)
            finalize = StateStore._finalize_drain_if_empty_in_transaction
            reached = []

            def fail_after_finalize(*args, **kwargs):
                reached.append(finalize(*args, **kwargs))
                raise InjectedYieldFailure('after drain finalization')

            before = sha256(fixture.store.path.read_bytes()).digest()
            with patch.object(StateStore, '_finalize_drain_if_empty_in_transaction', side_effect=fail_after_finalize):
                with self.assertRaises(InjectedYieldFailure):
                    self.yield_request(fixture, claim, request)
            self.assertEqual(reached, [True])
            self.assert_restored(fixture, claim, before)
            self.assertEqual(fixture.store.get_goal('goal-one')['status'], 'draining')

    def test_failure_after_state_sealing_before_commit_rolls_back_everything(self):
        with leased_fixture() as (fixture, claim, request):
            seal = StateStore._seal_current_state_in_transaction
            reached = []

            def fail_after_seal(*args, **kwargs):
                seal(*args, **kwargs)
                reached.append(True)
                raise InjectedYieldFailure('after state sealing')

            before = sha256(fixture.store.path.read_bytes()).digest()
            with patch.object(StateStore, '_seal_current_state_in_transaction', side_effect=fail_after_seal):
                with self.assertRaises(InjectedYieldFailure):
                    self.yield_request(fixture, claim, request)
            self.assertEqual(reached, [True])
            self.assert_restored(fixture, claim, before)

    def test_retry_binds_usage_and_explicit_versus_measured_input_mode(self):
        for initial_elapsed in (None, 2000):
            with self.subTest(initial_elapsed=initial_elapsed), leased_fixture() as (fixture, claim, request):
                arguments = dict(attempt_id=claim['attempt_id'], performer_id='worker',
                                 lease_token=claim['lease_token'], request=request, tokens_consumed=2,
                                 elapsed_ms=initial_elapsed)
                committed = fixture.store.yield_for_intervention(**arguments, at=runtime_fixture.NOW + timedelta(seconds=2))
                self.assertEqual(committed['accounted_elapsed_ms'], 2000)
                before = sha256(fixture.store.path.read_bytes()).digest()
                exact = fixture.store.yield_for_intervention(**arguments, at=runtime_fixture.NOW + timedelta(seconds=3))
                self.assertEqual((exact['mutation'], exact['idempotent'], exact['accounted_elapsed_ms']), ('none', True, 2000))
                self.assertEqual(sha256(fixture.store.path.read_bytes()).digest(), before)
                changed_inputs = ({'tokens_consumed': 3}, {'elapsed_ms': 3000},
                                  {'elapsed_ms': 2000 if initial_elapsed is None else None})
                for changed in changed_inputs:
                    with self.subTest(changed=changed), self.assertRaisesRegex(AutonomyError, 'idempotency_conflict'):
                        fixture.store.yield_for_intervention(**(arguments | changed), at=runtime_fixture.NOW + timedelta(seconds=3))
                    self.assertEqual(sha256(fixture.store.path.read_bytes()).digest(), before)
                with fixture.store._connection(write=False) as connection:
                    budget = connection.execute('SELECT consumed_tokens,consumed_elapsed_ms,reserved_tokens FROM budgets').fetchone()
                    self.assertEqual(tuple(budget), (2, 2000, 0))
                self.assertTrue(fixture.store.verify_audit()['ok'])
