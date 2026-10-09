"""Independent integration acceptance for the read-only work-unit case file.

The lower-level contract tests cover each projection helper.  These checks use
real ledger transitions and a second connection where needed, so they protect
the cross-boundary guarantees: a report is one verified snapshot, never a
mutation capability or a disclosure channel.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
import tracemalloc
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError, AutonomyStore
from tasktra.goal_readiness import goal_readiness
from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tasktra.work_inspection import inspect_work_unit
from tasktra import work_inspection as inspection_module
from tests import test_codex_runs_capacity as capacity_fixture
from tests import test_execution_recovery_integration as recovery_fixture
from tests import test_intervention_integration as intervention_fixture
from tests import test_stage4_effect_ledger as effects_fixture


NOW = capacity_fixture.NOW


class WorkInspectionIntegrationTests(unittest.TestCase):
    """Acceptance checks intentionally independent from projection implementation."""

    @staticmethod
    def durable_files(path: Path) -> dict[str, bytes]:
        """Only durable database content is immutable under read coordination."""
        return {
            str(item): item.read_bytes()
            for item in (path, Path(str(path) + "-wal"))
            if item.exists()
        }

    def simple_store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(goal_id="goal-one", title="Inspection", description="fixture", acceptance=["Done."])
        store.create_work_unit(goal_id="goal-one", work_unit_id="parent", title="Parent")
        store.create_work_unit(
            goal_id="goal-one", work_unit_id="selected", title="Selected", prerequisite_ids=("parent",),
        )
        store.create_work_unit(goal_id="goal-one", work_unit_id="child", title="Child", prerequisite_ids=("selected",))
        return store

    def inspect(self, store: StateStore, *, unit: str = "selected", **kwargs: object) -> dict:
        kwargs.setdefault("at", NOW)
        return inspect_work_unit(
            store, project_root=Path("project root with spaces"), goal_id="goal-one", work_unit_id=unit, **kwargs,
        )

    def test_closed_observational_report_is_byte_stable_and_matches_readiness(self):
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            with closing(sqlite3.connect(store.path)) as keeper:
                self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                keeper.execute("PRAGMA wal_autocheckpoint=0")
                keeper.execute("BEGIN")
                keeper.execute("SELECT count(*) FROM goals").fetchone()
                store.create_work_unit(goal_id="goal-one", work_unit_id="independent", title="Independent")
                before = self.durable_files(store.path)
                audit_before = store.verify_audit()
                report = self.inspect(store)
                second = self.inspect(StateStore(store.path))
                self.assertEqual(json.dumps(report, sort_keys=True), json.dumps(second, sort_keys=True))
                self.assertEqual(self.durable_files(store.path), before)
                self.assertEqual(store.verify_audit(), audit_before)
            self.assertEqual(list(report), [
                "kind", "version", "schema_version", "goal_id", "work_unit_id", "root", "read_only",
                "authority_evaluated", "claimability_evaluated", "notice", "capture", "unit", "goal_context",
                "attempts", "activity", "evidence_index", "action_candidates",
            ])
            self.assertEqual((report["kind"], report["version"], report["schema_version"]),
                             ("tasktra.work-unit-inspection", 1, SCHEMA_VERSION))
            self.assertEqual(report["root"], str(Path("project root with spaces").resolve()))
            self.assertTrue(report["read_only"])
            self.assertFalse(report["authority_evaluated"])
            self.assertFalse(report["claimability_evaluated"])
            readiness = goal_readiness(store, goal_id="goal-one", limit=50)
            expected = next(row for name in ("ready_frontier", "blocking_frontier")
                            for row in readiness["frontiers"][name]["items"]
                            if row["work_unit_id"] == "selected")
            structural_keys = (
                "status", "checkpoint_id", "category", "category_reason_code", "structural_ready",
                "incomplete_direct_prerequisite_ids", "remaining_wave", "downstream_structural_depth",
                "deepest_remaining_branch", "incomplete_direct_dependents_count",
                "direct_prerequisite_gates_cleared_if_completed",
                "remaining_direct_prerequisite_gates_cleared_if_completed", "terminal_attention",
                "frontier_memberships", "operational_reason_codes",
            )
            for key in structural_keys:
                self.assertEqual(report["unit"][key], expected[key], key)
            for value in ("Parent", "Selected", "Child", "fixture", "lease_token", "owner_id"):
                self.assertNotIn(value, json.dumps(report))

    def test_complete_and_leased_units_retain_documented_structural_and_lease_facts(self):
        case = capacity_fixture.CodexCapacityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        claim = case.claim()
        leased = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
        self.assertEqual(leased["unit"]["status"], "leased")
        self.assertTrue(leased["unit"]["current_attempt"]["is_current"])
        self.assertTrue(leased["unit"]["current_attempt"]["lease_live_at_capture"])
        self.assertFalse(leased["unit"]["current_attempt"]["lease_expired_at_capture"])
        case.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=case.token,
            outcome="permanent", at=NOW + timedelta(seconds=1),
        )
        failed = inspect_work_unit(
            case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one",
            at=NOW + timedelta(seconds=2),
        )
        self.assertEqual((failed["unit"]["status"], failed["unit"]["attempt_count"]), ("failed", 1))
        self.assertIsNone(failed["unit"]["current_attempt"])
        self.assertEqual(failed["attempts"]["items"][0]["outcome_class"], "failed")

    def test_attempt_and_activity_keysets_are_exclusive_and_never_disclose_context(self):
        case = capacity_fixture.CodexCapacityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        first = case.claim(at=NOW)
        case.store.finish_attempt(
            attempt_id=first["attempt_id"], performer_id="worker", lease_token=case.token,
            outcome="transient", at=NOW + timedelta(seconds=1),
        )
        second = case.claim(at=NOW + timedelta(seconds=2))
        case.store.finish_attempt(
            attempt_id=second["attempt_id"], performer_id="worker", lease_token=case.token,
            outcome="permanent", at=NOW + timedelta(seconds=3),
        )
        first_page = inspect_work_unit(
            case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", limit=1,
            at=NOW + timedelta(seconds=4),
        )
        attempts = first_page["attempts"]
        activity = first_page["activity"]
        self.assertEqual((attempts["returned"], attempts["has_more"]), (1, True))
        self.assertEqual((activity["returned"], activity["has_more"]), (1, True))
        second_page = inspect_work_unit(
            case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", limit=1,
            before_attempt_no=attempts["next_before_attempt_no"], before_sequence=activity["next_before_sequence"],
            at=NOW + timedelta(seconds=4),
        )
        self.assertLess(second_page["attempts"]["items"][0]["attempt_no"], attempts["items"][0]["attempt_no"])
        self.assertLess(second_page["activity"]["items"][0]["sequence"], activity["items"][0]["sequence"])
        encoded = json.dumps(first_page)
        for private in (case.token, "fixture", "worker", "lease_token_hash", "owner_id"):
            self.assertNotIn(private, encoded)
        self.assertEqual(set(attempts["items"][0]["repository_context"]), {"repository", "revision", "branch", "workspace"})
        for item in attempts["items"][0]["repository_context"].values():
            self.assertEqual(set(item), {"present", "sha256", "redacted"})

    def test_unknown_and_secret_bearing_activity_is_curated_without_payload_reflection(self):
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            secret = "secret-activity-token-" + "x" * 32
            with store._connection() as connection:
                store._prepare_write(connection)
                AutonomyStore._append(
                    connection, "effect.legacy-private", goal_id="goal-one", work_unit_id="selected",
                    payload={"secret": secret, "host": "/absolute/private/path", "nested": [secret]},
                )
            report = self.inspect(store)
            item = report["activity"]["items"][0]
            self.assertEqual((item["event_type"], item["recognized"], item["payload_status"], item["metadata"]),
                             ("unrecognized", False, "unrecognized_type", {}))
            self.assertEqual(item["event_type_sha256"], sha256(b"effect.legacy-private").hexdigest())
            encoded = json.dumps(report)
            self.assertNotIn(secret, encoded)
            self.assertNotIn("/absolute/private/path", encoded)

    def test_forged_known_event_with_valid_scalars_never_becomes_authoritative_metadata(self):
        """A familiar event name and syntactically valid hash cannot establish lineage."""
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            with store._connection() as connection:
                store._prepare_write(connection)
                StateStore._append_event_in_transaction(
                    connection, "work.finished", goal_id="goal-one", work_unit_id="selected",
                    payload={"attempt_id": "attempt-forged", "outcome": "failed",
                             "request_sha256": "a" * 64},
                )
            report = self.inspect(store)
            item = report["activity"]["items"][0]
            self.assertTrue(item["recognized"])
            self.assertEqual((item["payload_status"], item["metadata"]), ("unsupported_shape", {}))

    def test_history_queries_use_keysets_and_limit_at_the_database_boundary(self):
        case = capacity_fixture.CodexCapacityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        case.claim()
        statements: list[str] = []
        original = case.store._connection

        @contextmanager
        def traced_connection(*, write: bool = True):
            with original(write=write) as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(case.store, "_connection", traced_connection):
            inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", limit=2, at=NOW)
        history = [sql.upper() for sql in statements]
        history = [sql for sql in history if "FROM WORK_ATTEMPTS" in sql or "FROM AUDIT_EVENTS" in sql]
        self.assertTrue(history)
        self.assertFalse(any(" OFFSET " in sql for sql in history))
        attempts = [sql for sql in history if "FROM WORK_ATTEMPTS" in sql and "ORDER BY ATTEMPT_NO DESC" in sql]
        self.assertTrue(attempts)
        self.assertTrue(all(" LIMIT " in sql for sql in attempts))

    def test_integrity_schema_and_cross_goal_fail_closed_without_writes(self):
        mutations = (
            "DROP TRIGGER audit_events_no_update; UPDATE audit_events SET payload='{}' WHERE sequence=1",
            "DROP TRIGGER authority_seals_no_update; UPDATE authority_seals SET row_hash='tampered' WHERE table_name='work_units'",
            "PRAGMA user_version=13",
        )
        for statement in mutations:
            with self.subTest(statement=statement), TemporaryDirectory() as directory:
                store = self.simple_store(directory)
                with closing(sqlite3.connect(store.path)) as connection:
                    connection.executescript(statement)
                    connection.commit()
                before = self.durable_files(store.path)
                with self.assertRaises(StateError):
                    self.inspect(store)
                self.assertEqual(self.durable_files(store.path), before)
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            store.create_goal(goal_id="goal-two", title="Other", description="Other", acceptance=["Done."])
            with self.assertRaises(StateError):
                inspect_work_unit(store, project_root=Path("."), goal_id="goal-two", work_unit_id="selected")

    def test_unrelated_tamper_fails_closed_without_reflecting_the_other_identity(self):
        with TemporaryDirectory() as directory:
            store = self.simple_store(directory)
            private_id = "unrelated-private-unit"
            store.create_goal(goal_id="goal-two", title="Other", description="Other", acceptance=["Done."])
            store.create_work_unit(goal_id="goal-two", work_unit_id=private_id, title="Private")
            with closing(sqlite3.connect(store.path)) as connection:
                connection.execute("DROP TRIGGER authority_seals_no_update")
                connection.execute(
                    "UPDATE authority_seals SET row_hash='tampered' WHERE table_name='work_units' AND row_id=?",
                    (private_id,),
                )
                connection.commit()
            before = self.durable_files(store.path)
            with self.assertRaises(StateError) as rejected:
                self.inspect(store)
            self.assertNotIn(private_id, str(rejected.exception))
            self.assertEqual(self.durable_files(store.path), before)

    def test_one_verified_snapshot_excludes_a_concurrent_committed_write(self):
        """The writer commits after verification but before the report has finished reading."""
        case = capacity_fixture.CodexCapacityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        with closing(sqlite3.connect(case.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        entered = Event()
        committed = Event()

        def writer() -> None:
            self.assertTrue(entered.wait(5))
            StateStore(case.path).create_work_unit(
                goal_id="goal-one", work_unit_id="written-during-read", title="Later",
                scope={"paths": ["src"], "exclusions": []},
            )
            committed.set()

        original = StateStore._assert_current_state_integrity_in_transaction

        def release_writer(connection: sqlite3.Connection) -> None:
            original(connection)
            if not entered.is_set():
                entered.set()
                self.assertTrue(committed.wait(5))

        thread = Thread(target=writer)
        thread.start()
        with patch.object(StateStore, "_assert_current_state_integrity_in_transaction", side_effect=release_writer), \
             patch.object(case.store, "_connection", wraps=case.store._connection) as opened:
            report = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(committed.is_set())
        self.assertEqual(opened.call_count, 1)
        with closing(sqlite3.connect(case.path)) as connection:
            current_head = connection.execute("SELECT max(sequence) FROM audit_events").fetchone()[0]
        self.assertLess(report["capture"]["audit_head"]["sequence"], current_head)
        self.assertNotIn("written-during-read", json.dumps(report))

    def test_real_intervention_codex_and_provider_transitions_remain_private_and_observational(self):
        """Use the established integration fixture; none of these transitions call a host/provider."""
        case = intervention_fixture.InterventionIntegrationTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        claim = case.claim()
        request, _ = case.yield_request(claim, "private-request")
        response = case.answer(request, "private-response")
        report = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
        intervention = report["evidence_index"]["interventions"]
        self.assertEqual(intervention["returned"], 1)
        self.assertEqual(intervention["items"][0]["request_id"], "private-request")
        self.assertEqual(intervention["items"][0]["response_head"]["response_id"], "private-response")
        encoded = json.dumps(report)
        for private in (request["prompt"], request["rationale"], "Use the reviewed option.", "worker", "operator"):
            self.assertNotIn(private, encoded)
        for candidate in report["action_candidates"]:
            self.assertFalse(candidate["authority_evaluated"])
            self.assertFalse(candidate["capture_identity_enforced"])
        requeue = next(
            candidate for candidate in report["action_candidates"]
            if candidate["kind"] == "guarded_template"
            and candidate["relevant_identities"].get("request_id") == "private-request"
        )
        self.assertEqual(
            {name: requeue["relevant_identities"][name] for name in ("request_id", "response_id", "response_sha256")},
            {"request_id": "private-request", "response_id": "private-response",
             "response_sha256": intervention["items"][0]["response_head"]["response_sha256"]},
        )
        self.assertIn("requeue", requeue["argv"])
        self.assertNotIn("capture_audit_head", requeue["argv"])

        with self.assertRaisesRegex(AutonomyError, "response[_ ]head|stale"):
            case.store.requeue_work(
                work_unit_id="unit-one", performer_id="worker", envelope_sha256=case.digests["goal-one"],
                evidence={"decision": "reviewed"}, intervention_request_id="private-request",
                expected_intervention_response_id="wrong-response",
                expected_intervention_response_sha256=response["response_sha256"], at=NOW,
            )
        applied = case.store.requeue_work(
            work_unit_id="unit-one", performer_id="worker", envelope_sha256=case.digests["goal-one"],
            evidence={"decision": "reviewed"}, intervention_request_id="private-request",
            expected_intervention_response_id="private-response",
            expected_intervention_response_sha256=response["response_sha256"], at=NOW,
        )
        replay = case.store.requeue_work(
            work_unit_id="unit-one", performer_id="worker", envelope_sha256=case.digests["goal-one"],
            evidence={"decision": "reviewed"}, intervention_request_id="private-request",
            expected_intervention_response_id="private-response",
            expected_intervention_response_sha256=response["response_sha256"], at=NOW,
        )
        self.assertEqual((applied["mutation"], replay["mutation"], replay["idempotent"]), ("applied", "none", True))

    def test_provider_retry_with_ambiguous_privacy_lineage_is_redacted(self):
        """A retry cannot discard earlier lease material when its intent is rebound."""
        case = effects_fixture.ProviderEffectLedgerTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        operation = effects_fixture.descriptor()
        key = "inspection-retry-binding"
        intent = case.store.prepare_provider_effect(
            idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation,
            request={"body": "private provider request"}, envelope_sha256=case.digest, performer_id="worker",
            work_attempt_id=case.claim["attempt_id"], lease_token=case.claim["lease_token"], at=effects_fixture.NOW,
        )
        first = case.store.begin_provider_effect_dispatch(
            idempotency_key=key, operation_descriptor=operation, performer_id="worker",
            lease_token=case.claim["lease_token"], at=effects_fixture.NOW,
        )
        case.mark_indeterminate(key, first)
        original = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one",
                                     work_unit_id="unit-one", at=effects_fixture.NOW)
        visible = original["evidence_index"]["provider_effects"]["items"][0]
        self.assertEqual(visible["attempt_id"], first["work_attempt_id"])
        self.assertEqual(visible["receipt_outcome"], "indeterminate")
        self.assertEqual((visible["provider"], visible["capability"], visible["action"]),
                         ("github", "issue-comment", "remote-comment"))
        case.store._record_adapter_provider_reconciliation(
            idempotency_key=key, resolution="absent", observation={"private": "do not disclose"},
            performer_id="worker", at=effects_fixture.NOW,
        )
        at = effects_fixture.NOW + timedelta(seconds=31)
        case.store.recover_expired_leases(goal_id="goal-one", at=at)
        replacement = case.store.claim_next_work(
            goal_id="goal-one", performer_id="worker", envelope_sha256=case.digest, lease_seconds=30,
            repository="replacement-repository", revision="replacement-revision", branch="main",
            workspace="replacement-workspace", at=at,
        )
        self.assertIsNotNone(replacement)
        case.store.retry_provider_effect(
            idempotency_key=intent["idempotency_key"], performer_id="worker",
            work_attempt_id=replacement["attempt_id"], lease_token=replacement["lease_token"], at=at,
        )
        report = inspect_work_unit(
            case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=at,
        )
        self.assertEqual(report["evidence_index"]["provider_effects"]["items"], [])
        self.assertEqual(report["evidence_index"]["provider_effects"]["redacted_items"], 1)
        self.assertNotIn("private provider request", json.dumps(report))
        case.store.begin_provider_effect_dispatch(
            idempotency_key=key, operation_descriptor=operation, performer_id="worker",
            lease_token=replacement["lease_token"], at=at,
        )
        dispatched = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one",
                                       work_unit_id="unit-one", at=at)
        self.assertEqual(dispatched["evidence_index"]["provider_effects"]["items"], [])
        self.assertEqual(dispatched["evidence_index"]["provider_effects"]["redacted_items"], 1)
        self.assertNotIn(key, json.dumps(dispatched))

    def test_codex_states_usage_and_observation_eligibility_use_recorded_receipts(self):
        for opaque in (None, "private-opaque-host-id"):
            with self.subTest(opaque=opaque):
                case = capacity_fixture.CodexCapacityTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.add_unit("unit-one")
                claim = case.claim()
                run = case.prepare(claim, "inspection-run")
                arguments = dict(project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
                prepared = inspect_work_unit(case.store, **arguments)
                self.assertEqual(prepared["evidence_index"]["codex_runs"]["items"][0]["state"], "prepared")
                case.store.record_codex_start(run_id=run["run_id"], observer_id="fixture-observer",
                                              host_canonical_name=run["requested_task_name"],
                                              host_agent_id=opaque, at=NOW)
                started = inspect_work_unit(case.store, **arguments)
                item = started["evidence_index"]["codex_runs"]["items"][0]
                self.assertEqual(item["state"], "started")
                candidates = [row for row in started["action_candidates"]
                              if row["reason_code"] == "action.codex_reconciliation_available"]
                self.assertEqual(len(candidates), int(opaque is None))
                case.store.record_codex_finish(run_id=run["run_id"], observer_id="fixture-observer",
                    outcome="failed", result_status="unavailable", result_sha256=None,
                    usage_status="unavailable", at=NOW)
                finished = inspect_work_unit(case.store, **arguments)
                item = finished["evidence_index"]["codex_runs"]["items"][0]
                self.assertEqual((item["state"], item["outcome"], item["usage_status"]),
                                 ("finished", "failed", "unavailable"))
                self.assertIsNone(item["input_tokens"])
                self.assertIsNone(item["output_tokens"])
                self.assertFalse(any(row["reason_code"] == "action.codex_reconciliation_available"
                                     for row in finished["action_candidates"]))
                activity = next(row for row in finished["activity"]["items"] if row["event_type"] == "codex_run.finished")
                self.assertEqual(activity["payload_status"], "recognized")
                self.assertEqual(activity["metadata"]["run_id"], run["run_id"])
                encoded = json.dumps(finished)
                for private in (case.token, "fixture-observer", "private-opaque-host-id", run["requested_task_name"]):
                    self.assertNotIn(private, encoded)

    def test_work_activity_requires_native_fields_and_a_matching_terminal_attempt(self):
        case = capacity_fixture.CodexCapacityTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.add_unit("unit-one")
        claim = case.claim()
        case.store.append_event("work.finished", goal_id="goal-one", work_unit_id="unit-one",
                                payload={"attempt_id": claim["attempt_id"]})
        case.store.append_event("provider_effect.prepared", goal_id="goal-one", work_unit_id="unit-one",
                                payload={"idempotency_key": ["unsupported", "private"]})
        before = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
        for event in before["activity"]["items"][:2]:
            self.assertEqual(event["payload_status"], "unsupported_shape")
            self.assertEqual(event["metadata"], {})
        claimed = next(row for row in before["activity"]["items"] if row["event_type"] == "work.claimed")
        self.assertEqual(claimed["payload_status"], "recognized")
        case.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=case.token,
                                  outcome="permanent", at=NOW + timedelta(seconds=1))
        after = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=NOW)
        finished = [row for row in after["activity"]["items"] if row["event_type"] == "work.finished"]
        self.assertEqual([row["payload_status"] for row in finished], ["recognized", "unsupported_shape"])
        self.assertEqual(finished[0]["metadata"]["outcome"], "failed")

    def test_native_provider_activity_is_linked_to_exact_same_attempt_receipts(self):
        case = effects_fixture.ProviderEffectLedgerTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        operation = effects_fixture.descriptor()
        key = "provider-activity"
        case.store.prepare_provider_effect(idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one",
            operation_descriptor=operation, request={"body": "Private provider content"}, envelope_sha256=case.digest,
            performer_id="worker", work_attempt_id=case.claim["attempt_id"], lease_token=case.claim["lease_token"], at=effects_fixture.NOW)
        first = case.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation,
            performer_id="worker", lease_token=case.claim["lease_token"], at=effects_fixture.NOW)
        case.mark_indeterminate(key, first)
        case.store._record_adapter_provider_reconciliation(idempotency_key=key, resolution="absent",
            observation={"private": "hidden"}, performer_id="worker", at=effects_fixture.NOW)
        case.store.retry_provider_effect(idempotency_key=key, performer_id="worker",
            work_attempt_id=case.claim["attempt_id"], lease_token=case.claim["lease_token"], at=effects_fixture.NOW)
        second = case.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation,
            performer_id="worker", lease_token=case.claim["lease_token"], at=effects_fixture.NOW)
        at = effects_fixture.NOW + timedelta(seconds=31)
        case.store.begin_provider_effect_dispatch(idempotency_key=key, operation_descriptor=operation,
            performer_id="worker", lease_token=case.claim["lease_token"], at=at)
        report = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=at)
        activity = [row for row in report["activity"]["items"] if row["event_type"].startswith("provider_effect.")]
        self.assertEqual(len(activity), 7)
        self.assertTrue(all(row["payload_status"] == "recognized" for row in activity))
        self.assertTrue(all(row["metadata"]["idempotency_key"] == key for row in activity))
        receipt = next(row for row in activity if row["event_type"] == "provider_effect.receipt_recorded")
        self.assertEqual(receipt["metadata"]["effect_attempt_id"], first["id"])
        self.assertEqual(receipt["metadata"]["outcome"], "indeterminate")
        effect = report["evidence_index"]["provider_effects"]["items"][0]
        self.assertEqual(effect["current_effect_attempt_id"], second["id"])
        self.assertIsNone(effect["receipt_outcome"])
        case.store.append_event("provider_effect.receipt_recorded", goal_id="goal-one", work_unit_id="unit-one",
                                payload={"idempotency_key": key})
        forged = inspect_work_unit(case.store, project_root=Path("."), goal_id="goal-one", work_unit_id="unit-one", at=at)
        self.assertEqual(forged["activity"]["items"][0]["payload_status"], "unsupported_shape")
        self.assertEqual(forged["activity"]["items"][0]["metadata"], {})
        self.assertNotIn("Private provider content", json.dumps(forged))

    def test_large_goal_and_sparse_activity_have_bounded_projection_and_one_synthesis(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(goal_id="goal-one", title="Scale", description="Synthetic inspection scale")
            store.create_work_unit(goal_id="goal-one", work_unit_id="chain-0000", title="First")
            with closing(sqlite3.connect(store.path)) as connection:
                connection.row_factory = sqlite3.Row
                template = connection.execute("SELECT goal_id,status,scope,created_at,updated_at FROM work_units WHERE id='chain-0000'").fetchone()
                connection.executemany("INSERT INTO work_units(id,title,goal_id,status,scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    [(f"chain-{index:04d}", "Private title", *template) for index in range(1, 5000)])
                connection.executemany("INSERT INTO work_unit_dependencies VALUES(?,?)",
                    [(f"chain-{index:04d}", f"chain-{index-1:04d}") for index in range(1, 5000)])
                for index in range(6000):
                    store._append_event_in_transaction(connection, "fixture.private-event", goal_id="goal-one",
                        work_unit_id="chain-0000" if index % 100 == 0 else "chain-4999", payload={"private": "do not reflect"})
                connection.commit()
            store.attest_ledger(actor_id="fixture-steward")
            before = self.durable_files(store.path)
            tracemalloc.start()
            started = time.perf_counter()
            with patch.object(inspection_module, "_synthesize", wraps=inspection_module._synthesize) as synthesis:
                sparse = self.inspect(store, unit="chain-0000", limit=3)
                dense = self.inspect(store, unit="chain-4999", limit=3)
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(synthesis.call_count, 2)
            self.assertEqual(sparse["unit"]["downstream_structural_depth"], 5000)
            self.assertEqual(dense["unit"]["remaining_wave"], 4999)
            self.assertEqual((sparse["activity"]["returned"], dense["activity"]["returned"]), (3, 3))
            self.assertTrue(sparse["activity"]["has_more"])
            for report in (sparse, dense):
                self.assertLessEqual(report["attempts"]["returned"], 3)
                for family in ("interventions", "codex_runs", "provider_effects"):
                    self.assertLessEqual(report["evidence_index"][family]["returned"], 3)
                self.assertNotIn("do not reflect", json.dumps(report))
            self.assertEqual(self.durable_files(store.path), before)
            print(json.dumps({"inspection_scale": {"units": 5000, "unit_events": 6000,
                "public_calls": 2, "elapsed_seconds": round(elapsed, 4), "python_peak_bytes": peak,
                "activity_rows_returned": 6, "synthesis_calls": synthesis.call_count,
                "includes_full_ledger_verification": True}}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
