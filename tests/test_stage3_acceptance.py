"""Adversarial acceptance checks for Stage 3 durable autonomy.

These tests deliberately exercise paths that are easy to miss in the normal
workflow: contention, stolen and expired lease credentials, each non-success
classification, every enforced execution budget, pause/stop recovery, audit
corruption, CLI input boundaries, and migration snapshots.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra.cli import build_parser, main
from tasktra.state import SCHEMA_VERSION


NOW = datetime(2031, 1, 1, tzinfo=timezone.utc)


def authority(goal_id: str, *, tokens: int | None = 20, attempts: int = 3,
              elapsed_seconds: int = 60, concurrency: int = 1) -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1,
        "goal_id": goal_id, "outcome": "Exercise durable execution safely.",
        "motivation": "Acceptance checks must prove recovery boundaries.",
        "author_id": "human-owner",
        "acceptance_criteria": [{"id": "done", "statement": "Durable checks pass."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "goal-resume", "work-claim", "work-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
        "quality_requirements": ["Preserve an auditable recovery trail."],
        "budgets": {"tokens": tokens, "attempts": attempts,
                    "elapsed_seconds": elapsed_seconds, "concurrency": concurrency},
        "dependencies": [], "checkpoints": [], "stop_conditions": ["Stop when unsafe."],
        "escalation_conditions": ["Escalate when authority is insufficient."],
    }


def run_cli(*arguments: str) -> tuple[int, dict]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class Stage3AcceptanceTests(unittest.TestCase):
    def build_active_store(self, path: Path, *, goal_id: str = "goal-a", tokens: int | None = 20,
                           attempts: int = 3, elapsed_seconds: int = 60, concurrency: int = 1) -> tuple[AutonomyStore, str]:
        store = AutonomyStore(path)
        store.create_goal(goal_id=goal_id, title="Acceptance goal", description="Exercise ledger", acceptance=["Durable checks pass."])
        contract = authority(goal_id, tokens=tokens, attempts=attempts, elapsed_seconds=elapsed_seconds, concurrency=concurrency)
        digest = authority_envelope_sha256(contract)
        store.define_goal_contract(goal_id, contract, actor_id="human-owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        store.record_transition_approval(
            goal_id=goal_id, action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human-owner", performer_id="goal-runner",
            valid_until=expiry, at=NOW,
        )
        store.record_transition_approval(
            goal_id=goal_id, action="goal-resume", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human-owner", performer_id="goal-runner",
            valid_until=expiry, at=NOW,
        )
        store.activate_goal(goal_id, actor_id="goal-runner", envelope_sha256=digest, at=NOW)
        for performer in ("worker-one", "worker-two"):
            store.record_transition_approval(
                goal_id=goal_id, action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=digest, approver_id="goal-steward", approver_kind="steward",
                performer_id=performer, valid_until=expiry, at=NOW,
            )
        return store, digest

    @staticmethod
    def claim(store: AutonomyStore, digest: str, *, goal_id: str = "goal-a", performer: str = "worker-one",
              at: datetime = NOW, lease_seconds: int = 20, token_reservation: int = 0) -> dict | None:
        return store.claim_next_work(
            goal_id=goal_id, performer_id=performer, envelope_sha256=digest,
            repository="repository", revision="revision", branch="branch", workspace="workspace",
            at=at, lease_seconds=lease_seconds, token_reservation=token_reservation,
        )

    def test_atomic_contention_allows_one_owner_only(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            seed, digest = self.build_active_store(path, concurrency=2)
            seed.create_work_unit(goal_id="goal-a", work_unit_id="one-unit", title="One unit", scope={"paths": ["src"], "exclusions": []})
            stores = (AutonomyStore(path), AutonomyStore(path))
            barrier = threading.Barrier(2)
            claims: list[dict | None] = []
            failures: list[BaseException] = []

            def contend(store: AutonomyStore, performer: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    claims.append(self.claim(store, digest, performer=performer))
                except BaseException as error:  # report the exact concurrent failure below
                    failures.append(error)

            threads = [threading.Thread(target=contend, args=(stores[0], "worker-one")),
                       threading.Thread(target=contend, args=(stores[1], "worker-two"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads), "claim contention deadlocked")
            self.assertEqual(failures, [])
            successful = [claim for claim in claims if claim is not None]
            self.assertEqual(len(successful), 1)
            self.assertEqual(seed.get_work_unit("one-unit")["current_attempt_id"], successful[0]["attempt_id"])

    def test_foreign_and_recovered_lease_cannot_mutate_work(self):
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "state.sqlite")
            store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
            claim = self.claim(store, digest, lease_seconds=1)
            assert claim is not None
            with self.assertRaisesRegex(AutonomyError, "owner or lease token"):
                store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker-two", lease_token=claim["lease_token"], at=NOW)
            with self.assertRaisesRegex(AutonomyError, "owner or lease token"):
                store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker-two", lease_token=claim["lease_token"], outcome="blocked", at=NOW)
            self.assertEqual(store.recover_expired_leases(at=NOW + timedelta(seconds=1)), [claim["attempt_id"]])
            for operation in ("heartbeat", "finish"):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(AutonomyError, "stale"):
                        if operation == "heartbeat":
                            store.heartbeat(attempt_id=claim["attempt_id"], performer_id="worker-one", lease_token=claim["lease_token"], at=NOW + timedelta(seconds=2))
                        else:
                            store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker-one", lease_token=claim["lease_token"], outcome="blocked", at=NOW + timedelta(seconds=2))

    def test_each_failure_classification_is_durable_and_distinct(self):
        expected = {
            "transient": ("retry", "retry-wait"),
            "permanent": ("failed", "failed"),
            "blocked": ("blocked", "blocked"),
            "approval-required": ("approval-required", "approval-required"),
            "exhausted": ("exhausted", "exhausted"),
        }
        for outcome, (attempt_outcome, unit_status) in expected.items():
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                store, digest = self.build_active_store(Path(directory) / "state.sqlite")
                store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
                claim = self.claim(store, digest)
                assert claim is not None
                result = store.finish_attempt(
                    attempt_id=claim["attempt_id"], performer_id="worker-one", lease_token=claim["lease_token"],
                    outcome=outcome, outcome_evidence={"classification": outcome}, at=NOW + timedelta(seconds=1),
                )
                self.assertEqual(result["outcome"], attempt_outcome)
                self.assertEqual(store.get_work_unit("unit-a")["status"], unit_status)

    def test_budget_limits_fail_closed_before_or_at_the_boundary(self):
        # Token reservations cannot cross the cap, and rejected reporting does
        # not consume or release a still-valid lease.
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "tokens.sqlite", tokens=5)
            store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
            with self.assertRaisesRegex(AutonomyError, "token budget"):
                self.claim(store, digest, token_reservation=6)
            self.assertEqual(store.budget_summary("goal-a")["consumed_attempts"], 0)
            claim = self.claim(store, digest, token_reservation=5)
            assert claim is not None
            with self.assertRaisesRegex(AutonomyError, "exceeds the reservation"):
                store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker-one", lease_token=claim["lease_token"], outcome="blocked", tokens_consumed=6, at=NOW + timedelta(seconds=1))
            self.assertEqual(store.get_work_unit("unit-a")["status"], "leased")

        # Concurrent leases respect the global cap even with separately eligible work.
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "concurrency.sqlite", concurrency=1)
            for identifier in ("unit-a", "unit-b"):
                store.create_work_unit(goal_id="goal-a", work_unit_id=identifier, title=identifier, scope={"paths": ["src"], "exclusions": []})
            self.assertIsNotNone(self.claim(store, digest))
            self.assertIsNone(self.claim(store, digest, performer="worker-two"))
            self.assertEqual(store.budget_summary("goal-a")["consumed_attempts"], 1)

        # A final transient failure exhausts an attempt budget rather than re-queuing indefinitely.
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "attempts.sqlite", attempts=1)
            store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
            claim = self.claim(store, digest)
            assert claim is not None
            self.assertEqual(store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker-one", lease_token=claim["lease_token"], outcome="transient", at=NOW + timedelta(seconds=1))["outcome"], "exhausted")
            self.assertEqual(store.get_work_unit("unit-a")["status"], "exhausted")

        # Elapsed time constrains a lease and recovery closes it at the cap.
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "elapsed.sqlite", elapsed_seconds=1)
            store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
            claim = self.claim(store, digest, lease_seconds=20)
            assert claim is not None
            self.assertEqual(claim["lease_expires_at"], "2031-01-01T00:00:01Z")
            self.assertEqual(store.recover_expired_leases(at=NOW + timedelta(seconds=1)), [claim["attempt_id"]])
            self.assertEqual(store.get_work_unit("unit-a")["status"], "exhausted")

    def test_pause_resume_stop_and_emergency_stop_preserve_recoverable_state(self):
        with TemporaryDirectory() as directory:
            store, digest = self.build_active_store(Path(directory) / "state.sqlite")
            store.create_work_unit(goal_id="goal-a", work_unit_id="unit-a", title="Unit", scope={"paths": ["src"], "exclusions": []})
            first = self.claim(store, digest, token_reservation=3)
            assert first is not None
            store.pause_goal("goal-a", actor_id="safety", at=NOW + timedelta(seconds=1))
            self.assertEqual(store.get_goal("goal-a")["status"], "paused")
            self.assertEqual(store.get_work_unit("unit-a")["status"], "paused")
            with self.assertRaisesRegex(AutonomyError, "stale"):
                store.finish_attempt(attempt_id=first["attempt_id"], performer_id="worker-one", lease_token=first["lease_token"], outcome="blocked", at=NOW + timedelta(seconds=2))
            store.resume_goal("goal-a", actor_id="goal-runner", envelope_sha256=digest, at=NOW + timedelta(seconds=2))
            self.assertEqual(store.get_work_unit("unit-a")["status"], "eligible")
            second = self.claim(store, digest, at=NOW + timedelta(seconds=3))
            assert second is not None
            self.assertTrue(store.set_emergency_stop(actor_id="safety", reason="operator intervention", at=NOW + timedelta(seconds=4)))
            self.assertEqual(store.get_goal("goal-a")["status"], "paused")
            self.assertEqual(store.get_work_unit("unit-a")["status"], "paused")
            self.assertTrue(store.clear_emergency_stop(actor_id="safety", approver_kind="human", at=NOW + timedelta(seconds=5)))
            self.assertEqual(store.get_goal("goal-a")["status"], "paused", "clearing an e-stop must not silently resume work")
            store.resume_goal("goal-a", actor_id="goal-runner", envelope_sha256=digest, at=NOW + timedelta(seconds=5))
            third = self.claim(store, digest, at=NOW + timedelta(seconds=6))
            assert third is not None
            store.stop_goal("goal-a", actor_id="safety", at=NOW + timedelta(seconds=7))
            self.assertEqual((store.get_goal("goal-a")["status"], store.get_work_unit("unit-a")["status"]), ("stopped", "stopped"))
            with self.assertRaisesRegex(Exception, "Cannot transition"):
                store.resume_goal("goal-a", actor_id="goal-runner", envelope_sha256=digest, at=NOW + timedelta(seconds=8))

    def test_audit_hash_check_detects_persisted_tampering(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = AutonomyStore(path)
            store.create_goal(goal_id="goal-a", title="Goal", description="Audit")
            connection = sqlite3.connect(path)
            try:
                # This simulates a database-level compromise which bypasses
                # the ordinary immutable-row trigger; the chain must still
                # expose the changed event to diagnostics.
                connection.execute("DROP TRIGGER audit_events_no_update")
                connection.execute("UPDATE audit_events SET payload='{}' WHERE sequence=1")
                connection.commit()
            finally:
                connection.close()
            diagnosis = store.verify_audit()
            self.assertFalse(diagnosis["ok"])
            self.assertIn("audit hash mismatch at sequence 1", diagnosis["issues"])

    def test_cli_rejects_ambiguous_json_and_only_accepts_lease_tokens_from_environment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")
            code, payload = run_cli("contract", "--root", str(root), "goal-a", str(duplicate), "--actor", "owner")
            self.assertEqual(code, 2)
            self.assertIn("duplicate JSON key", payload["error"])
            nonfinite = root / "nonfinite.json"
            nonfinite.write_text('{"number":NaN}', encoding="utf-8")
            code, payload = run_cli("contract", "--root", str(root), "goal-a", str(nonfinite), "--actor", "owner")
            self.assertEqual(code, 2)
            self.assertIn("non-finite JSON value", payload["error"])
            oversized = root / "oversized.json"
            oversized.write_bytes(b"{" + b"x" * (64 * 1024) + b"}")
            code, payload = run_cli("contract", "--root", str(root), "goal-a", str(oversized), "--actor", "owner")
            self.assertEqual(code, 2)
            self.assertIn("exceeds 64KiB", payload["error"])

            with self.assertRaises(SystemExit) as rejected_secret_argument:
                with redirect_stderr(io.StringIO()):
                    build_parser().parse_args(["work", "heartbeat", "attempt-a", "--actor", "worker-one", "--lease-token", "secret"])
            self.assertEqual(rejected_secret_argument.exception.code, 2)
            with patch.dict(os.environ, {"TASKTRA_LEASE_TOKEN": ""}, clear=False):
                code, payload = run_cli("work", "--root", str(root), "heartbeat", "attempt-a", "--actor", "worker-one")
            self.assertEqual(code, 2)
            self.assertIn("lease token environment variable is unset", payload["error"])

    def test_v2_migration_creates_a_recoverable_backup_before_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT, work_unit_id TEXT, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
                connection.execute("INSERT INTO events(goal_id,work_unit_id,event_type,payload,created_at) VALUES(NULL,NULL,'legacy.event','{}','2030-01-01T00:00:00Z')")
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(AutonomyStore(path).migrate(), SCHEMA_VERSION)
            backup = path.with_name("state.sqlite.v2.bak")
            self.assertTrue(backup.is_file())
            snapshot = sqlite3.connect(backup)
            try:
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(snapshot.execute("SELECT event_type FROM events").fetchone()[0], "legacy.event")
            finally:
                snapshot.close()
