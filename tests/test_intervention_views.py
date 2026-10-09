from datetime import timedelta
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra import intervention_views
from tasktra.interventions import (
    intervention_detail, intervention_inbox, intervention_response_history,
    intervention_request_sha256, intervention_response_sha256,
)
from tasktra.operations import operational_status
from tasktra.overview import orchestration_overview
from tasktra.state import StateError
from tests.test_stage3_autonomy import NOW, envelope
from tests.test_intervention_contracts import request, response


class InterventionViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = AutonomyStore(Path(self.directory.name) / 'state.sqlite')
        self.store.create_goal(goal_id='goal-1', title='Goal', description='Goal', acceptance=['Done.'])
        contract = envelope()
        contract['budgets'].update(tokens=None, attempts=100, elapsed_seconds=3600, concurrency=10)
        self.digest = authority_envelope_sha256(contract)
        self.store.define_goal_contract('goal-1', contract, actor_id='owner', at=NOW)
        for action, performer, kind in [('goal-activate', 'owner', 'human'), ('work-claim', 'worker', 'steward'), ('work-requeue', 'worker', 'steward')]:
            self.store.record_transition_approval(
                goal_id='goal-1', action=action, effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=self.digest, approver_id='approver', approver_kind=kind,
                performer_id=performer, valid_until=NOW + timedelta(days=1), at=NOW,
            )
        self.store.activate_goal('goal-1', actor_id='owner', envelope_sha256=self.digest, at=NOW)

    def unit(self, identifier, prerequisites=()):
        self.store.create_work_unit(goal_id='goal-1', work_unit_id=identifier, title=identifier,
                                    scope={'paths': ['src'], 'exclusions': []}, prerequisite_ids=list(prerequisites))

    def claim(self):
        return self.store.claim_next_work(goal_id='goal-1', performer_id='worker', envelope_sha256=self.digest,
                                          repository='repo', revision='revision', branch='main', workspace='workspace',
                                          lease_seconds=60, at=NOW)

    def yielded(self, identifier, *, approval=False):
        self.unit(identifier)
        claim = self.claim()
        self.assertEqual(claim['work_unit_id'], identifier)
        value = request()
        value.update(request_id='request-' + identifier, outcome_class='approval-required' if approval else 'blocked',
                     requires_human_approval=approval)
        value['source'] = {'goal_id': 'goal-1', 'work_unit_id': identifier, 'attempt_id': claim['attempt_id']}
        value['producer'] = {'actor_id': 'worker'}
        value['evidence_refs'][0]['locator'] = 'python -m unittest private_fixture_name'
        self.store.yield_for_intervention(attempt_id=claim['attempt_id'], performer_id='worker',
                                          lease_token=claim['lease_token'], request=value, at=NOW)
        return value

    def answer(self, value, identifier, *, disposition='answered', previous=None):
        reply = response()
        reply.update(response_id=identifier, disposition=disposition,
                     request={'request_id': value['request_id'], 'request_sha256': intervention_request_sha256(value)},
                     expected_current_response=None if previous is None else {
                         'response_id': previous['response_id'], 'response_sha256': intervention_response_sha256(previous),
                     })
        reply['answer'] = 'Full private answer visible in explicit detail.'
        self.store.record_intervention_response(response=reply, responder_id='operator-one', responder_kind='human', at=NOW)
        return reply

    def test_missing_empty_and_bounds_are_noncreating(self):
        missing = AutonomyStore(Path(self.directory.name) / 'missing.sqlite')
        with self.assertRaisesRegex(StateError, 'does not exist'):
            intervention_inbox(missing)
        self.assertFalse(missing.path.exists())
        before = self.store.path.read_bytes()
        result = intervention_inbox(self.store, at=NOW)
        self.assertEqual(result['pagination']['total'], 0)
        self.assertEqual(result['items'], [])
        self.assertEqual(self.store.path.read_bytes(), before)
        for kwargs in ({'limit': 0}, {'limit': True}, {'offset': 1_000_001}, {'include_closed': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StateError):
                intervention_inbox(self.store, **kwargs)

    def test_inbox_minimizes_evidence_and_overview_only_exposes_counts(self):
        value = self.yielded('a')
        self.unit('dependent', prerequisites=['a'])
        self.answer(value, 'answer-a')
        before = self.store.path.read_bytes()
        inbox = intervention_inbox(self.store, at=NOW + timedelta(seconds=5))
        item = inbox['items'][0]
        self.assertEqual(item['age_seconds'], 5)
        self.assertEqual(item['incomplete_direct_dependents_count'], 1)
        self.assertEqual(item['response_state'], 'answered')
        self.assertEqual(set(item['evidence_refs'][0]), {'id', 'kind', 'summary'})
        self.assertNotIn('private_fixture_name', json.dumps(inbox))
        self.assertNotIn('Full private answer', json.dumps(inbox))
        detail = intervention_detail(self.store, value['request_id'], at=NOW)
        self.assertIn('private_fixture_name', json.dumps(detail))
        self.assertIn('Full private answer', json.dumps(detail))
        for projection in (orchestration_overview(self.store), operational_status(self.store)):
            self.assertNotIn(value['prompt'], json.dumps(projection))
            self.assertNotIn('private_fixture_name', json.dumps(projection))
        overview = orchestration_overview(self.store, goal_id='goal-1')
        self.assertEqual(overview['goal']['interventions']['answered'], 1)
        self.assertIn('intervention-answered', [row['code'] for row in overview['goal']['attention']])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_closed_pages_and_direct_impact_use_indexes_without_history_sorting(self):
        value = self.yielded('closed-unit')
        reply = self.answer(value, 'closed-answer')
        self.store.requeue_work(work_unit_id='closed-unit', performer_id='worker', envelope_sha256=self.digest,
            evidence={'decision': 'Use the reviewed answer.'}, intervention_request_id=value['request_id'],
            expected_intervention_response_id=reply['response_id'],
            expected_intervention_response_sha256=intervention_response_sha256(reply), at=NOW)
        original_connection = self.store._connection
        for filters in ({}, {'goal_id': 'goal-1'}, {'work_unit_id': 'closed-unit'}):
            with self.subTest(filters=filters):
                statements = []

                @contextmanager
                def traced_connection(*args, **kwargs):
                    with original_connection(*args, **kwargs) as connection:
                        connection.set_trace_callback(statements.append)
                        yield connection

                before = self.store.path.read_bytes()
                with patch.object(self.store, '_connection', traced_connection):
                    page = intervention_inbox(self.store, include_closed=True, include_legacy=False,
                                              limit=1, at=NOW, **filters)
                self.assertEqual([item['request_id'] for item in page['items']], [value['request_id']])
                self.assertEqual(self.store.path.read_bytes(), before)
                closed_queries = [query for query in statements if "SELECT 'structured-request' AS kind,c.request_id AS id" in query]
                impact_queries = [query for query in statements if query.startswith('SELECT count(*) FROM work_unit_dependencies d JOIN work_units u')]
                self.assertEqual(len(closed_queries), 1)
                self.assertEqual(len(impact_queries), 1)
                with original_connection(write=False) as connection:
                    closed_plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + closed_queries[0])]
                    impact_plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + impact_queries[0])]
                self.assertTrue(any('intervention_closures_closed_request' in line for line in closed_plan), closed_plan)
                self.assertFalse(any('TEMP B-TREE' in line or line == 'SCAN c' for line in closed_plan), closed_plan)
                self.assertTrue(any('work_unit_dependencies_prerequisite' in line for line in impact_plan), impact_plan)

    def test_pages_cross_current_legacy_and_closed_category_boundaries(self):
        self.yielded('a-current')
        self.unit('b-legacy')
        claim = self.claim()
        self.store.finish_attempt(attempt_id=claim['attempt_id'], performer_id='worker',
            lease_token=claim['lease_token'], outcome='blocked', at=NOW)
        closed = self.yielded('c-closed')
        reply = self.answer(closed, 'closed-answer')
        self.store.requeue_work(work_unit_id='c-closed', performer_id='worker', envelope_sha256=self.digest,
            evidence={'decision': 'Use the reviewed answer.'}, intervention_request_id=closed['request_id'],
            expected_intervention_response_id=reply['response_id'],
            expected_intervention_response_sha256=intervention_response_sha256(reply), at=NOW)
        full = intervention_inbox(self.store, include_closed=True, limit=20, at=NOW)
        self.assertEqual([item['work_unit_id'] for item in full['items']], ['a-current', 'b-legacy', 'c-closed'])
        for offset in range(5):
            for limit in (1, 2, 3):
                with self.subTest(offset=offset, limit=limit):
                    page = intervention_inbox(self.store, include_closed=True, limit=limit, offset=offset, at=NOW)
                    self.assertEqual(page['items'], full['items'][offset:offset + limit])
                    self.assertEqual(page['pagination']['total'], 3)
                    self.assertEqual(page['pagination']['has_more'], offset + len(page['items']) < 3)
    def test_binary_order_pages_filters_and_legacy_rows(self):
        first = self.yielded('a')
        self.yielded('b', approval=True)
        self.yielded('c')
        self.answer(first, 'answer-a')
        self.unit('z-legacy')
        claimed = self.claim()
        self.store.finish_attempt(attempt_id=claimed['attempt_id'], performer_id='worker',
                                  lease_token=claimed['lease_token'], outcome='blocked', at=NOW)
        pages = [intervention_inbox(self.store, limit=1, offset=index, at=NOW) for index in range(4)]
        self.assertEqual([page['items'][0]['work_unit_id'] for page in pages], ['b', 'c', 'a', 'z-legacy'])
        self.assertTrue(all(page['pagination']['total'] == 4 for page in pages))
        self.assertFalse(pages[-1]['pagination']['has_more'])
        self.assertEqual(pages[-1]['items'][0]['response_state'], 'unstructured')
        self.assertEqual(intervention_inbox(self.store, include_legacy=False)['pagination']['total'], 3)
        filtered = intervention_inbox(self.store, goal_id='goal-1', work_unit_id='a')
        self.assertEqual(filtered['pagination']['total'], 1)
        for kwargs in ({'goal_id': 'missing'}, {'work_unit_id': 'missing'}):
            with self.assertRaises(StateError):
                intervention_inbox(self.store, **kwargs)

    def test_revision_history_and_closed_request_remain_inspectable(self):
        value = self.yielded('a')
        first = self.answer(value, 'answer-1', disposition='declined')
        second = self.answer(value, 'answer-2', disposition='cancelled', previous=first)
        third = self.answer(value, 'answer-3', previous=second)
        page = intervention_response_history(self.store, value['request_id'], limit=2, at=NOW)
        self.assertEqual([item['disposition'] for item in page['responses']], ['declined', 'cancelled'])
        self.assertEqual(page['pagination'], {'limit': 2, 'after_revision': 0, 'total': 3, 'returned': 2, 'has_more': True, 'next_after_revision': 2})
        last = intervention_response_history(self.store, value['request_id'], after_revision=2, limit=2, at=NOW)
        self.assertEqual(last['responses'][0]['id'], 'answer-3')
        self.assertFalse(last['pagination']['has_more'])
        self.store.requeue_work(work_unit_id='a', performer_id='worker', envelope_sha256=self.digest, evidence={'reason': 'Reviewed answer.'},
                                intervention_request_id=value['request_id'], expected_intervention_response_id=third['response_id'],
                                expected_intervention_response_sha256=intervention_response_sha256(third), at=NOW)
        self.assertEqual(intervention_inbox(self.store)['pagination']['total'], 0)
        history = intervention_inbox(self.store, include_closed=True, at=NOW)
        self.assertEqual(history['aggregates']['closed'], 1)
        self.assertFalse(history['items'][0]['current'])
        self.assertEqual(history['items'][0]['closure']['response_id'], 'answer-3')
        self.assertEqual(intervention_detail(self.store, value['request_id'])['request']['response_revision_count'], 3)

    def test_capture_does_not_mix_response_committed_between_queries(self):
        value = self.yielded('a')
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        original = intervention_views._request_item
        wrote = False

        def update_then_read(*args, **kwargs):
            nonlocal wrote
            if not wrote:
                wrote = True
                self.answer(value, 'answer-concurrent')
            return original(*args, **kwargs)

        with patch.object(intervention_views, '_request_item', side_effect=update_then_read):
            snapshot = intervention_inbox(self.store, at=NOW)
        self.assertEqual(snapshot['aggregates']['open'], 1)
        self.assertEqual(snapshot['items'][0]['response_state'], 'open')
        self.assertIsNone(snapshot['items'][0]['response'])
        self.assertEqual(intervention_inbox(self.store)['aggregates']['answered'], 1)

    def test_tampered_current_pointer_fails_before_projection(self):
        self.yielded('a')
        with closing(sqlite3.connect(self.store.path)) as connection:
            with connection:
                connection.execute("UPDATE work_units SET current_intervention_id=NULL WHERE id='a'")
        with self.assertRaises(StateError):
            intervention_inbox(self.store)


if __name__ == '__main__':
    unittest.main()
