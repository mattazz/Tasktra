"""Adversarial Stage 3 dependency and durable checkpoint gates."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra.handoffs import HANDOFF_KIND, HANDOFF_VERSION
from tasktra.state import StateError
from tasktra.workflow import accept_handoff, new_workflow, workflow_completion_token
from tests.approval_helpers import v3_approval_kwargs


NOW = datetime(2033, 1, 1, tzinfo=timezone.utc)


def authority(goal_id: str, *, dependencies: list[str] | None = None, checkpoints: list[str] | None = None) -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1, "goal_id": goal_id,
        "outcome": "Deliver ordered durable work.", "motivation": "Prove evidence gates.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "work-claim", "work-complete", "goal-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": ["Test."],
        "budgets": {"tokens": 20, "attempts": 5, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": dependencies or [], "checkpoints": checkpoints or [],
        "stop_conditions": ["Stop when unsafe."], "escalation_conditions": ["Escalate when blocked."],
    }


def complete_workflow(goal_id: str, work_unit_id: str) -> dict:
    source = {"goal_id": goal_id, "work_unit_id": work_unit_id}
    state = new_workflow(source)
    for role in ("implementer", "tester", "reviewer"):
        handoff = {
            "kind": HANDOFF_KIND, "version": HANDOFF_VERSION, "handoff_id": f"{work_unit_id}-{role}",
            "source": source, "producer": {"role": role, "actor_id": role},
            "human_summary": "Completed.", "status": {"state": "completed", "summary": "Passed."},
            "verified_facts": [{"statement": "Passed.", "evidence_ids": ["check"]}], "inferences": [], "changed_paths": [],
            "validation_results": [{"name": "check", "outcome": "passed", "detail": "Passed.", "evidence_ids": ["check"]}],
            "evidence_refs": [{"id": "check", "kind": "command", "locator": "python -m unittest", "summary": "Passed."}],
            "blockers": [], "downstream_brief": {"objective": "Continue.", "context": [], "constraints": [], "recommended_next_steps": []},
            "requested_actions": [],
        }
        state = accept_handoff(state, handoff)
    return state


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = AutonomyStore(Path(self.directory.name) / "state.sqlite")
        self.contract = authority("goal-one", checkpoints=["first", "second"])
        self.digest = authority_envelope_sha256(self.contract)
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        self.store.define_goal_contract("goal-one", self.contract, actor_id="owner", at=NOW)
        expiry = NOW + timedelta(days=1)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", action="work-complete", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="steward", approver_kind="steward", performer_id="worker", valid_until=expiry, at=NOW)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expiry, at=NOW)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=NOW)
        for unit_id, checkpoint in (("unit-first", "first"), ("unit-second", "second")):
            self.store.create_work_unit(goal_id="goal-one", work_unit_id=unit_id, title=unit_id,
                                        scope={"paths": ["src"], "exclusions": []}, checkpoint_id=checkpoint)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def claim(self) -> dict | None:
        return self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest,
                                          repository="repo", revision="rev", branch="main", workspace="work", at=NOW)

    def finish_success(self, claim: dict) -> None:
        workflow = complete_workflow("goal-one", claim["work_unit_id"])
        self.store.finish_attempt(attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
                                  outcome="success", workflow=workflow, completion_token=workflow_completion_token(workflow), at=NOW)

    def test_missing_dependency_blocks_activation(self) -> None:
        with TemporaryDirectory() as directory:
            store = AutonomyStore(Path(directory) / "dependency.sqlite")
            contract = authority("blocked-goal", dependencies=["absent-goal"])
            digest = authority_envelope_sha256(contract)
            store.create_goal(goal_id="blocked-goal", title="Blocked", description="Blocked", acceptance=["Done."])
            store.define_goal_contract("blocked-goal", contract, actor_id="owner", at=NOW)
            store.record_transition_approval(goal_id="blocked-goal", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                                              envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=NOW + timedelta(days=1), at=NOW)
            with self.assertRaisesRegex(StateError, "dependency does not exist"):
                store.activate_goal("blocked-goal", actor_id="owner", envelope_sha256=digest, at=NOW)

    def test_later_checkpoint_cannot_be_claimed_or_skipped(self) -> None:
        claim = self.claim()
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual((claim["work_unit_id"], claim["work_unit"]["checkpoint_id"]), ("unit-first", "first"))
        self.assertEqual(self.store.get_work_unit("unit-second")["status"], "planned")

    def test_completion_records_sealed_checkpoint_evidence_then_unblocks_next(self) -> None:
        first = self.claim()
        assert first is not None
        self.finish_success(first)
        checkpoints = self.store.get_goal_checkpoints("goal-one")
        self.assertEqual([(item["checkpoint_id"], item["position"], item["status"]) for item in checkpoints],
                         [("first", 0, "reached"), ("second", 1, "pending")])
        evidence = json.loads(checkpoints[0]["evidence_json"])
        self.assertEqual(evidence["trigger_work_unit_id"], "unit-first")
        self.assertLess(len(checkpoints[0]["evidence_json"].encode("utf-8")), 64 * 1024)
        second = self.claim()
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second["work_unit_id"], "unit-second")

    def test_reached_checkpoint_rejects_new_work(self) -> None:
        first = self.claim()
        assert first is not None
        self.finish_success(first)
        with self.assertRaisesRegex(StateError, "reached checkpoint"):
            self.store.create_work_unit(
                goal_id="goal-one", work_unit_id="late-first", title="Late",
                scope={"paths": ["src"], "exclusions": []}, checkpoint_id="first",
            )

    def test_goal_completion_requires_every_checkpoint_to_be_reached(self) -> None:
        first = self.claim()
        assert first is not None
        self.finish_success(first)
        recorded = self.store.record_acceptance_evidence(
            goal_id="goal-one", criterion_id="done", evidence={"proof": True},
            performer_id="owner", envelope_sha256=self.digest, at=NOW,
        )
        binding = {
            "kind": "acceptance-evidence", "id": "done",
            "sha256": sha256(recorded["evidence_json"].encode("utf-8")).hexdigest(),
        }
        approval_kwargs = v3_approval_kwargs(
            approval_id="checkpoint-final-v3", goal_id="goal-one", work_unit_id=None,
            action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
            scope={"paths": ["."], "exclusions": []}, resource_scope=None,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=NOW + timedelta(days=1), attested_at=NOW, evidence=[binding],
        )
        self.store.record_transition_approval(
            goal_id="goal-one", action="goal-complete", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=NOW + timedelta(days=1), at=NOW, **approval_kwargs,
        )
        with self.assertRaisesRegex(AutonomyError, "incomplete work"):
            self.store.complete_goal(goal_id="goal-one", performer_id="owner", envelope_sha256=self.digest, at=NOW)

    def test_checkpoint_sql_tampering_fails_closed_before_claim(self) -> None:
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute("UPDATE goal_checkpoints SET status='reached',evidence_json='{}',reached_at='2033-01-01T00:00:00Z' WHERE goal_id='goal-one' AND checkpoint_id='first'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "authoritative state is unsealed or tampered.*goal_checkpoints"):
            self.claim()

    def test_attestation_recomputes_checkpoint_evidence_hashes(self) -> None:
        first = self.claim()
        assert first is not None
        self.finish_success(first)
        connection = sqlite3.connect(self.store.path)
        try:
            evidence = json.loads(connection.execute(
                "SELECT evidence_json FROM goal_checkpoints WHERE goal_id='goal-one' AND checkpoint_id='first'"
            ).fetchone()[0])
            evidence["work_units_sha256"] = "0" * 64
            connection.execute(
                "UPDATE goal_checkpoints SET evidence_json=? WHERE goal_id='goal-one' AND checkpoint_id='first'",
                (json.dumps(evidence, sort_keys=True, separators=(",", ":")),),
            )
            connection.execute("DROP TRIGGER authority_seals_no_update")
            connection.execute("DROP TRIGGER authority_seals_no_delete")
            connection.execute("DROP TABLE authority_seals")
            connection.execute(
                """CREATE TABLE authority_seals (
                    table_name TEXT NOT NULL, row_id TEXT NOT NULL, version INTEGER NOT NULL,
                    row_hash TEXT NOT NULL, sealed_at TEXT NOT NULL,
                    PRIMARY KEY(table_name,row_id,version)
                )"""
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "invalid checkpoint evidence"):
            self.store.attest_ledger(actor_id="human")
