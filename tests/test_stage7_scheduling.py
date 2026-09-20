"""Adversarial coverage for Stage 7 schedule preview and durable resume."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.cli import build_parser, main
from tasktra.config import initialize_project, load_project_config
from tasktra.scheduling import SchedulerError, adapters_from_health, preview_schedule, reserve_schedule_resume


def invoke(*arguments: str) -> tuple[int, dict]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def contract() -> dict:
    return {
        "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "schedule-goal",
        "outcome": "Resume bounded work", "motivation": "Preview only test.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "work-claim"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
        "quality_requirements": [],
        "budgets": {"tokens": 100, "attempts": 2, "elapsed_seconds": 600, "concurrency": 1},
        "dependencies": [], "checkpoints": ["schedule-stage"],
        "stop_conditions": [], "escalation_conditions": [],
    }


class SchedulePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        initialize_project(self.root, name="Scheduling")
        store = AutonomyStore(load_project_config(self.root).database_path(self.root))
        store.migrate()
        store.create_goal(goal_id="schedule-goal", title="Schedule", description="Schedule", acceptance=["Done."])
        self.contract = contract()
        self.digest = authority_envelope_sha256(self.contract)
        now = datetime.now(timezone.utc)
        store.define_goal_contract("schedule-goal", self.contract, actor_id="owner", at=now)
        store.record_transition_approval(
            goal_id="schedule-goal", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=now + timedelta(minutes=10), at=now,
        )
        store.activate_goal("schedule-goal", actor_id="owner", envelope_sha256=self.digest, at=now)
        store.create_work_unit(
            goal_id="schedule-goal", work_unit_id="schedule-unit", title="Resume", checkpoint_id="schedule-stage",
            scope={"paths": ["src"], "exclusions": []},
        )
        self.store = store

    def tearDown(self) -> None:
        self.directory.cleanup()

    def preview(self, **overrides):
        values = {
            "goal_id": "schedule-goal", "work_unit_id": "schedule-unit", "envelope_sha256": self.digest,
            "checkpoint_id": "schedule-stage", "cadence": "weekdays 09:00", "notification_intent": "on-failure",
            "performer_id": "scheduler", "repository": "tasktra", "revision": "abc123", "branch": "master",
            "workspace": "C:/workspace/tasktra", "lease_seconds": 300, "token_reservation": 25,
        }
        values.update(overrides)
        return preview_schedule(self.store, **values)

    def test_preview_is_deterministic_read_only_and_preserves_resume_context(self) -> None:
        before = snapshot(self.root)
        first, second = self.preview(), self.preview()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["mutation"], "none")
        self.assertTrue(first["local_work_can_continue"])
        self.assertFalse(first["provider_data_is_authority"])
        invocation = first["invocation"]
        self.assertEqual(invocation["goal_id"], "schedule-goal")
        self.assertEqual(invocation["work_unit_id"], "schedule-unit")
        self.assertEqual(invocation["envelope_sha256"], self.digest)
        self.assertEqual(invocation["checkpoint_id"], "schedule-stage")
        self.assertIn("budget_reference", invocation)
        self.assertEqual(invocation["lease"]["performer_id"], "scheduler")
        self.assertEqual(invocation["lease"]["repository"], "tasktra")
        self.assertEqual(invocation["lease"]["revision"], "abc123")
        self.assertEqual(invocation["lease"]["branch"], "master")
        self.assertEqual(invocation["lease"]["workspace"], "C:/workspace/tasktra")
        self.assertEqual(invocation["lease"]["requested_lease_seconds"], 300)
        self.assertEqual(invocation["lease"]["requested_token_reservation"], 25)
        self.assertIn("recover-expired-lease", invocation["recovery"]["action"])
        self.assertRegex(invocation["idempotency_key"], r"^schedule-[0-9a-f]{40}$")
        self.assertNotIn("TASKTRA_LEASE_TOKEN=", json.dumps(first))
        self.assertIn("cannot self-approve", first["prompt"])
        self.assertIn("Do not create", first["prompt"])

    def test_replay_is_rejected_after_the_exact_unit_is_claimed(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_transition_approval(goal_id="schedule-goal", work_unit_id="schedule-unit", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest, approver_id="human", performer_id="scheduler", valid_until=now + timedelta(minutes=10), at=now)
        invocation = self.preview()["invocation"]
        result = reserve_schedule_resume(self.store, invocation, lease_token="x" * 32)
        self.assertEqual(result["work_unit_id"], "schedule-unit")
        with self.assertRaisesRegex(SchedulerError, "already consumed"):
            reserve_schedule_resume(self.store, invocation, lease_token="x" * 32)

    def test_resume_atomically_recovers_expired_current_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_transition_approval(
            goal_id="schedule-goal", work_unit_id="schedule-unit", action="work-claim",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="human", performer_id="scheduler",
            valid_until=now + timedelta(minutes=10), at=now - timedelta(minutes=3),
        )
        expired = self.store.claim_next_work(
            goal_id="schedule-goal", performer_id="scheduler", envelope_sha256=self.digest,
            lease_seconds=1, token_reservation=25, repository="tasktra", revision="old",
            branch="master", workspace="C:/workspace/tasktra", lease_token="o" * 32,
            at=now - timedelta(minutes=2),
        )
        invocation = self.preview()["invocation"]
        result = reserve_schedule_resume(self.store, invocation, lease_token="n" * 32)
        self.assertEqual(result["recovered_attempts"], [expired["attempt_id"]])
        self.assertEqual(result["attempt_no"], 2)
        self.assertEqual(self.store.get_work_unit("schedule-unit")["current_attempt_id"], result["attempt_id"])

    def test_resume_recovers_expired_other_attempt_before_concurrency_check(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.create_work_unit(
            goal_id="schedule-goal", work_unit_id="a-expired-unit", title="Expired",
            checkpoint_id="schedule-stage", scope={"paths": ["src"], "exclusions": []},
        )
        for unit in ("a-expired-unit", "schedule-unit"):
            self.store.record_transition_approval(
                goal_id="schedule-goal", work_unit_id=unit, action="work-claim",
                effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
                approver_id="human", performer_id="scheduler",
                valid_until=now + timedelta(minutes=10), at=now - timedelta(minutes=3),
            )
        expired = self.store.claim_next_work(
            goal_id="schedule-goal", performer_id="scheduler", envelope_sha256=self.digest,
            lease_seconds=1, token_reservation=10, repository="tasktra", revision="old",
            branch="master", workspace="C:/workspace/tasktra", lease_token="o" * 32,
            at=now - timedelta(minutes=2),
        )
        self.assertEqual(expired["work_unit_id"], "a-expired-unit")
        result = reserve_schedule_resume(
            self.store, self.preview()["invocation"], lease_token="n" * 32,
        )
        self.assertEqual(result["recovered_attempts"], [expired["attempt_id"]])
        self.assertEqual(result["work_unit_id"], "schedule-unit")

    def test_cli_resume_binds_actor_token_and_rejects_replay(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_transition_approval(
            goal_id="schedule-goal", work_unit_id="schedule-unit", action="work-claim",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="human", performer_id="scheduler",
            valid_until=now + timedelta(minutes=10), at=now,
        )
        plan = self.root / "schedule-plan.json"
        plan.write_text(json.dumps(self.preview()), encoding="utf-8")
        environment = {"TASKTRA_TEST_LEASE": "z" * 32}
        with patch.dict(os.environ, environment, clear=False):
            code, mismatch = invoke(
                "schedule", "--root", str(self.root), "resume", str(plan),
                "--actor", "other", "--lease-token-env", "TASKTRA_TEST_LEASE",
            )
            self.assertEqual(code, 2)
            self.assertIn("actor", mismatch["error"])

            code, result = invoke(
                "schedule", "--root", str(self.root), "resume", str(plan),
                "--actor", "scheduler", "--lease-token-env", "TASKTRA_TEST_LEASE",
            )
            self.assertEqual((code, result["action"], result["mutation"]), (0, "schedule-resume", "local-ledger"))
            self.assertEqual(result["claim"]["work_unit_id"], "schedule-unit")
            self.assertNotIn(environment["TASKTRA_TEST_LEASE"], json.dumps(result))

            code, replay = invoke(
                "schedule", "--root", str(self.root), "resume", str(plan),
                "--actor", "scheduler", "--lease-token-env", "TASKTRA_TEST_LEASE",
            )
            self.assertEqual(code, 2)
            self.assertIn("already consumed", replay["error"])

    def test_unavailable_schedulers_remain_visible_and_manual_fallback_keeps_work_eligible(self) -> None:
        result = self.preview(requested_adapter="codex-scheduled-tasks")
        self.assertEqual(result["adapter"]["id"], "codex-scheduled-tasks")
        self.assertFalse(result["adapter"]["available"])
        self.assertTrue(result["local_work_can_continue"])
        self.assertEqual([item["id"] for item in result["available_adapters"]], [
            "codex-scheduled-tasks", "ci", "local-runner", "manual",
        ])
        self.assertFalse(any(item["can_create_schedule"] for item in result["available_adapters"]))

    def test_host_health_selects_codex_but_cannot_become_authority(self) -> None:
        health = {
            "kind": "tasktra.scheduler-health-report", "version": 1,
            "schedulers": [{
                "scheduler": "codex-scheduled-tasks", "state": "available", "summary": "Desktop enabled",
                "preferred_environment": "isolated-worktree",
            }],
        }
        result = self.preview(health=health)
        self.assertEqual(result["adapter"]["id"], "codex-scheduled-tasks")
        self.assertTrue(result["adapter"]["available"])
        self.assertFalse(result["adapter"]["can_create_schedule"])
        self.assertEqual(result["invocation"]["authority"], "persisted-ledger-only")
        self.assertFalse(result["adapter"]["provider_data_is_authority"])

    def test_mismatched_existing_authority_or_work_is_rejected(self) -> None:
        with self.assertRaisesRegex(SchedulerError, "exact current authority"):
            self.preview(envelope_sha256="0" * 64)
        with self.assertRaisesRegex(SchedulerError, "checkpoint"):
            self.preview(checkpoint_id="other-stage")
        with self.assertRaisesRegex(SchedulerError, "does not belong"):
            self.preview(work_unit_id="missing")

    def test_malformed_or_duplicate_health_fails_closed(self) -> None:
        with self.assertRaisesRegex(SchedulerError, "duplicate"):
            adapters_from_health({"kind": "tasktra.scheduler-health-report", "version": 1, "schedulers": [
                {"scheduler": "ci", "state": "available", "summary": "a", "preferred_environment": "isolated-runner"},
                {"scheduler": "ci", "state": "available", "summary": "b", "preferred_environment": "isolated-runner"},
            ]})
        with self.assertRaisesRegex(SchedulerError, "Invalid"):
            adapters_from_health({"kind": "wrong", "version": 1, "schedulers": []})

    def test_cli_exposes_only_preview_and_never_creates_a_schedule(self) -> None:
        health = self.root / "health.json"
        health.write_text(json.dumps({"kind": "tasktra.scheduler-health-report", "version": 1, "schedulers": []}), encoding="utf-8")
        before = snapshot(self.root)
        code, result = invoke(
            "schedule", "--root", str(self.root), "preview", "--goal-id", "schedule-goal",
            "--work-unit-id", "schedule-unit", "--envelope-sha256", self.digest,
            "--checkpoint", "schedule-stage", "--scheduler-health", str(health),
            "--performer-id", "scheduler", "--repository", "tasktra", "--revision", "abc123",
            "--branch", "master", "--workspace", "C:/workspace/tasktra",
            "--lease-seconds", "300", "--token-reservation", "25",
        )
        self.assertEqual((code, result["action"], result["mutation"]), (0, "schedule-preview", "none"))
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(("schedule", "create"))


if __name__ == "__main__":
    unittest.main()
