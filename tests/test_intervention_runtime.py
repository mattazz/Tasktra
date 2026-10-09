from datetime import datetime, timedelta, timezone
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, InterventionConflictError, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import AUTHORITY_ENVELOPE_KIND, AUTHORITY_ENVELOPE_VERSION, authority_envelope_sha256
from tasktra.interventions import (
    canonical_intervention_request,
    canonical_intervention_response,
    intervention_request_sha256,
    intervention_response_sha256,
)
from tasktra.state import StateError, StateStore


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def envelope():
    return {
        "kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION,
        "goal_id": "goal-one", "outcome": "Bounded work.", "motivation": "Test.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "work-claim", "work-requeue"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": ["Test."],
        "budgets": {"tokens": 20, "attempts": 3, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": ["Stop."], "escalation_conditions": ["Escalate."],
    }


class InterventionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = AutonomyStore(f"{self.directory.name}/state.sqlite")
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        self.envelope = envelope()
        self.digest = authority_envelope_sha256(self.envelope)
        self.store.define_goal_contract("goal-one", self.envelope, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)

    def tearDown(self):
        self.directory.cleanup()

    def claim(self):
        return self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest,
                                          token_reservation=5, lease_seconds=10, repository="repo", revision="rev", branch="main", workspace="work", at=NOW)

    def request(self, claim):
        return {
            "kind": "tasktra.intervention-request", "version": 1, "request_id": "request-one",
            "source": {"goal_id": "goal-one", "work_unit_id": "unit-one", "attempt_id": claim["attempt_id"]},
            "producer": {"actor_id": "worker"}, "outcome_class": "blocked",
            "prompt": "Choose one option.", "rationale": "A decision is needed.", "impact": "Work remains blocked.",
            "requires_human_approval": False, "evidence_refs": [],
        }

    def response(self, request, *, response_id="response-one", expected=None, disposition="answered"):
        return {
            "kind": "tasktra.intervention-response", "version": 1, "response_id": response_id,
            "request": {"request_id": request["request_id"], "request_sha256": intervention_request_sha256(request)},
            "expected_current_response": expected, "responder": {"kind": "human", "actor_id": "operator"},
            "disposition": disposition, "answer": "Use the safe option.", "rationale": "It is approved.", "evidence_refs": [],
        }

    def test_attestation_rejects_unbound_intervention_without_writing_audit(self):
        claim = self.claim()
        request = self.request(claim)
        encoded = canonical_intervention_request(request)
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                """INSERT INTO intervention_requests(
                    id,version,goal_id,work_unit_id,attempt_id,producer_id,outcome_class,
                    request_json,request_sha256,yield_tokens_consumed,yield_elapsed_input_mode,
                    yield_elapsed_input_ms,yield_accounted_elapsed_ms,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request["request_id"], request["version"], "goal-one", "unit-one", claim["attempt_id"], "worker",
                    request["outcome_class"], encoded, intervention_request_sha256(request), 0, "measured", None, 0,
                    "2030-01-01T00:00:00Z",
                ),
            )
            connection.commit()
            before = (
                connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
                connection.execute("SELECT count(*) FROM authority_seals").fetchone()[0],
            )
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "request is not bound to its finished attempt"):
            self.store.attest_ledger(actor_id="human", at=NOW)
        connection = sqlite3.connect(self.store.path)
        try:
            after = (
                connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
                connection.execute("SELECT count(*) FROM authority_seals").fetchone()[0],
            )
        finally:
            connection.close()
        self.assertEqual(after, before)

    def _assert_attestation_rejects_response_expected_head(self, *, with_predecessor: bool) -> None:
        claim = self.claim()
        request = self.request(claim)
        self.store.yield_for_intervention(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            request=request, tokens_consumed=0, elapsed_ms=0, at=NOW,
        )
        predecessor = None
        if with_predecessor:
            first = self.response(request)
            predecessor = self.store.record_intervention_response(
                response=first, responder_id="operator", responder_kind="human", at=NOW,
            )
        wrong_expected = {"response_id": "nonexistent-head", "response_sha256": "a" * 64}
        response = self.response(
            request,
            response_id="response-two" if predecessor else "response-one",
            expected=wrong_expected,
        )
        encoded = canonical_intervention_response(response)
        response_sha256 = intervention_response_sha256(response)
        connection = sqlite3.connect(self.store.path)
        connection.row_factory = sqlite3.Row
        try:
            revision_no = 2 if predecessor else 1
            previous_id = None if predecessor is None else predecessor["response_id"]
            previous_sha256 = None if predecessor is None else predecessor["response_sha256"]
            connection.execute(
                """INSERT INTO intervention_responses(
                    id,version,request_id,request_sha256,revision_no,previous_response_id,
                    expected_previous_sha256,responder_kind,responder_id,disposition,response_json,
                    response_sha256,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    response["response_id"], response["version"], request["request_id"], intervention_request_sha256(request),
                    revision_no, previous_id, previous_sha256, "human", "operator", "answered", encoded,
                    response_sha256, "2030-01-01T00:00:00Z",
                ),
            )
            if predecessor is None:
                connection.execute(
                    "INSERT INTO intervention_response_heads VALUES(?,?,?,?,?)",
                    (request["request_id"], response["response_id"], response_sha256, revision_no, "2030-01-01T00:00:00Z"),
                )
            else:
                connection.execute(
                    "UPDATE intervention_response_heads SET current_response_id=?,current_response_sha256=?,revision_no=?,updated_at=? WHERE request_id=?",
                    (response["response_id"], response_sha256, revision_no, "2030-01-01T00:00:00Z", request["request_id"]),
                )
            StateStore._append_event_in_transaction(
                connection, "intervention.responded", goal_id="goal-one", work_unit_id="unit-one",
                payload={
                    "request_id": request["request_id"], "request_sha256": intervention_request_sha256(request),
                    "response_id": response["response_id"], "response_sha256": response_sha256,
                    "revision_no": revision_no, "previous_response_id": previous_id,
                    "previous_response_sha256": previous_sha256, "responder_id": "operator",
                    "responder_kind": "human", "disposition": "answered", "timestamp": "2030-01-01T00:00:00Z",
                },
            )
            connection.commit()
            before = (
                connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
                connection.execute("SELECT count(*) FROM authority_seals").fetchone()[0],
            )
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "response chain is invalid"):
            self.store.attest_ledger(actor_id="human", at=NOW)
        connection = sqlite3.connect(self.store.path)
        try:
            after = (
                connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
                connection.execute("SELECT count(*) FROM authority_seals").fetchone()[0],
            )
        finally:
            connection.close()
        self.assertEqual(after, before)

    def test_attestation_rejects_first_response_with_mismatched_expected_head(self):
        self._assert_attestation_rejects_response_expected_head(with_predecessor=False)

    def test_attestation_rejects_revision_with_mismatched_expected_head(self):
        self._assert_attestation_rejects_response_expected_head(with_predecessor=True)

    def test_yield_response_revision_and_exact_structured_requeue(self):
        claim = self.claim()
        request = self.request(claim)
        yielded = self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                                    request=request, tokens_consumed=2, elapsed_ms=0, at=NOW)
        self.assertEqual((yielded["status"], yielded["mutation"]), ("blocked", "applied"))
        retry = self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                                  request=request, tokens_consumed=2, elapsed_ms=0, at=NOW)
        self.assertTrue(retry["idempotent"])
        first = self.response(request)
        recorded = self.store.record_intervention_response(response=first, responder_id="operator", responder_kind="human", at=NOW)
        second = self.response(request, response_id="response-two", expected={"response_id": recorded["response_id"], "response_sha256": recorded["response_sha256"]})
        revised = self.store.record_intervention_response(response=second, responder_id="operator", responder_kind="human", at=NOW)
        old_response_retry = self.store.record_intervention_response(response=first, responder_id="operator", responder_kind="human", at=NOW)
        self.assertTrue(old_response_retry["idempotent"])
        self.assertFalse(old_response_retry["current"])
        self.assertEqual(old_response_retry["current_intervention_id"], "request-one")
        self.assertIsNone(old_response_retry["current_attempt_id"])
        self.assertEqual(old_response_retry["stale_reason"], "unit_advanced")
        with self.assertRaises(InterventionConflictError) as caught:
            self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digest, evidence={"decision": "safe"},
                                    intervention_request_id="request-one", expected_intervention_response_id=recorded["response_id"],
                                    expected_intervention_response_sha256=recorded["response_sha256"], at=NOW)
        self.assertEqual(caught.exception.code, "response_head_changed")
        self.assertEqual(caught.exception.details, {"current_response_id": revised["response_id"], "current_response_sha256": revised["response_sha256"]})
        requeued = self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digest, evidence={"decision": "safe"},
                                           intervention_request_id="request-one", expected_intervention_response_id=revised["response_id"],
                                           expected_intervention_response_sha256=revised["response_sha256"], at=NOW)
        self.assertEqual(requeued["status"], "eligible")
        replay = self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digest, evidence={"decision": "safe"},
                                         intervention_request_id="request-one", expected_intervention_response_id=revised["response_id"],
                                         expected_intervention_response_sha256=revised["response_sha256"], at=NOW)
        self.assertTrue(replay["idempotent"])
        self.assertFalse(replay["current"])
        newer = self.claim()
        stale_replay = self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digest, evidence={"decision": "safe"},
                                               intervention_request_id="request-one", expected_intervention_response_id=revised["response_id"],
                                               expected_intervention_response_sha256=revised["response_sha256"], at=NOW)
        self.assertTrue(stale_replay["idempotent"])
        self.assertFalse(stale_replay["current"])
        self.assertEqual(stale_replay["current_attempt_id"], newer["attempt_id"])
        historical_response = self.store.record_intervention_response(response=first, responder_id="operator", responder_kind="human", at=NOW)
        self.assertTrue(historical_response["idempotent"])
        self.assertEqual(historical_response["current_attempt_id"], newer["attempt_id"])
        self.assertIsNone(historical_response["current_intervention_id"])
        self.assertEqual(historical_response["stale_reason"], "unit_advanced")
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_yield_rolls_back_when_accounting_cannot_fit(self):
        claim = self.claim()
        request = self.request(claim)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(AutonomyError, "non-negative"):
            self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                              request=request, elapsed_ms=-1, at=NOW)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.store.get_work_unit("unit-one")["status"], "leased")

    def test_yield_database_failure_rolls_back_request_and_lease_release(self):
        claim = self.claim()
        request = self.request(claim)
        with self.store._connection() as connection:
            connection.execute("CREATE TRIGGER abort_intervention_insert BEFORE INSERT ON intervention_requests BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=NOW)
        with self.store._connection(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM intervention_requests").fetchone()[0], 0)
        with self.store._connection() as connection:
            connection.execute("DROP TRIGGER abort_intervention_insert")
        unit = self.store.get_work_unit("unit-one")
        self.assertEqual((unit["status"], unit["current_attempt_id"]), ("leased", claim["attempt_id"]))

    def test_yield_rejects_the_current_lease_secret_in_every_request_text_boundary(self):
        for location in ("prompt", "embedded", "locator"):
            with self.subTest(location=location):
                claim = self.claim()
                request = self.request(claim)
                if location == "prompt":
                    request["prompt"] = claim["lease_token"]
                elif location == "embedded":
                    request["impact"] = f"Do not store {claim['lease_token']} here."
                else:
                    request["evidence_refs"] = [{"id": "proof", "kind": "command", "locator": claim["lease_token"], "summary": "Evidence."}]
                with self.assertRaisesRegex(AutonomyError, "supplied lease token"):
                    self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=NOW)
                with self.store._connection(write=False) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM intervention_requests").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM work_attempts WHERE status='leased'").fetchone()[0], 1)
                self.tearDown()
                self.setUp()

    def test_response_rejects_exact_and_embedded_lease_tokens_without_persisting_them(self):
        claim = self.claim()
        request = self.request(claim)
        self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=NOW)
        for answer in (claim["lease_token"], f"Use {claim['lease_token']} now."):
            response = self.response(request)
            response["answer"] = answer
            with self.assertRaisesRegex(AutonomyError, "lease token"):
                self.store.record_intervention_response(response=response, responder_id="operator", responder_kind="human", at=NOW)
        with self.store._connection(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM intervention_responses").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
