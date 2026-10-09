"""End-to-end acceptance checks for the durable intervention seam.

These deliberately use separate stores over one database where an operator and
worker would otherwise race.  The lower-level runtime tests cover individual
transactions; this module protects the bindings between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import contextmanager
from threading import Event, Thread
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import AUTHORITY_ENVELOPE_KIND, AUTHORITY_ENVELOPE_VERSION, authority_envelope_sha256
from tasktra.interventions import (
    canonical_intervention_request,
    canonical_intervention_response,
    intervention_inbox,
    intervention_request_sha256,
    intervention_response_history,
    intervention_response_sha256,
)
from tasktra.state import StateError


NOW = datetime(2034, 1, 1, tzinfo=timezone.utc)


def envelope(goal_id: str) -> dict:
    return {
        "kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION,
        "goal_id": goal_id, "outcome": "Exercise intervention recovery.", "motivation": "Acceptance test.",
        "author_id": "owner", "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "goal-resume", "work-claim", "work-requeue"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 100000, "attempts": 10000, "elapsed_seconds": 3600, "concurrency": 8},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


class InterventionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.store = AutonomyStore(self.path)
        self.digests: dict[str, str] = {}
        self.add_goal("goal-one")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def add_goal(self, goal_id: str) -> None:
        contract = envelope(goal_id)
        digest = authority_envelope_sha256(contract)
        self.digests[goal_id] = digest
        self.store.create_goal(goal_id=goal_id, title=goal_id, description=goal_id, acceptance=["Done."])
        self.store.define_goal_contract(goal_id, contract, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id=goal_id, action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id=goal_id, action="goal-resume", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id=goal_id, action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.activate_goal(goal_id, actor_id="owner", envelope_sha256=digest, at=NOW)

    def add_unit(self, unit_id: str, *, goal_id: str = "goal-one", requeue: bool = True) -> None:
        self.store.create_work_unit(goal_id=goal_id, work_unit_id=unit_id, title=unit_id, scope={"paths": ["src"], "exclusions": []})
        if requeue:
            self.store.record_transition_approval(goal_id=goal_id, work_unit_id=unit_id, action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE,
                                                  envelope_sha256=self.digests[goal_id], approver_id="steward", approver_kind="steward",
                                                  performer_id="worker", valid_until=NOW + timedelta(days=1), at=NOW)

    def claim(self, *, goal_id: str = "goal-one", at: datetime = NOW, lease_seconds: int = 60) -> dict:
        result = self.store.claim_next_work(goal_id=goal_id, performer_id="worker", envelope_sha256=self.digests[goal_id],
                                            repository="repo", revision="revision", branch="main", workspace="workspace", lease_seconds=lease_seconds, at=at)
        self.assertIsNotNone(result)
        return result

    @staticmethod
    def request(claim: dict, request_id: str) -> dict:
        return {
            "kind": "tasktra.intervention-request", "version": 1, "request_id": request_id,
            "source": {"goal_id": claim["goal_id"], "work_unit_id": claim["work_unit_id"], "attempt_id": claim["attempt_id"]},
            "producer": {"actor_id": "worker"}, "outcome_class": "blocked", "prompt": "Choose a safe option.",
            "rationale": "A bounded operator decision is needed.", "impact": "Work remains blocked.",
            "requires_human_approval": False, "evidence_refs": [],
        }

    @staticmethod
    def response(request: dict, response_id: str, *, expected: dict | None = None, disposition: str = "answered") -> dict:
        return {
            "kind": "tasktra.intervention-response", "version": 1, "response_id": response_id,
            "request": {"request_id": request["request_id"], "request_sha256": intervention_request_sha256(request)},
            "expected_current_response": expected, "responder": {"kind": "human", "actor_id": "operator"},
            "disposition": disposition, "answer": "Use the reviewed option.", "rationale": "Recorded decision.", "evidence_refs": [],
        }

    def yield_request(self, claim: dict, request_id: str) -> tuple[dict, dict]:
        request = self.request(claim, request_id)
        yielded = self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=NOW)
        self.assertEqual((yielded["mutation"], yielded["current"]), ("applied", True))
        return request, yielded

    def answer(self, request: dict, response_id: str, *, expected: dict | None = None) -> dict:
        return self.store.record_intervention_response(response=self.response(request, response_id, expected=expected), responder_id="operator", responder_kind="human", at=NOW)

    def requeue(self, unit_id: str, request_id: str, response: dict, *, goal_id: str = "goal-one") -> dict:
        return self.store.requeue_work(work_unit_id=unit_id, performer_id="worker", envelope_sha256=self.digests[goal_id], evidence={"decision": "reviewed"},
                                       intervention_request_id=request_id, expected_intervention_response_id=response["response_id"],
                                       expected_intervention_response_sha256=response["response_sha256"], at=NOW)

    def build_large_verified_fixture(self, *, closed_count: int = 2_000, revisions: int = 1_024) -> str:
        """Build immutable rows in one trusted transaction, then seal and verify them.

        Public transitions above establish a real schema-13 ledger.  This helper
        only avoids repeating complete-ledger validation and sealing thousands
        of times while constructing an invariant-equivalent volume fixture.
        It never suppresses the final audit/current-state verification or any
        public projection under test.
        """
        timestamp = "2034-01-01T00:00:00Z"
        evidence_sha256 = sha256(json.dumps({"decision": "reviewed"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        def insert_unit(connection, *, unit_id: str, status: str) -> None:
            connection.execute(
                """INSERT INTO work_units(id,goal_id,title,status,scope,lease_holder,lease_expires_at,created_at,updated_at,
                   current_attempt_id,attempt_count,retry_at,last_outcome_class,checkpoint_id,current_intervention_id)
                   VALUES(?,?,?,?,'{"paths":["src"],"exclusions":[]}',NULL,NULL,?,?,NULL,1,NULL,'blocked',NULL,?)""",
                (unit_id, "goal-one", unit_id, status, timestamp, timestamp, None),
            )

        def insert_attempt(connection, *, unit_id: str, attempt_id: str, request_id: str, request_sha256: str) -> None:
            outcome = json.dumps({
                "intervention_request_id": request_id, "request_sha256": request_sha256,
                "tokens_consumed": 0, "elapsed_input_mode": "measured", "elapsed_input_ms": None,
                "accounted_elapsed_ms": 0,
            }, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,repository,revision,branch,workspace,
                   acquired_at,heartbeat_at,expires_at,ended_at,status,outcome_class,outcome_json,tokens_reserved,tokens_consumed,elapsed_ms)
                   VALUES(?,?,1,'worker',1,?,'repo','revision','main','workspace',?,?,?,?, 'finished','blocked',?,0,0,0)""",
                (attempt_id, unit_id, sha256(b"fixture-token").hexdigest(), timestamp, timestamp, timestamp, timestamp, outcome),
            )
            connection.execute("UPDATE budgets SET consumed_attempts=consumed_attempts+1 WHERE goal_id='goal-one'")

        def insert_request(connection, request: dict, *, closed: bool) -> tuple[str, str]:
            request_json = canonical_intervention_request(request)
            request_sha = intervention_request_sha256(request)
            insert_unit(connection, unit_id=request["source"]["work_unit_id"], status="eligible" if closed else "blocked")
            insert_attempt(connection, unit_id=request["source"]["work_unit_id"], attempt_id=request["source"]["attempt_id"],
                           request_id=request["request_id"], request_sha256=request_sha)
            connection.execute(
                """INSERT INTO intervention_requests(id,version,goal_id,work_unit_id,attempt_id,producer_id,outcome_class,request_json,request_sha256,
                   yield_tokens_consumed,yield_elapsed_input_mode,yield_elapsed_input_ms,yield_accounted_elapsed_ms,created_at)
                   VALUES(?,1,'goal-one',?,?,?,'blocked',?,?,0,'measured',NULL,0,?)""",
                (request["request_id"], request["source"]["work_unit_id"], request["source"]["attempt_id"], "worker", request_json, request_sha, timestamp),
            )
            if not closed:
                connection.execute("UPDATE work_units SET current_intervention_id=? WHERE id=?", (request["request_id"], request["source"]["work_unit_id"]))
            self.store._append_event_in_transaction(connection, "intervention.requested", goal_id="goal-one", work_unit_id=request["source"]["work_unit_id"], payload={
                "request_id": request["request_id"], "request_sha256": request_sha, "attempt_id": request["source"]["attempt_id"],
                "producer_id": "worker", "outcome_class": "blocked", "requires_human_approval": False, "timestamp": timestamp,
            })
            self.store._append_event_in_transaction(connection, "work.finished", goal_id="goal-one", work_unit_id=request["source"]["work_unit_id"], payload={
                "attempt_id": request["source"]["attempt_id"], "outcome": "blocked", "request_id": request["request_id"],
                "request_sha256": request_sha, "timestamp": timestamp,
            })
            return request_json, request_sha

        def insert_response(connection, response: dict, *, revision_no: int, previous: dict | None) -> dict:
            response_json = canonical_intervention_response(response)
            response_sha = intervention_response_sha256(response)
            connection.execute(
                """INSERT INTO intervention_responses(id,version,request_id,request_sha256,revision_no,previous_response_id,expected_previous_sha256,
                   responder_kind,responder_id,disposition,response_json,response_sha256,created_at)
                   VALUES(?,1,?,?,?,?,?,'human','operator','answered',?,?,?)""",
                (response["response_id"], response["request"]["request_id"], response["request"]["request_sha256"], revision_no,
                 None if previous is None else previous["response_id"], None if previous is None else previous["response_sha256"], response_json, response_sha, timestamp),
            )
            self.store._append_event_in_transaction(connection, "intervention.responded", goal_id="goal-one", work_unit_id=response["request"]["request_id"].replace("request", "unit", 1), payload={
                "request_id": response["request"]["request_id"], "request_sha256": response["request"]["request_sha256"],
                "response_id": response["response_id"], "response_sha256": response_sha, "revision_no": revision_no,
                "previous_response_id": None if previous is None else previous["response_id"],
                "previous_response_sha256": None if previous is None else previous["response_sha256"],
                "responder_id": "operator", "responder_kind": "human", "disposition": "answered", "timestamp": timestamp,
            })
            return {"response_id": response["response_id"], "response_sha256": response_sha}

        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            for index in range(closed_count):
                suffix = f"{index:04d}"
                request = self.request({"goal_id": "goal-one", "work_unit_id": f"closed-unit-{suffix}", "attempt_id": f"closed-attempt-{suffix}"}, f"closed-request-{suffix}")
                _, request_sha = insert_request(connection, request, closed=True)
                response = self.response(request, f"closed-response-{suffix}")
                recorded = insert_response(connection, response, revision_no=1, previous=None)
                connection.execute("INSERT INTO intervention_response_heads VALUES(?,?,?,1,?)", (request["request_id"], recorded["response_id"], recorded["response_sha256"], timestamp))
                closure_id = f"closed-closure-{suffix}"
                connection.execute(
                    """INSERT INTO intervention_closures(id,request_id,response_id,response_sha256,closure_kind,requeue_evidence_sha256,
                       envelope_sha256,closed_by,closed_at) VALUES(?,?,?,?,'requeued',?,?,?,?)""",
                    (closure_id, request["request_id"], recorded["response_id"], recorded["response_sha256"], evidence_sha256,
                     self.digests["goal-one"], "worker", timestamp),
                )
                self.store._append_event_in_transaction(connection, "work.requeued", goal_id="goal-one", work_unit_id=f"closed-unit-{suffix}", payload={
                    "performer_id": "worker", "previous_status": "blocked", "request_id": request["request_id"], "request_sha256": request_sha,
                    "response_id": recorded["response_id"], "response_sha256": recorded["response_sha256"], "response_revision_no": 1,
                    "closure_id": closure_id, "envelope_sha256": self.digests["goal-one"], "evidence_sha256": evidence_sha256, "timestamp": timestamp,
                })
            long_request = self.request({"goal_id": "goal-one", "work_unit_id": "long-unit", "attempt_id": "long-attempt"}, "long-request")
            _, long_sha = insert_request(connection, long_request, closed=False)
            previous = None
            for revision_no in range(1, revisions + 1):
                response = self.response(long_request, f"long-response-{revision_no:04d}", expected=previous)
                previous = insert_response(connection, response, revision_no=revision_no, previous=previous)
            assert previous is not None
            connection.execute("INSERT INTO intervention_response_heads VALUES(?,?,?, ?,?)", (long_request["request_id"], previous["response_id"], previous["response_sha256"], revisions, timestamp))
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())
        return long_sha

    def test_two_connection_response_head_race_and_stale_requeue_leave_head_intact(self) -> None:
        self.add_unit("unit-one")
        request, _ = self.yield_request(self.claim(), "request-one")
        first = self.answer(request, "response-one")
        expected = {"response_id": first["response_id"], "response_sha256": first["response_sha256"]}
        caller = AutonomyStore(self.path)
        operator_two = AutonomyStore(self.path)
        old_head_read = Event()
        release_requeue = Event()
        caller_result: dict[str, object] = {}

        def requeue_after_read_barrier() -> None:
            with caller._connection(write=False) as connection:
                head = connection.execute(
                    "SELECT current_response_id,current_response_sha256 FROM intervention_response_heads WHERE request_id=?",
                    (request["request_id"],),
                ).fetchone()
            caller_result["head"] = (head["current_response_id"], head["current_response_sha256"])
            old_head_read.set()
            if not release_requeue.wait(5):
                caller_result["error"] = RuntimeError("test barrier was not released")
                return
            try:
                caller.requeue_work(
                    work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digests["goal-one"], evidence={"decision": "reviewed"},
                    intervention_request_id=request["request_id"], expected_intervention_response_id=head["current_response_id"],
                    expected_intervention_response_sha256=head["current_response_sha256"], at=NOW,
                )
            except Exception as error:  # inspected by the controlling test thread
                caller_result["error"] = error

        caller_thread = Thread(target=requeue_after_read_barrier)
        caller_thread.start()
        self.assertTrue(old_head_read.wait(5))
        self.assertEqual(caller_result["head"], (first["response_id"], first["response_sha256"]))
        winner = operator_two.record_intervention_response(response=self.response(request, "response-two", expected=expected), responder_id="operator", responder_kind="human", at=NOW)
        before_stale_requeue = self.store.verify_audit()
        release_requeue.set()
        caller_thread.join(5)
        self.assertFalse(caller_thread.is_alive())
        self.assertIsInstance(caller_result.get("error"), AutonomyError)
        self.assertRegex(str(caller_result["error"]), "response_head_changed")
        with self.assertRaisesRegex(AutonomyError, "response_head_changed"):
            self.store.record_intervention_response(response=self.response(request, "response-three", expected=expected), responder_id="operator", responder_kind="human", at=NOW)
        self.assertEqual(self.store.get_work_unit("unit-one")["status"], "blocked")
        self.assertEqual(self.store.verify_audit(), before_stale_requeue)
        self.assertEqual(self.requeue("unit-one", request["request_id"], winner)["status"], "eligible")
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_yield_precedes_heartbeat_finish_recovery_drain_stop_and_emergency_without_double_accounting(self) -> None:
        self.add_unit("unit-one")
        claim = self.claim()
        request, _ = self.yield_request(claim, "request-one")
        budget = self.store.budget_summary("goal-one")
        for operation in (
            lambda: self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW),
            lambda: self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], outcome="blocked", at=NOW),
        ):
            with self.assertRaisesRegex(AutonomyError, "stale or no longer current"):
                operation()
        self.assertEqual(self.store.recover_expired_leases(at=NOW + timedelta(days=1)), [])
        self.assertEqual(self.store.drain_goal("goal-one", actor_id="owner", at=NOW)["status_after"], "paused")
        self.assertEqual(self.store.stop_goal("goal-one", actor_id="owner", at=NOW)["status"], "stopped")
        self.assertTrue(self.store.set_emergency_stop(actor_id="owner", reason="operator stop", at=NOW))
        self.assertEqual(self.store.budget_summary("goal-one")["consumed_elapsed_ms"], budget["consumed_elapsed_ms"])
        self.assertEqual(self.store.get_work_unit("unit-one")["status"], "blocked")
        self.assertEqual(request["request_id"], "request-one")
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_control_winner_orderings_preserve_or_reject_later_yield_as_required(self) -> None:
        cases = (
            ("heartbeat", lambda claim: self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW), True, NOW),
            ("finish", lambda claim: self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], outcome="blocked", at=NOW), False, NOW),
            ("recover", lambda claim: self.store.recover_expired_leases(at=NOW + timedelta(seconds=1)), False, NOW + timedelta(seconds=1)),
            ("drain", lambda claim: self.store.drain_goal("goal-one", actor_id="owner", at=NOW), True, NOW),
            ("pause", lambda claim: self.store.pause_goal("goal-one", actor_id="owner", at=NOW), False, NOW),
            ("stop", lambda claim: self.store.stop_goal("goal-one", actor_id="owner", at=NOW), False, NOW),
            ("emergency", lambda claim: self.store.set_emergency_stop(actor_id="owner", reason="operator stop", at=NOW), False, NOW),
        )
        for index, (name, winner, may_yield, yielded_at) in enumerate(cases):
            with self.subTest(winner=name):
                if index:
                    self.tearDown()
                    self.setUp()
                self.add_unit("unit-one")
                claim = self.claim(lease_seconds=1 if name == "recover" else 60)
                winner(claim)
                request = self.request(claim, "request-one")
                if may_yield:
                    yielded = self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=yielded_at)
                    self.assertEqual(yielded["mutation"], "applied")
                else:
                    before = self.store.verify_audit()
                    with self.assertRaisesRegex(AutonomyError, "stale or no longer current|lease is expired|runtime is emergency-stopped"):
                        self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=yielded_at)
                    self.assertEqual(self.store.verify_audit(), before)
                self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_lifecycle_winner_lock_orderings_serialize_against_yield(self) -> None:
        cases = (
            ("heartbeat", lambda store, claim: store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW), NOW, True, "blocked"),
            ("finish", lambda store, claim: store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], outcome="blocked", at=NOW), NOW, False, "blocked"),
            ("recover", lambda store, claim: store.recover_expired_leases(at=NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1), False, "retry-wait"),
            ("drain", lambda store, claim: store.drain_goal("goal-one", actor_id="owner", at=NOW), NOW, True, "blocked"),
            ("pause", lambda store, claim: store.pause_goal("goal-one", actor_id="owner", at=NOW), NOW, False, "paused"),
            ("stop", lambda store, claim: store.stop_goal("goal-one", actor_id="owner", at=NOW), NOW, False, "stopped"),
            ("emergency", lambda store, claim: store.set_emergency_stop(actor_id="owner", reason="operator stop", at=NOW), NOW, False, "paused"),
        )
        for index, (name, operation, yield_at, yield_allowed, expected_status) in enumerate(cases):
            with self.subTest(winner=name):
                if index:
                    self.tearDown()
                    self.setUp()
                self.add_unit("unit-one")
                claim = self.claim(lease_seconds=1 if name == "recover" else 60)
                winner, loser = AutonomyStore(self.path), AutonomyStore(self.path)
                winner_lock_acquired, loser_attempting, release_winner = Event(), Event(), Event()
                winner_errors: list[BaseException] = []
                loser_error: list[AutonomyError | StateError] = []
                loser_yield: list[dict] = []
                original_winner_connection, original_loser_connection = winner._connection, loser._connection

                @contextmanager
                def winner_connection(*, write: bool = True):
                    with original_winner_connection(write=write) as connection:
                        if write:
                            winner_lock_acquired.set()
                            if not release_winner.wait(5):
                                raise RuntimeError("winner lock barrier was not released")
                        yield connection

                @contextmanager
                def loser_connection(*, write: bool = True):
                    if write:
                        loser_attempting.set()
                    with original_loser_connection(write=write) as connection:
                        yield connection

                winner._connection = winner_connection
                loser._connection = loser_connection
                request = self.request(claim, "request-one")

                def run_winner() -> None:
                    try:
                        operation(winner, claim)
                    except BaseException as error:
                        winner_errors.append(error)

                def run_loser() -> None:
                    try:
                        loser_yield.append(loser.yield_for_intervention(
                            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=yield_at,
                        ))
                    except (AutonomyError, StateError) as error:
                        loser_error.append(error)

                winner_thread, loser_thread = Thread(target=run_winner), Thread(target=run_loser)
                winner_thread.start()
                self.assertTrue(winner_lock_acquired.wait(5))
                loser_thread.start()
                self.assertTrue(loser_attempting.wait(5))
                release_winner.set()
                winner_thread.join(5)
                loser_thread.join(5)
                self.assertFalse(winner_thread.is_alive())
                self.assertFalse(loser_thread.is_alive())
                self.assertEqual(winner_errors, [])
                with self.store._connection(write=False) as connection:
                    request_count = connection.execute("SELECT count(*) FROM intervention_requests").fetchone()[0]
                if yield_allowed:
                    self.assertEqual((len(loser_yield), loser_error, request_count), (1, [], 1))
                    self.assertEqual(self.store.get_work_unit("unit-one")["status"], "blocked")
                    self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], 0)
                    if name == "drain":
                        self.assertEqual(self.store.get_goal("goal-one")["status"], "paused")
                else:
                    self.assertEqual(loser_yield, [])
                    self.assertEqual(len(loser_error), 1)
                    self.assertRegex(str(loser_error[0]), "stale or no longer current|lease is expired|runtime is emergency-stopped")
                    self.assertEqual(request_count, 0)
                    self.assertEqual(self.store.get_work_unit("unit-one")["status"], expected_status)
                    self.assertEqual(self.store.budget_summary("goal-one")["reserved_tokens"], 0)
                self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_yield_winner_then_each_lifecycle_control_preserves_intervention_facts(self) -> None:
        cases = ("heartbeat", "finish", "recover", "drain", "pause", "stop", "emergency")
        for index, name in enumerate(cases):
            with self.subTest(control=name):
                if index:
                    self.tearDown()
                    self.setUp()
                self.add_unit("unit-one")
                claim = self.claim()
                request, yielded = self.yield_request(claim, "request-one")
                before_budget = self.store.budget_summary("goal-one")
                if name == "heartbeat":
                    with self.assertRaisesRegex(AutonomyError, "stale or no longer current"):
                        self.store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], at=NOW)
                elif name == "finish":
                    with self.assertRaisesRegex(AutonomyError, "stale or no longer current"):
                        self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], outcome="blocked", at=NOW)
                elif name == "recover":
                    self.assertEqual(self.store.recover_expired_leases(at=NOW + timedelta(days=1)), [])
                elif name == "drain":
                    self.assertEqual(self.store.drain_goal("goal-one", actor_id="owner", at=NOW)["status_after"], "paused")
                elif name == "pause":
                    self.assertEqual(self.store.pause_goal("goal-one", actor_id="owner", at=NOW)["status"], "paused")
                elif name == "stop":
                    self.assertEqual(self.store.stop_goal("goal-one", actor_id="owner", at=NOW)["status"], "stopped")
                else:
                    self.assertTrue(self.store.set_emergency_stop(actor_id="owner", reason="operator stop", at=NOW))
                unit = self.store.get_work_unit("unit-one")
                self.assertEqual(unit["status"], "blocked")
                self.assertEqual(unit["current_attempt_id"], None)
                self.assertEqual(self.store.budget_summary("goal-one"), before_budget)
                with self.store._connection(write=False) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM intervention_requests WHERE id=?", (request["request_id"],)).fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT count(*) FROM intervention_responses WHERE request_id=?", (request["request_id"],)).fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM intervention_closures WHERE request_id=?", (request["request_id"],)).fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT current_intervention_id FROM work_units WHERE id='unit-one'").fetchone()[0], request["request_id"])
                self.assertEqual(yielded["request_id"], request["request_id"])
                self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_historical_yield_and_requeue_replays_cannot_change_new_attempt_or_request(self) -> None:
        self.add_unit("unit-one")
        first_claim = self.claim()
        first_request, _ = self.yield_request(first_claim, "request-one")
        first_response = self.answer(first_request, "response-one")
        self.requeue("unit-one", "request-one", first_response)
        second_claim = self.claim()
        second_request, _ = self.yield_request(second_claim, "request-two")
        yield_replay = self.store.yield_for_intervention(attempt_id=first_claim["attempt_id"], performer_id="worker", lease_token=first_claim["lease_token"], request=first_request, at=NOW)
        requeue_replay = self.requeue("unit-one", "request-one", first_response)
        self.assertEqual((yield_replay["mutation"], yield_replay["current"], yield_replay["current_intervention_id"]), ("none", False, "request-two"))
        self.assertEqual((requeue_replay["mutation"], requeue_replay["current"], requeue_replay["current_intervention_id"]), ("none", False, "request-two"))
        self.assertEqual(self.store.get_work_unit("unit-one")["status"], "blocked")
        self.assertEqual(second_request["request_id"], "request-two")

    def test_response_identity_request_binding_and_postclosure_edits_fail_closed(self) -> None:
        self.add_unit("unit-one")
        self.add_unit("unit-two")
        request_one, _ = self.yield_request(self.claim(), "request-one")
        request_two, _ = self.yield_request(self.claim(), "request-two")
        wrong_digest = self.response(request_one, "wrong-digest")
        wrong_digest["request"]["request_sha256"] = "0" * 64
        with self.assertRaisesRegex(AutonomyError, "digest-mismatched"):
            self.store.record_intervention_response(response=wrong_digest, responder_id="operator", responder_kind="human", at=NOW)
        cross_request = self.response(request_two, "cross-request")
        cross_request["request"]["request_sha256"] = intervention_request_sha256(request_one)
        with self.assertRaisesRegex(AutonomyError, "digest-mismatched"):
            self.store.record_intervention_response(response=cross_request, responder_id="operator", responder_kind="human", at=NOW)
        answered = self.answer(request_one, "response-one")
        changed = self.response(request_one, "response-one")
        changed["answer"] = "A changed answer must not replace an immutable response."
        with self.assertRaisesRegex(AutonomyError, "idempotency_conflict"):
            self.store.record_intervention_response(response=changed, responder_id="operator", responder_kind="human", at=NOW)
        self.requeue("unit-one", "request-one", answered)
        revision = self.response(request_one, "response-two", expected={"response_id": answered["response_id"], "response_sha256": answered["response_sha256"]})
        with self.assertRaisesRegex(AutonomyError, "no longer current|closed"):
            self.store.record_intervention_response(response=revision, responder_id="operator", responder_kind="human", at=NOW)
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_response_disposition_and_structured_requeue_require_current_answered_head_and_authority(self) -> None:
        self.add_unit("unit-one")
        claim = self.claim()
        request = self.request(claim, "request-one")
        request.update(outcome_class="approval-required", requires_human_approval=True)
        self.store.yield_for_intervention(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"], request=request, at=NOW)
        steward = self.response(request, "steward-response")
        steward["responder"] = {"kind": "steward", "actor_id": "steward"}
        with self.assertRaisesRegex(AutonomyError, "require a human"):
            self.store.record_intervention_response(response=steward, responder_id="steward", responder_kind="steward", at=NOW)
        declined = self.response(request, "response-one", disposition="declined")
        first = self.store.record_intervention_response(response=declined, responder_id="operator", responder_kind="human", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "not answered"):
            self.requeue("unit-one", "request-one", first)
        cancelled = self.response(request, "response-two", expected={"response_id": first["response_id"], "response_sha256": first["response_sha256"]}, disposition="cancelled")
        second = self.store.record_intervention_response(response=cancelled, responder_id="operator", responder_kind="human", at=NOW)
        with self.assertRaisesRegex(AutonomyError, "not answered"):
            self.requeue("unit-one", "request-one", second)
        third = self.answer(request, "response-three", expected={"response_id": second["response_id"], "response_sha256": second["response_sha256"]})
        with self.assertRaisesRegex(AutonomyError, "structured intervention requeue"):
            self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digests["goal-one"], evidence={"decision": "reviewed"}, at=NOW)
        with self.assertRaisesRegex(AutonomyError, "structured requeue requires"):
            self.store.requeue_work(work_unit_id="unit-one", performer_id="worker", envelope_sha256=self.digests["goal-one"], evidence={"decision": "reviewed"}, intervention_request_id="request-one", at=NOW)
        self.assertEqual(self.requeue("unit-one", "request-one", third)["status"], "eligible")
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_historical_requeue_rejects_changed_performer_envelope_or_evidence(self) -> None:
        self.add_unit("unit-one")
        request, _ = self.yield_request(self.claim(), "request-one")
        response = self.answer(request, "response-one")
        self.requeue("unit-one", "request-one", response)
        base = {
            "work_unit_id": "unit-one", "performer_id": "worker", "envelope_sha256": self.digests["goal-one"],
            "evidence": {"decision": "reviewed"}, "intervention_request_id": "request-one",
            "expected_intervention_response_id": response["response_id"], "expected_intervention_response_sha256": response["response_sha256"], "at": NOW,
        }
        for change in (
            {"performer_id": "other-worker"},
            {"envelope_sha256": "f" * 64},
            {"evidence": {"decision": "changed"}},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(AutonomyError, "idempotency_conflict"):
                self.store.requeue_work(**(base | change))
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())

    def test_goal_isolation_denied_requeue_and_tampered_pointer_fail_closed_without_secret_leakage(self) -> None:
        self.add_goal("goal-two")
        self.add_unit("unit-one", requeue=False)
        self.add_unit("unit-two", goal_id="goal-two")
        request_one, _ = self.yield_request(self.claim(), "request-one")
        response_one = self.answer(request_one, "response-one")
        with self.assertRaisesRegex(AutonomyError, "no current approval") as denied:
            self.requeue("unit-one", "request-one", response_one)
        self.assertNotIn("token=private", str(denied.exception))
        request_two, _ = self.yield_request(self.claim(goal_id="goal-two"), "request-two")
        with self.assertRaisesRegex(AutonomyError, "stale or does not belong"):
            self.requeue("unit-two", "request-one", response_one, goal_id="goal-two")
        with self.store._connection() as connection:
            self.store._prepare_write(connection, allow_unsealed=True)
            connection.execute("UPDATE work_units SET current_intervention_id='request-one' WHERE id='unit-two'")
        audit = self.store.verify_audit()
        self.assertFalse(audit["ok"])
        self.assertTrue(any("tampered" in issue or "intervention" in issue for issue in audit["issues"]))
        bad_request = dict(request_two); bad_request["prompt"] = "token=private"
        with self.assertRaisesRegex(AutonomyError, "credentials or secrets") as rejected:
            self.store.yield_for_intervention(attempt_id=request_two["source"]["attempt_id"], performer_id="worker", lease_token="irrelevant", request=bad_request, at=NOW)
        self.assertNotIn("token=private", str(rejected.exception))

    def test_thousands_of_closed_requests_and_long_history_keep_projection_bounded_and_indexed(self) -> None:
        self.build_large_verified_fixture()
        before_reads = self.path.read_bytes()
        inbox = intervention_inbox(self.store, include_closed=True, include_legacy=False, limit=5, offset=1_000, at=NOW)
        self.assertEqual((inbox["pagination"]["total"], inbox["pagination"]["returned"], inbox["aggregates"]["closed"]), (2_001, 5, 2_000))
        first_page = intervention_inbox(self.store, include_closed=True, include_legacy=False, limit=1, at=NOW)
        self.assertEqual((first_page["items"][0]["request_id"], first_page["items"][0]["response"]["id"], first_page["items"][0]["response_revision_count"]),
                         ("long-request", "long-response-1024", 1_024))
        self.assertNotIn("responses", first_page["items"][0])
        history = intervention_response_history(self.store, "long-request", after_revision=1_000, limit=3, at=NOW)
        self.assertEqual([row["id"] for row in history["responses"]], ["long-response-1001", "long-response-1002", "long-response-1003"])
        self.assertEqual(history["pagination"], {"limit": 3, "after_revision": 1_000, "total": 1_024, "returned": 3, "has_more": True, "next_after_revision": 1_003})
        with self.store._connection(write=False) as connection:
            plan = " ".join(row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM intervention_responses WHERE request_id=? AND revision_no>? ORDER BY revision_no LIMIT ?",
                ("long-request", 1_000, 3),
            ))
        self.assertIn("USING INDEX", plan)
        self.assertIn("request_id", plan)
        self.assertEqual(self.path.read_bytes(), before_reads)
        self.assertTrue(self.store.verify_audit()["ok"], self.store.verify_audit())


if __name__ == "__main__":
    unittest.main()
