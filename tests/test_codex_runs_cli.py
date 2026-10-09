"""CLI coverage for privacy-preserving Codex host-run receipts."""

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
from tasktra.cli import main
from tasktra.config import initialize_project, load_project_config


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class CodexRunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        initialize_project(self.root, name="Codex receipt CLI")
        self.store = AutonomyStore(load_project_config(self.root).database_path(self.root))
        self.token = "lease-token-for-cli-test-0123456789"
        contract = {
            "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
            "outcome": "Exercise bounded local controls.", "motivation": "CLI receipt test.", "author_id": "owner",
            "acceptance_criteria": [{"id": "done", "statement": "Done."}],
            "scope": {"paths": ["src"], "exclusions": []},
            "allowed_actions": ["goal-activate", "work-claim"],
            "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [],
            "quality_requirements": [],
            "budgets": {"tokens": 100, "attempts": 3, "elapsed_seconds": 300, "concurrency": 1},
            "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
        }
        digest = authority_envelope_sha256(contract)
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
        self.store.define_goal_contract("goal-one", contract, actor_id="owner")
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        self.store.record_transition_approval(
            goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=expiry,
        )
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=digest)
        self.store.create_work_unit(
            goal_id="goal-one", work_unit_id="unit-one", title="Unit",
            scope={"paths": ["src"], "exclusions": []},
        )
        self.store.record_transition_approval(
            goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="worker", valid_until=expiry,
        )
        self.claim = self.store.claim_next_work(
            goal_id="goal-one", performer_id="worker", envelope_sha256=digest, lease_token=self.token,
            repository="repo", revision="revision", branch="main", workspace="workspace",
        )
        self.request = self.root / "routing-request.json"
        self.request.write_text(json.dumps({
            "kind": "tasktra.routing-request", "version": 1, "task_id": "codex-run-cli",
            "source": {"goal_id": "goal-one", "work_unit_id": "unit-one"}, "objective": "Inspect bounded state.",
            "primary_signal": "inspect", "signals": ["inspect"], "constraints": [],
            "verified_facts": [], "evidence_refs": [],
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_prepare_start_finish_and_show_digest_without_raw_result(self) -> None:
        code, planned = invoke(
            "delegation", "--root", str(self.root), "plan", str(self.request),
        )
        self.assertEqual((code, planned["action"]), (0, "delegation-plan"))
        with patch.dict(os.environ, {"TASKTRA_TEST_LEASE": self.token}, clear=False):
            code, prepared = invoke(
                "delegation", "--root", str(self.root), "prepare", self.claim["attempt_id"],
                "--actor", "worker", "--request", str(self.request), "--idempotency-key", "receipt-key",
                "--lease-token-env", "TASKTRA_TEST_LEASE",
            )
        self.assertEqual((code, prepared["action"]), (0, "delegation-prepare"))
        run = prepared["run"]
        self.assertEqual(set(run), {
            "run_id", "attempt_id", "requested_task_name", "launch_directive", "idempotent", "agent",
            "plan_sha256", "brief_sha256",
        })
        self.assertEqual(run["launch_directive"], "invoke-once-now")
        self.assertIsInstance(run["requested_task_name"], str)
        self.assertTrue(run["requested_task_name"])
        self.assertEqual(run["plan_sha256"], planned["plan_sha256"])
        self.assertEqual(run["brief_sha256"], planned["brief_sha256"])
        self.assertNotIn(self.token, json.dumps(prepared))

        with patch("tasktra.cli._catalog_root", side_effect=FileNotFoundError("catalog unavailable")):
            code, started = invoke(
                "delegation", "--root", str(self.root), "start", run["run_id"], "--actor", "worker",
                "--host-canonical-name", run["requested_task_name"],
            )
        self.assertEqual(code, 0, started)
        self.assertEqual(started["action"], "delegation-start")
        result = b"child completed with bounded evidence"
        stdin = io.TextIOWrapper(io.BytesIO(result), encoding="utf-8")
        with patch("tasktra.cli._catalog_root", side_effect=FileNotFoundError("catalog unavailable")), patch("sys.stdin", stdin):
            code, finished = invoke(
                "delegation", "--root", str(self.root), "finish", run["run_id"], "--actor", "worker",
                "--outcome", "completed", "--usage-status", "unavailable", "--result-stdin",
            )
        self.assertEqual((code, finished["action"]), (0, "delegation-finish"))
        self.assertNotIn(result.decode(), json.dumps(finished))
        self.assertEqual(finished["run"]["result"]["sha256"], sha256(result).hexdigest())

        with patch("tasktra.cli._catalog_root", side_effect=FileNotFoundError("catalog unavailable")):
            code, shown = invoke("delegation", "--root", str(self.root), "show", run["run_id"])
        self.assertEqual((code, shown["action"]), (0, "delegation-show"))
        self.assertEqual(shown["run"]["actual"]["canonical_name"], run["requested_task_name"])
        self.assertNotIn(result.decode(), json.dumps(shown))
        with patch("tasktra.cli._catalog_root", side_effect=FileNotFoundError("catalog unavailable")):
            code, listed = invoke(
                "delegation", "--root", str(self.root), "list", "--attempt-id", self.claim["attempt_id"],
            )
        self.assertEqual((code, listed["action"]), (0, "delegation-list"))
        self.assertEqual([item["run_id"] for item in listed["runs"]["items"]], [run["run_id"]])


if __name__ == "__main__":
    unittest.main()
