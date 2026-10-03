"""CLI integration for verification selection, recovery, and bounded host runs."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tasktra.authority import authority_envelope_sha256, transition_approval_subject_sha256
from tasktra.autonomy import AutonomyStore
from tasktra.config import initialize_project, load_project_config
from tasktra.migrations import CommittedRuntimeRecoveryRequired
from tasktra.state import StateStore
from tasktra.upgrades import upgrade_plan_digest
from tests.test_stage6_cli import payload


class ProgramCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = {
            "ok": True, "action": "upgrade-preview", "mutation": "none", "target": {"packs": []},
            "managed_writes": [], "migration_steps": [], "validation_commands": [],
        }
        initialize_project(self.root, name="CLI program integration")
        database = load_project_config(self.root).database_path(self.root)
        StateStore(database).migrate()
        self.store = AutonomyStore(database)
        self.contract = {
            "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "program-goal",
            "outcome": "Complete bounded integration work", "motivation": "CLI integration coverage",
            "author_id": "owner", "acceptance_criteria": [{"id": "done", "statement": "Changes verified"}],
            "scope": {"paths": ["."], "exclusions": []},
            "allowed_actions": [
                "goal-activate", "local-effect", "work-claim", "effect-recovery-resolve",
                "verify-research-review", "verify-documentation-review", "verify-deterministic-direct",
            ],
            "allowed_effects": ["local-reversible-write"], "prohibited_actions": [],
            "quality_requirements": [],
            "budgets": {"tokens": 1000, "attempts": 10, "elapsed_seconds": 600, "concurrency": 1},
            "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
        }
        self.digest = authority_envelope_sha256(self.contract)
        self.store.create_goal(goal_id="program-goal", title="Program", description="Integration", acceptance=["Changes verified"])
        self.store.define_goal_contract("program-goal", self.contract, actor_id="owner")
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.approve("goal-activate", unit=None)
        self.store.activate_goal("program-goal", actor_id="worker", envelope_sha256=self.digest)
        self.store.create_work_unit(goal_id="program-goal", work_unit_id="program-work", title="Work", scope={"paths": ["."], "exclusions": []})
        self.approve("local-effect")

    def approve(self, action, *, unit="program-work"):
        return self.store.record_transition_approval(
            goal_id="program-goal", work_unit_id=unit, action=action, effect="local-reversible-write",
            envelope_sha256=self.digest, approver_id="human", performer_id="worker", valid_until=self.expiry,
        )

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def upgrade_arguments(self, action, key):
        arguments = [
            "upgrade", "--root", str(self.root), action, "--goal-id", "program-goal",
            "--work-unit-id", "program-work", "--envelope-sha256", self.digest,
            "--actor", "worker", "--idempotency-key", key, "--confirm",
        ]
        if action == "apply":
            arguments.extend(["--plan-sha256", upgrade_plan_digest(self.plan)])
        else:
            arguments.extend(["--snapshot-plan-sha256", "a" * 64, "--before-sha256", "b" * 64])
        return arguments

    def test_late_upgrade_failure_keeps_recovery_evidence_and_blocks_redispatch(self):
        verification = {"runtime_schema_changed": True, "runtime_schema_before": 9, "runtime_schema_after": 10}
        error = CommittedRuntimeRecoveryRequired("runtime committed; recovery required", verification)
        for action, function in (("apply", "apply_upgrade"), ("rollback", "rollback_upgrade")):
            with self.subTest(action=action):
                key = f"late-{action}"
                arguments = self.upgrade_arguments(action, key)
                with patch("tasktra.cli._upgrade_preview", return_value=(self.root, None, None, self.root / "catalog", self.plan)), patch(f"tasktra.cli.{function}", side_effect=error) as mutation:
                    code, result = payload(*arguments)
                    self.assertEqual(code, 2)
                    self.assertTrue(result["recovery_required"])
                    effect = self.store.inspect_effect(key)
                    self.assertEqual(effect["status"], "recovery-required")
                    self.assertEqual(effect["receipt"]["outcome"], "recovery-required")
                    self.assertEqual(json.loads(effect["receipt"]["evidence_json"])["verification"], verification)
                    retry_code, retry = payload(*arguments)
                    self.assertEqual(retry_code, 2)
                    self.assertIn("not dispatchable", retry["error"])
                    self.assertEqual(mutation.call_count, 1)

    def test_unspecified_upgrade_failure_is_indeterminate(self):
        for action, function in (("apply", "apply_upgrade"), ("rollback", "rollback_upgrade")):
            with self.subTest(action=action):
                key = f"unknown-{action}"
                with patch("tasktra.cli._upgrade_preview", return_value=(self.root, None, None, self.root / "catalog", self.plan)), patch(f"tasktra.cli.{function}", side_effect=ValueError("unclassified late failure")):
                    code, _ = payload(*self.upgrade_arguments(action, key))
                self.assertEqual(code, 2)
                effect = self.store.inspect_effect(key)
                self.assertEqual(effect["status"], "recovery-required")
                self.assertEqual(effect["receipt"]["outcome"], "indeterminate")

    def test_effect_recovery_command_requires_approval_and_retains_original_receipt(self):
        self.store.prepare_effect(
            idempotency_key="recover-cli", goal_id="program-goal", work_unit_id="program-work",
            effect_class="local-reversible-write", operation="local-effect", request={"action": "check"},
            envelope_sha256=self.digest, performer_id="worker",
        )
        self.store.record_effect_receipt(idempotency_key="recover-cli", outcome="indeterminate", evidence={"partial": True}, performer_id="worker")
        evidence = self.write_json("recovery.json", {"verified": "local state inspected"})
        arguments = [
            "effect", "--root", str(self.root), "resolve-recovery", "recover-cli", "--resolution", "applied",
            "--evidence", evidence, "--actor", "worker", "--envelope-sha256", self.digest,
        ]
        denied_code, denied = payload(*arguments)
        self.assertEqual(denied_code, 2)
        self.assertIn("approval", denied["error"])
        self.assertEqual(self.store.inspect_effect("recover-cli")["status"], "recovery-required")
        self.approve("effect-recovery-resolve")
        code, result = payload(*arguments)
        self.assertEqual(code, 0)
        self.assertEqual(result["effect"]["status"], "received")
        self.assertEqual(result["effect"]["receipt"]["outcome"], "indeterminate")

    def test_work_and_workflow_commands_accept_explicit_verification_policies(self):
        scope = self.write_json("scope.json", {"paths": ["."], "exclusions": []})
        for policy in ("implementation-review", "research-review", "documentation-review", "deterministic-direct"):
            with self.subTest(policy=policy):
                code, result = payload(
                    "work", "--root", str(self.root), "create", "program-goal", "Scoped work", "--id", f"unit-{policy}",
                    "--scope", scope, "--verification-policy", policy,
                )
                self.assertEqual(code, 0, result)
                self.assertEqual(result["work_unit"]["verification_policy"], policy)
                code, result = payload("workflow", "create", "--goal-id", "program-goal", "--verification-policy", policy)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["workflow"].get("verification_policy", "implementation-review"), policy)
        code, legacy = payload("workflow", "create", "--goal-id", "program-goal")
        self.assertEqual((code, legacy["workflow"]["version"]), (0, 1))

    def test_codex_message_attestation_accepts_natural_authorization_and_exact_hash(self):
        message = "Ok resolve these\n"
        approval = {
            "kind": "tasktra.transition-approval", "version": 4, "approval_id": "natural-message",
            "goal_id": "program-goal", "work_unit_id": "program-work", "action": "work-claim",
            "effect": "local-reversible-write", "scope": {"paths": ["."], "exclusions": []}, "resource_scope": None,
            "envelope_sha256": self.digest, "decision": "approved", "approver": {"kind": "human", "id": "human"},
            "performer_id": "worker", "authority_clause": "Human authorized the bounded work in Codex", "evidence": [],
            "valid_until": self.expiry.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "revoked_at": None,
            "provenance": {
                "kind": "codex-user-message", "attester_id": "human", "subject_sha256": "0" * 64,
                "message_sha256": "0" * 64, "attested_at": "2030-01-01T00:00:00Z",
            },
        }
        approval["provenance"]["subject_sha256"] = transition_approval_subject_sha256(approval)
        path = self.write_json("approval.json", approval)
        arguments = ["approval", "--root", str(self.root), "record", path, "--human-actor", "human"]
        for invalid in ("", "   ", "x" * 2001):
            with self.subTest(message_length=len(invalid)):
                self.assertEqual(payload(*arguments, "--codex-user-message", invalid)[0], 2)
        code, result = payload(*arguments, "--codex-user-message", message)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["approval"]["provenance"]["message_sha256"], sha256(message.encode()).hexdigest())

    def test_run_is_preview_by_default_passes_pins_and_returns_failure_status(self):
        run = Mock(return_value={"ok": True, "action": "run-preview"})
        arguments = ["run", "--root", str(self.root), "--goal-id", "program-goal", "--work-unit-id", "program-work", "--actor", "worker", "--envelope-sha256", self.digest]
        with patch.dict("sys.modules", {"tasktra.supervisor": SimpleNamespace(run_work=run)}), patch("tasktra.cli._execution_profile", return_value=("configured-model", "high")) as profile:
            code, _ = payload(*arguments)
            self.assertEqual(code, 0)
            kwargs = run.call_args.kwargs
            self.assertFalse(kwargs["apply"])
            self.assertEqual((kwargs["token_reservation"], kwargs["timeout_seconds"]), (100_000, 900))
            self.assertEqual(kwargs["profile_for_role"]("writer"), ("configured-model", "high"))
            profile.assert_called_once_with(self.root.resolve(), "writer")
            run.return_value = {"ok": False, "action": "run-failed"}
            code, _ = payload(*arguments, "--apply", "--token-reservation", "500", "--timeout", "30")
            self.assertEqual(code, 1)
            self.assertTrue(run.call_args.kwargs["apply"])
            self.assertEqual((run.call_args.kwargs["token_reservation"], run.call_args.kwargs["timeout_seconds"]), (500, 30))
            run.side_effect = ValueError("host run unavailable")
            code, result = payload(*arguments)
            self.assertEqual(code, 2)
            self.assertIn("unavailable", result["error"])

    def test_benchmark_unverified_quality_cannot_return_success(self):
        before = self.write_json("baseline.json", [{"scenario": "no-checks", "context_tokens": 100}])
        after = self.write_json("candidate.json", [{"scenario": "no-checks", "context_tokens": 0}])
        code, result = payload("benchmark", before, after)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["has_unverified_quality"])


if __name__ == "__main__":
    unittest.main()
