"""Focused black-box acceptance coverage for Stage 6 CLI workflows."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.config import initialize_project, load_project_config
from tasktra.state import StateStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = REPOSITORY_ROOT / "catalog"


def invoke(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


def payload(*arguments: str) -> tuple[int, dict]:
    code, stdout, stderr = invoke(*arguments)
    return code, json.loads(stdout or stderr)


def file_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def telemetry_record() -> dict:
    return {
        "schema_version": 1,
        "event_id": "event-006",
        "recorded_at": "2026-09-20T12:00:00Z",
        "role": "tester",
        "model_tier": "balanced",
        "tools": ["unittest"],
        "elapsed_ms": 125,
        "retries": 0,
        "evidence_reused": True,
        "validation_outcome": "passed",
        "human_interventions": 0,
        "final_outcome": "succeeded",
        "input_tokens": 42,
        "output_tokens": 17,
    }


def lesson_proposal() -> dict:
    return {
        "proposal_id": "cli-lesson",
        "author": "lesson-author",
        "problem": "Ambiguous remote writes can be retried before reconciliation.",
        "general_principle": "Require durable reconciliation evidence before retrying consequential effects.",
        "evidence": [{
            "kind": "validation", "ref": "evidence/retry.json", "sha256": "a" * 64,
        }],
        "affected_contracts": [{
            "contract_id": "provider.retry-policy",
            "canonical_source": "catalog/policies/provider-retry.toml",
            "change": "Require provider-owned absence evidence before retry.",
        }],
        "applicability": {
            "contexts": ["Consequential remote writes"],
            "constraints": ["Local work remains available"],
        },
        "risks": ["A broad absence claim could duplicate a write."],
        "regression_checks": [{
            "id": "provider-retry-regression",
            "description": "Ambiguous effects remain locked against retry.",
            "argv": ["python", "-m", "unittest", "tests.test_stage4_provider_execution"],
        }],
    }


class Stage6CliTests(unittest.TestCase):
    def test_adopt_preview_is_read_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("project-owned instructions\n", encoding="utf-8")
            before = file_snapshot(root)

            code, result = payload(
                "adopt", "--root", str(root), "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )

            self.assertEqual(code, 0)
            self.assertEqual((result["action"], result["mutation"]), ("adoption-preview", "none"))
            self.assertEqual(file_snapshot(root), before)
            self.assertIn("AGENTS.md", result["preserved_paths"])

    def test_upgrade_preview_has_stable_digest_and_is_read_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_project(root, name="CLI upgrade preview")
            compiled_code, compiled = payload(
                "compile", "--root", str(root), "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )
            self.assertEqual((compiled_code, compiled["ok"]), (0, True))
            before = file_snapshot(root)
            arguments = (
                "upgrade", "--root", str(root), "preview", "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )

            first_code, first = payload(*arguments)
            second_code, second = payload(*arguments)

            self.assertEqual((first_code, second_code), (0, 0))
            self.assertEqual((first["action"], first["mutation"]), ("upgrade-preview", "none"))
            self.assertRegex(first["plan_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(second["plan_sha256"], first["plan_sha256"])
            self.assertEqual(file_snapshot(root), before)

    def test_blocked_upgrade_preview_is_still_structured_and_read_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_project(root, name="CLI blocked preview")
            before = file_snapshot(root)

            code, result = payload(
                "upgrade", "--root", str(root), "preview",
                "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )

            self.assertEqual(code, 0)
            self.assertFalse(result["ok"])
            self.assertRegex(result["plan_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(any(item["component"] == "lockfile" for item in result["conflicts"]))
            self.assertEqual(file_snapshot(root), before)

    def test_upgrade_apply_requires_authority_and_records_recoverable_effect_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_project(root, name="CLI authorized upgrade")
            compiled_code, _ = payload(
                "compile", "--root", str(root), "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )
            self.assertEqual(compiled_code, 0)
            database = load_project_config(root).database_path(root)
            StateStore(database).migrate()
            store = AutonomyStore(database)
            contract = {
                "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "upgrade-goal",
                "outcome": "Apply one reviewed Tasktra upgrade.", "motivation": "CLI authority coverage.",
                "author_id": "owner", "acceptance_criteria": [{"id": "done", "statement": "Upgrade verified."}],
                "scope": {"paths": ["."], "exclusions": []},
                "allowed_actions": ["goal-activate", "local-effect"],
                "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
                "quality_requirements": [],
                "budgets": {"tokens": 1000, "attempts": 3, "elapsed_seconds": 300, "concurrency": 1},
                "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
            }
            digest = authority_envelope_sha256(contract)
            store.create_goal(goal_id="upgrade-goal", title="Upgrade", description="Upgrade", acceptance=["Upgrade verified."])
            store.define_goal_contract("upgrade-goal", contract, actor_id="owner")
            expiry = datetime.now(timezone.utc) + timedelta(days=1)
            store.record_transition_approval(
                goal_id="upgrade-goal", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry,
            )
            store.activate_goal("upgrade-goal", actor_id="owner", envelope_sha256=digest)
            store.create_work_unit(
                goal_id="upgrade-goal", work_unit_id="upgrade-work", title="Upgrade",
                scope={"paths": ["."], "exclusions": []},
            )
            store.record_transition_approval(
                goal_id="upgrade-goal", work_unit_id="upgrade-work", action="local-effect",
                effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest,
                approver_id="human", performer_id="worker", valid_until=expiry,
            )
            _, preview = payload(
                "upgrade", "--root", str(root), "preview", "--catalog", str(CATALOG_ROOT), "--trust-catalog",
            )

            denied_code, denied = payload(
                "upgrade", "--root", str(root), "apply", "--catalog", str(CATALOG_ROOT), "--trust-catalog",
                "--plan-sha256", preview["plan_sha256"], "--goal-id", "upgrade-goal",
                "--work-unit-id", "upgrade-work", "--envelope-sha256", digest,
                "--actor", "intruder", "--idempotency-key", "upgrade-denied", "--confirm",
            )
            self.assertEqual(denied_code, 2)
            self.assertIn("no current approval", denied["error"])

            code, result = payload(
                "upgrade", "--root", str(root), "apply", "--catalog", str(CATALOG_ROOT), "--trust-catalog",
                "--plan-sha256", preview["plan_sha256"], "--goal-id", "upgrade-goal",
                "--work-unit-id", "upgrade-work", "--envelope-sha256", digest,
                "--actor", "worker", "--idempotency-key", "upgrade-authorized", "--confirm",
            )
            self.assertEqual((code, result["ok"], result["effect_receipt"]["outcome"]), (0, True, "success"))
            self.assertTrue(result["migration"]["rollback_available"])
            self.assertEqual(store.inspect_effect("upgrade-authorized")["status"], "received")
            rollback_code, rollback = payload(
                "upgrade", "--root", str(root), "rollback",
                "--snapshot-plan-sha256", result["migration"]["plan_sha256"],
                "--before-sha256", result["migration"]["before_sha256"],
                "--goal-id", "upgrade-goal", "--work-unit-id", "upgrade-work",
                "--envelope-sha256", digest, "--actor", "worker",
                "--idempotency-key", "rollback-authorized", "--confirm",
            )
            self.assertEqual((rollback_code, rollback["ok"], rollback["effect_receipt"]["outcome"]), (0, True, "success"))
            self.assertEqual(store.inspect_effect("upgrade-authorized")["receipt"]["outcome"], "success")
            self.assertEqual(store.inspect_effect("rollback-authorized")["receipt"]["outcome"], "success")
            self.assertEqual(store.verify_audit()["issues"], [])

    def test_telemetry_requires_opt_in_and_only_exports_locally(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_path = root / "record.json"
            record_path.write_text(json.dumps(telemetry_record()), encoding="utf-8")

            disabled_code, disabled = payload("telemetry", "--root", str(root), "record", str(record_path))
            self.assertEqual(disabled_code, 2)
            self.assertIn("disabled by default", disabled["error"])
            self.assertFalse((root / ".tasktra" / "telemetry").exists())

            recorded_code, recorded = payload(
                "telemetry", "--root", str(root), "record", str(record_path), "--enable",
            )
            self.assertEqual((recorded_code, recorded["action"], recorded["appended"]), (0, "telemetry-record", True))
            exported_code, exported = payload(
                "telemetry", "--root", str(root), "export", ".tasktra/exports/telemetry.json",
            )
            destination = root / ".tasktra" / "exports" / "telemetry.json"
            self.assertEqual((exported_code, exported["action"], exported["sanitized"]), (0, "telemetry-export", True))
            self.assertEqual(Path(exported["path"]), destination.resolve())
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["records"][0]["event_id"], "event-006")

    def test_benchmark_regressions_are_nonzero_and_do_not_invent_savings(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.json"
            candidate = root / "candidate.json"
            baseline.write_text(json.dumps([{
                "scenario": "retrieval", "retrievals": ["doc-a", "doc-b"],
                "context_tokens": 100, "escalation_count": 0, "retry_count": 0,
            }]), encoding="utf-8")
            candidate.write_text(json.dumps([{
                "scenario": "retrieval", "retrievals": ["doc-a", "doc-a", "doc-b"],
                "context_tokens": 140, "escalation_count": 1, "retry_count": 2,
            }]), encoding="utf-8")

            code, result = payload("benchmark", str(baseline), str(candidate))

            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual([item["kind"] for item in result["findings"]], [
                "duplicate-retrieval", "avoidable-context-growth", "unnecessary-escalation", "retry-regression",
            ])
            self.assertIsNone(result["estimated_token_savings"])

    def test_lesson_cli_requires_independent_review_and_only_previews_promotion(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "lesson.json"
            source.write_text(json.dumps(lesson_proposal()), encoding="utf-8")

            created_code, created = payload("lesson", "--root", str(root), "create", str(source))
            self.assertEqual((created_code, created["proposal"]["status"], created["proposal"]["version"]), (0, "draft", 1))
            self_review_code, self_review = payload(
                "lesson", "--root", str(root), "review", "cli-lesson", "--expected-version", "1", "--actor", "lesson-author",
            )
            self.assertEqual(self_review_code, 2)
            self.assertIn("author cannot review", self_review["error"])

            reviewed_code, reviewed = payload(
                "lesson", "--root", str(root), "review", "cli-lesson", "--expected-version", "1", "--actor", "independent-reviewer",
            )
            self.assertEqual((reviewed_code, reviewed["proposal"]["status"], reviewed["proposal"]["version"]), (0, "reviewed", 2))
            approved_code, approved = payload(
                "lesson", "--root", str(root), "approve", "cli-lesson", "--expected-version", "2",
                "--actor", "independent-reviewer", "--reason", "Evidence and regression checks are sufficient.",
            )
            self.assertEqual((approved_code, approved["proposal"]["status"], approved["proposal"]["version"]), (0, "approved", 3))

            lesson_path = root / ".tasktra" / "lessons" / "cli-lesson.json"
            before = lesson_path.read_bytes()
            preview_code, preview = payload("lesson", "--root", str(root), "promotion-preview", "cli-lesson")
            self.assertEqual((preview_code, preview["action"], preview["plan"]["applies_no_changes"]), (0, "lesson-promotion-preview", True))
            self.assertEqual(lesson_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
