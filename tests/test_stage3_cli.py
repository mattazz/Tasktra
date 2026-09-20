"""Focused black-box coverage for Stage 3 runtime CLI controls."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.cli import main
from tasktra.config import load_project_config
from tasktra.state import SCHEMA_VERSION, StateError, StateStore, _now


def invoke(*arguments: str) -> tuple[int, str, str]:
    """Run the CLI in process while preserving parser errors for assertions."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(arguments)
        except SystemExit as error:
            code = int(error.code)
    return code, stdout.getvalue(), stderr.getvalue()


def payload(*arguments: str) -> tuple[int, dict]:
    code, stdout, stderr = invoke(*arguments)
    return code, json.loads(stdout or stderr)


def authority(goal_id: str, *, checkpoints: list[str] | None = None) -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1, "goal_id": goal_id,
        "outcome": "Exercise local controls.", "motivation": "CLI integration coverage.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "work-claim", "work-complete", "work-requeue", "goal-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 100, "attempts": 5, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": [], "checkpoints": checkpoints or [], "stop_conditions": [], "escalation_conditions": [],
    }


class Stage3CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        code, result = payload("init", "--root", str(self.root), "--name", "CLI test", "--apply")
        self.assertEqual((code, result["ok"]), (0, True))

    def tearDown(self) -> None:
        self.directory.cleanup()

    @property
    def database(self) -> Path:
        return load_project_config(self.root).database_path(self.root)

    def _seed_active_goal(self, *, goal_id: str = "goal-one", checkpoints: list[str] | None = None) -> tuple[AutonomyStore, dict, str]:
        store = AutonomyStore(self.database)
        contract = authority(goal_id, checkpoints=checkpoints)
        digest = authority_envelope_sha256(contract)
        store.create_goal(goal_id=goal_id, title="Goal", description="Goal", acceptance=["Done."])
        store.define_goal_contract(goal_id, contract, actor_id="owner")
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        store.record_transition_approval(goal_id=goal_id, action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                         envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry)
        store.record_transition_approval(goal_id=goal_id, action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                         envelope_sha256=digest, approver_id="human", performer_id="worker", valid_until=expiry)
        store.activate_goal(goal_id, actor_id="owner", envelope_sha256=digest)
        return store, contract, digest

    def test_parser_requires_exact_subcommands_and_closed_work_scope(self) -> None:
        code, _, stderr = invoke("sta", "--root", str(self.root), "migrate", "--preview")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", stderr)
        code, _, stderr = invoke("work", "--root", str(self.root), "create", "goal-one", "Work")
        self.assertEqual(code, 2)
        self.assertIn("--scope", stderr)

    def test_claim_reads_caller_token_from_environment_without_printing_or_returning_it(self) -> None:
        _, _, digest = self._seed_active_goal()
        scope_path = self.root / "scope.json"
        scope_path.write_text(json.dumps({"paths": ["src"], "exclusions": []}), encoding="utf-8")
        code, created = payload("work", "--root", str(self.root), "create", "goal-one", "Work", "--id", "work-one", "--scope", str(scope_path))
        self.assertEqual((code, created["work_unit"]["id"]), (0, "work-one"))
        secret = "z" * 40
        with patch.dict(os.environ, {"TASKTRA_TEST_LEASE": secret}, clear=False):
            code, stdout, stderr = invoke(
                "work", "--root", str(self.root), "claim", "goal-one", "--actor", "worker",
                "--envelope-sha256", digest, "--repository", "repo", "--revision", "rev",
                "--branch", "main", "--workspace", "work", "--lease-token-env", "TASKTRA_TEST_LEASE",
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertNotIn(secret, stdout)
        result = json.loads(stdout)
        self.assertNotIn("lease_token", result["claim"])
        self.assertEqual(result["claim"]["work_unit_id"], "work-one")

    def test_requeue_cli_requires_audited_authority_and_evidence(self) -> None:
        store, _, digest = self._seed_active_goal()
        store.create_work_unit(
            goal_id="goal-one", work_unit_id="work-one", title="Work",
            scope={"paths": ["src"], "exclusions": []},
        )
        claim = store.claim_next_work(
            goal_id="goal-one", performer_id="worker", envelope_sha256=digest,
            repository="repo", revision="rev", branch="main", workspace="work",
        )
        store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker",
            lease_token=claim["lease_token"], outcome="blocked",
        )
        store.record_transition_approval(
            goal_id="goal-one", work_unit_id="work-one", action="work-requeue",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest,
            approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=datetime.now(timezone.utc) + timedelta(days=1),
        )
        evidence_path = self.root / "requeue.json"
        evidence_path.write_text(json.dumps({"unblocked_by": "decision-one"}), encoding="utf-8")
        code, result = payload(
            "work", "--root", str(self.root), "requeue", "work-one",
            "--actor", "worker", "--envelope-sha256", digest,
            "--evidence-json", str(evidence_path),
        )
        self.assertEqual((code, result["work_unit"]["status"]), (0, "eligible"))
        self.assertEqual(len(result["work_unit"]["requeue_evidence_sha256"]), 64)

    def test_state_migration_repair_attestation_and_checkpoint_assignment(self) -> None:
        store = StateStore(self.database)
        contract = authority("goal-one")
        digest = authority_envelope_sha256(contract)
        store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        store.define_goal_contract("goal-one", contract, actor_id="owner")
        original = store.record_transition_approval(
            goal_id="goal-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="worker",
            approval_id="approval-one", scope={"paths": ["."], "exclusions": []},
            valid_until="2031-01-01T00:00:00Z",
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DROP TABLE schedule_resume_idempotency")
            connection.execute("PRAGMA user_version=9")
            StateStore._seal_current_state_in_transaction(connection, _now(), existing_only=True)
            connection.commit()
        finally:
            connection.close()

        code, preview = payload("state", "--root", str(self.root), "migrate", "--preview")
        self.assertEqual((code, preview["current_schema"], preview["target_schema"]), (0, 9, SCHEMA_VERSION))
        code, applied = payload("state", "--root", str(self.root), "migrate", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("authority-bound", applied["error"])
        self.assertEqual(StateStore(self.database).migrate(), SCHEMA_VERSION)

        # Repair remains available for a current-schema approval explicitly
        # left unsealed for human recovery, but migration itself no longer
        # accepts an unsealed legacy ledger.
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE transition_approvals SET scope='{}' WHERE id='approval-one'")
            connection.execute("DROP TRIGGER authority_seals_no_delete")
            connection.execute("DELETE FROM authority_seals WHERE table_name='transition_approvals' AND row_id='approval-one'")
            connection.execute("CREATE TRIGGER authority_seals_no_delete BEFORE DELETE ON authority_seals BEGIN SELECT RAISE(ABORT, 'authority seals are immutable'); END")
            connection.commit()
        finally:
            connection.close()

        replacement = {
            "kind": "tasktra.transition-approval", "version": 1, "approval_id": "approval-one",
            "goal_id": "goal-one", "work_unit_id": None, "action": "work-claim", "effect": LOCAL_REVERSIBLE_WRITE,
            "scope": {"paths": ["."], "exclusions": []}, "envelope_sha256": digest,
            "decision": "approved", "approver": {"kind": "human", "id": "human"}, "performer_id": "worker",
            "authority_clause": original["authority_clause"], "evidence": [], "valid_until": original["valid_until"], "revoked_at": None,
        }
        replacement_path = self.root / "replacement-approval.json"
        replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
        code, repaired = payload("approval", "--root", str(self.root), "repair-scope", "approval-one", str(replacement_path), "--human-actor", "human")
        self.assertEqual((code, repaired["approval"]["scope"]), (0, {"paths": ["."], "exclusions": []}))
        code, attested = payload("state", "--root", str(self.root), "attest-ledger", "--human-actor", "human")
        self.assertEqual((code, attested["action"]), (0, "state-attest-ledger"))
        self.assertGreater(attested["attestation"]["sealed_row_count"], 0)

        checkpoint_contract = authority("goal-two", checkpoints=["first"])
        current = StateStore(self.database)
        current.create_goal(goal_id="goal-two", title="Goal", description="Goal", acceptance=["Done."])
        current.define_goal_contract("goal-two", checkpoint_contract, actor_id="owner")
        checkpoint_scope = self.root / "checkpoint-scope.json"
        checkpoint_scope.write_text(json.dumps({"paths": ["."], "exclusions": []}), encoding="utf-8")
        code, created = payload("work", "--root", str(self.root), "create", "goal-two", "Legacy", "--id", "legacy-one", "--scope", str(checkpoint_scope), "--checkpoint", "first")
        self.assertEqual(code, 0, created)
        self.assertEqual(created["work_unit"]["checkpoint_id"], "first")
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DROP TABLE schedule_resume_idempotency")
            connection.execute("PRAGMA user_version=9")
            StateStore._seal_current_state_in_transaction(connection, _now(), existing_only=True)
            connection.commit()
        finally:
            connection.close()
        blocked_code, blocked = payload("state", "--root", str(self.root), "migrate", "--apply")
        self.assertEqual(blocked_code, 2)
        self.assertIn("authority-bound", blocked["error"])
        self.assertEqual(StateStore(self.database).migrate(), SCHEMA_VERSION)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE work_units SET checkpoint_id=NULL WHERE id='legacy-one'")
            connection.execute("DROP TRIGGER authority_seals_no_delete")
            connection.execute("DELETE FROM authority_seals WHERE table_name='work_units' AND row_id='legacy-one'")
            connection.execute("CREATE TRIGGER authority_seals_no_delete BEFORE DELETE ON authority_seals BEGIN SELECT RAISE(ABORT, 'authority seals are immutable'); END")
            connection.commit()
        finally:
            connection.close()
        code, assigned = payload("work", "--root", str(self.root), "assign-checkpoint", "legacy-one", "first", "--human-actor", "human")
        self.assertEqual((code, assigned["work_unit"]["checkpoint_id"]), (0, "first"))
        self.assertEqual(payload("state", "--root", str(self.root), "attest-ledger", "--human-actor", "human")[0], 0)

    def test_human_only_emergency_clear_and_bounded_operational_views(self) -> None:
        code, stopped = payload("runtime", "--root", str(self.root), "emergency-stop", "--actor", "operator", "--reason", "test")
        self.assertEqual((code, stopped["changed"]), (0, True))
        code, _, stderr = invoke("runtime", "--root", str(self.root), "clear-emergency-stop", "--actor", "operator")
        self.assertEqual(code, 2)
        self.assertIn("--actor-kind", stderr)
        code, cleared = payload("runtime", "--root", str(self.root), "clear-emergency-stop", "--actor", "operator", "--actor-kind", "human")
        self.assertEqual((code, cleared["changed"]), (0, True))
        code, status = payload("status", "--root", str(self.root), "--detail-limit", "0")
        self.assertEqual((code, status["runtime"]["schema_version"]), (0, SCHEMA_VERSION))
        code, exported = payload("audit", "--root", str(self.root), "export", "--limit", "1", "--after-sequence", "0")
        self.assertEqual((code, exported["audit"]["limit"]), (0, 1))
        self.assertLessEqual(len(exported["audit"]["events"]), 1)
        code, rejected = payload("audit", "--root", str(self.root), "export", "--limit", "201")
        self.assertEqual(code, 2)
        self.assertIn("between 1 and 200", rejected["error"])
