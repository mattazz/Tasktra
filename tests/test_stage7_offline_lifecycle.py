"""Offline-only Stage 7 lifecycle acceptance coverage.

This test deliberately exercises the public CLI rather than the SQLite store:
an isolated project must remain able to finish a real local goal when every
optional remote capability is absent.  Network entry points are guarded so a
future accidental probe turns this into a deterministic failure.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.request

from tasktra.authority import (
    authority_envelope_sha256,
    transition_approval_subject_sha256,
)
from tasktra.cli import main


class NetworkUseForbidden(AssertionError):
    pass


def _forbid_network(*_args: object, **_kwargs: object) -> None:
    raise NetworkUseForbidden("the Stage 7 offline lifecycle must not use the network")


def invoke(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(arguments)
        except SystemExit as error:
            code = int(error.code)
    return code, stdout.getvalue(), stderr.getvalue()


class StageSevenOfflineLifecycleTests(unittest.TestCase):
    goal_id = "offline-lifecycle"
    work_unit_id = "offline-work"
    owner = "local-owner"
    operator = "local-operator"
    worker = "local-worker"

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.network = ExitStack()
        self.network.enter_context(patch.object(socket, "create_connection", _forbid_network))
        self.network.enter_context(patch.object(socket.socket, "connect", _forbid_network))
        self.network.enter_context(patch.object(urllib.request, "urlopen", _forbid_network))

    def tearDown(self) -> None:
        self.network.close()
        self.directory.cleanup()

    def command(self, *arguments: str) -> dict:
        code, stdout, stderr = invoke(*arguments)
        self.assertEqual(code, 0, stderr or stdout)
        return json.loads(stdout)

    def write_json(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def approval(self, *, approval_id: str, action: str, performer: str,
                 envelope_sha256: str, work_unit_id: str | None = None,
                 evidence: list[dict] | None = None) -> dict:
        """Build the current local-human-ceremony approval document."""
        value = {
            "kind": "tasktra.transition-approval",
            "version": 3,
            "approval_id": approval_id,
            "goal_id": self.goal_id,
            "work_unit_id": work_unit_id,
            "action": action,
            "effect": "local-reversible-write",
            "scope": {"paths": ["."], "exclusions": []},
            "resource_scope": None,
            "envelope_sha256": envelope_sha256,
            "decision": "approved",
            "approver": {"kind": "human", "id": self.owner},
            "performer_id": performer,
            "authority_clause": "The local owner explicitly approved this bounded local transition.",
            "evidence": evidence or [],
            "provenance": {
                "kind": "local-human-ceremony",
                "attester_id": self.owner,
                "subject_sha256": "0" * 64,
                "attested_at": "2035-01-01T00:00:00Z",
            },
            "valid_until": "2035-01-02T00:00:00Z",
            "revoked_at": None,
        }
        value["provenance"]["subject_sha256"] = transition_approval_subject_sha256(value)
        return value

    def record_human_approval(self, filename: str, value: dict) -> dict:
        """Exercise the CLI's local-TTY ceremony with its exact subject hash."""
        path = self.write_json(filename, value)
        subject_sha256 = transition_approval_subject_sha256(value)
        with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=subject_sha256):
            result = self.command(
                "approval", "--root", str(self.root), "record", str(path), "--human-actor", self.owner,
            )
        self.assertNotEqual(result["approval"]["provenance"]["attested_at"], "2035-01-01T00:00:00Z")
        self.assertEqual(result["approval"]["provenance"]["subject_sha256"], subject_sha256)
        return result

    def handoff(self, *, role: str, actor: str, handoff_id: str) -> dict:
        evidence_id = f"{role}-check"
        return {
            "kind": "tasktra.handoff",
            "version": 1,
            "handoff_id": handoff_id,
            "source": {"goal_id": self.goal_id, "work_unit_id": self.work_unit_id},
            "human_summary": f"{role.title()} completed its bounded local responsibility.",
            "producer": {"role": role, "actor_id": actor},
            "status": {"state": "completed", "summary": "The local workflow stage completed."},
            "verified_facts": [{"statement": "The bounded local lifecycle check passed.", "evidence_ids": [evidence_id]}],
            "inferences": [],
            "changed_paths": [],
            "validation_results": [{"name": f"{role}-check", "outcome": "passed", "detail": "Offline evidence recorded.", "evidence_ids": [evidence_id]}],
            "evidence_refs": [{"id": evidence_id, "kind": "command", "locator": "python -m unittest", "summary": "Focused local check."}],
            "blockers": [],
            "downstream_brief": {"objective": "Advance the local workflow.", "context": [], "constraints": ["Remain offline."], "recommended_next_steps": []},
            "requested_actions": [],
        }

    def test_offline_authorized_goal_runs_to_auditable_completion(self) -> None:
        initialized = self.command("init", "--root", str(self.root), "--name", "Offline lifecycle", "--apply")
        self.assertTrue(initialized["ok"])

        capabilities = self.command("capabilities", "--root", str(self.root))
        self.assertTrue(capabilities["local_work_can_continue"])
        self.assertEqual(capabilities["provider_health_source"], "offline-default")
        by_id = {item["id"]: item for item in capabilities["capabilities"]}
        for capability_id in ("github-cli-adapter", "jira-connector-adapter", "research-adapter", "scheduler-host"):
            with self.subTest(capability=capability_id):
                self.assertFalse(by_id[capability_id]["available"])
                self.assertEqual(by_id[capability_id]["state"], "unavailable")

        created = self.command(
            "goal", "--root", str(self.root), "create", "Complete an offline lifecycle",
            "Prove the local lifecycle can complete with no remote account.", "--id", self.goal_id,
            "--acceptance", "The offline lifecycle is complete.",
        )
        self.assertEqual(created["goal"]["status"], "planned")
        envelope = {
            "kind": "tasktra.authority-envelope",
            "version": 1,
            "goal_id": self.goal_id,
            "outcome": "Complete one local work unit without optional remote capabilities.",
            "motivation": "Stage 7 offline lifecycle acceptance coverage.",
            "author_id": self.owner,
            "acceptance_criteria": [{"id": "offline-complete", "statement": "The offline lifecycle is complete."}],
            "scope": {"paths": ["."], "exclusions": []},
            "allowed_actions": ["goal-activate", "work-claim", "work-complete", "goal-complete"],
            "allowed_effects": ["local-reversible-write"],
            "prohibited_actions": ["remote-mutation", "external-communication"],
            "quality_requirements": ["Use a completed implementation, test, and review workflow."],
            "budgets": {"tokens": 0, "attempts": 1, "elapsed_seconds": 300, "concurrency": 1},
            "dependencies": [],
            "checkpoints": [],
            "stop_conditions": [],
            "escalation_conditions": [],
        }
        digest = authority_envelope_sha256(envelope)
        self.command("contract", "--root", str(self.root), self.goal_id, str(self.write_json("authority.json", envelope)), "--actor", self.owner)

        self.record_human_approval(
            "activate-approval.json", self.approval(approval_id="activate-approval", action="goal-activate", performer=self.operator, envelope_sha256=digest),
        )
        active = self.command("goal", "--root", str(self.root), "activate", self.goal_id, "--actor", self.operator, "--envelope-sha256", digest)
        self.assertEqual(active["goal"]["status"], "active")

        scope = self.write_json("scope.json", {"paths": ["."], "exclusions": []})
        work = self.command("work", "--root", str(self.root), "create", self.goal_id, "Complete the offline workflow", "--id", self.work_unit_id, "--scope", str(scope))
        self.assertEqual(work["work_unit"]["status"], "planned")
        self.record_human_approval(
            "claim-approval.json", self.approval(approval_id="claim-approval", action="work-claim", performer=self.worker, envelope_sha256=digest, work_unit_id=self.work_unit_id),
        )

        lease_token = "o" * 32
        with patch.dict(os.environ, {"TASKTRA_OFFLINE_LEASE": lease_token}, clear=False):
            claim = self.command(
                "work", "--root", str(self.root), "claim", self.goal_id, "--actor", self.worker,
                "--envelope-sha256", digest, "--repository", "offline-repository", "--revision", "offline-revision",
                "--branch", "offline-branch", "--workspace", "offline-workspace", "--lease-token-env", "TASKTRA_OFFLINE_LEASE",
            )
            self.assertEqual(claim["claim"]["work_unit_id"], self.work_unit_id)
            self.assertNotIn("lease_token", claim["claim"])

            workflow = self.command("workflow", "create", "--goal-id", self.goal_id, "--work-unit-id", self.work_unit_id)["workflow"]
            for role, actor in (("implementer", "offline-implementer"), ("tester", "offline-tester"), ("reviewer", "offline-reviewer")):
                workflow_path = self.write_json("workflow.json", workflow)
                handoff_path = self.write_json(f"{role}-handoff.json", self.handoff(role=role, actor=actor, handoff_id=f"{role}-handoff"))
                accepted = self.command("workflow", "accept", str(workflow_path), str(handoff_path))
                workflow = accepted["workflow"]
            self.assertTrue(accepted["complete"])
            workflow_path = self.write_json("completed-workflow.json", workflow)

            self.record_human_approval(
                "complete-work-approval.json", self.approval(approval_id="complete-work-approval", action="work-complete", performer=self.worker, envelope_sha256=digest, work_unit_id=self.work_unit_id),
            )
            finished = self.command(
                "work", "--root", str(self.root), "finish", claim["claim"]["attempt_id"], "--actor", self.worker,
                "--lease-token-env", "TASKTRA_OFFLINE_LEASE", "--outcome", "success", "--workflow", str(workflow_path),
                "--evidence-json", str(self.write_json("outcome-evidence.json", {"result": "offline success"})),
            )
        self.assertEqual(finished["finish"]["outcome"], "success")

        # A first human ceremony authorizes recording the local evidence.  A
        # fresh final ceremony below binds the immutable evidence hash before
        # the goal itself can complete.
        self.record_human_approval(
            "record-evidence-approval.json", self.approval(approval_id="record-evidence-approval", action="goal-complete", performer=self.operator, envelope_sha256=digest),
        )
        evidence = self.command(
            "acceptance-evidence", "--root", str(self.root), self.goal_id, "offline-complete", "--actor", self.operator,
            "--envelope-sha256", digest, "--evidence", str(self.write_json("acceptance.json", {"workflow": "completed-workflow.json", "mode": "offline"})),
        )
        self.assertEqual(evidence["evidence"]["criterion_id"], "offline-complete")
        self.record_human_approval(
            "complete-goal-approval.json", self.approval(
                approval_id="complete-goal-approval", action="goal-complete", performer=self.operator,
                envelope_sha256=digest,
                evidence=[{
                    "kind": "acceptance-evidence", "id": "offline-complete",
                    "sha256": sha256(evidence["evidence"]["evidence_json"].encode("utf-8")).hexdigest(),
                }],
            ),
        )
        completed = self.command("goal", "--root", str(self.root), "complete", self.goal_id, "--actor", self.operator, "--envelope-sha256", digest)
        self.assertEqual(completed["goal"]["status"], "complete")

        audit = self.command("audit", "--root", str(self.root), "verify")
        self.assertTrue(audit["audit"]["ok"], audit)
        exported = self.command("audit", "--root", str(self.root), "export", "--goal-id", self.goal_id, "--limit", "100")
        event_types = {event["event_type"] for event in exported["audit"]["events"]}
        self.assertTrue({"goal.created", "goal.active", "work.claimed", "work.finished", "acceptance.evidence_recorded", "goal.completed"}.issubset(event_types))


if __name__ == "__main__":
    unittest.main()
