"""CLI coverage for closed, observational Codex execution recovery."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.cli import main
from tasktra.config import initialize_project
from tests.test_execution_recovery_runtime import fixture, observation, prepare


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def write_observation(case, run: dict[str, object], kind: str = "running") -> Path:
    path = case.root / "host-observation.json"
    path.write_text(json.dumps(observation(run, kind)), encoding="utf-8")
    return path


class ExecutionRecoveryCliTests(unittest.TestCase):
    def test_unresolved_and_running_then_completed_use_closed_transport(self):
        with fixture() as case:
            run = prepare(case, "recovery-cli")
            path = write_observation(case, run)
            code, unresolved = invoke(
                "delegation", "--root", str(case.root), "unresolved", "--goal-id", "goal-one", "--limit", "1",
            )
            self.assertEqual((code, unresolved["action"], unresolved["runs"]["items"][0]["reason"]),
                             (0, "delegation-unresolved", "prepared-unobserved"))
            guidance = unresolved["runs"]["items"][0]["next_action"]
            self.assertEqual(guidance["show"]["argv"], [
                "tasktra", "delegation", "--root", str(case.root.resolve()), "show", run["run_id"],
            ])
            self.assertNotIn("argv", guidance["reconcile"])
            self.assertEqual(guidance["reconcile"]["argv_template"], [
                "tasktra", "delegation", "--root", str(case.root.resolve()), "reconcile", run["run_id"],
                "--actor", "<actor>", "--observation", "<observation.json>",
            ])

            code, running = invoke(
                "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "observer",
                "--observation", str(path),
            )
            self.assertEqual((code, running["observation_kind"], running["mutation"], running["run"]["state"]),
                             (0, "running", "applied", "started"))
            code, retry = invoke(
                "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "retry-observer",
                "--observation", str(path),
            )
            self.assertEqual((code, retry["mutation"]), (0, "none"))
            self.assertEqual(retry["run"]["reconciliation_observer"], "retry-observer")
            self.assertEqual(retry["run"]["recorded_attribution"]["start"]["observed_by"], "observer")

            completed_path = write_observation(case, run, "completed")
            result = b"\r\nexact completed bytes: \xe2\x98\x83 \n"
            stdin = io.TextIOWrapper(io.BytesIO(result), encoding="utf-8")
            with patch("sys.stdin", stdin):
                code, completed = invoke(
                    "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "finisher",
                    "--observation", str(completed_path), "--result-stdin",
                )
            self.assertEqual((code, completed["observation_kind"], completed["mutation"]),
                             (0, "completed", "applied"))
            self.assertEqual(completed["run"]["result"]["sha256"], sha256(result).hexdigest())
            self.assertNotIn(result.decode("utf-8"), json.dumps(completed))

    def test_observation_and_stdin_bounds_fail_before_a_write(self):
        with fixture() as case:
            run = prepare(case, "recovery-cli-bounds")
            running_path = write_observation(case, run)
            before = case.store.get_codex_run(run["run_id"])
            oversized = case.root / "oversized-observation.json"
            oversized.write_bytes(b"{" + b" " * (32 * 1024) + b"}")
            code, failure = invoke(
                "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "observer",
                "--observation", str(oversized),
            )
            self.assertEqual(code, 2)
            self.assertIn("32KiB", failure["error"])

            stdin = io.TextIOWrapper(io.BytesIO(b"not accepted for running"), encoding="utf-8")
            with patch("sys.stdin", stdin):
                code, failure = invoke(
                    "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "observer",
                    "--observation", str(running_path), "--result-stdin",
                )
            self.assertEqual(code, 2)
            self.assertNotIn("not accepted for running", json.dumps(failure))
            self.assertEqual(case.store.get_codex_run(run["run_id"]), before)

    def test_duplicate_observation_keys_and_completed_without_stdin_are_safe_errors(self):
        with fixture() as case:
            run = prepare(case, "recovery-cli-strict")
            duplicate = case.root / "duplicate-observation.json"
            duplicate.write_text('{"source":"first","source":"collaboration.list_agents"}', encoding="utf-8")
            code, failure = invoke(
                "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "observer",
                "--observation", str(duplicate),
            )
            self.assertEqual(code, 2)
            self.assertIn("duplicate JSON key", failure["error"])

            code, failure = invoke(
                "delegation", "--root", str(case.root), "reconcile", run["run_id"], "--actor", "observer",
                "--observation", str(write_observation(case, run, "completed")),
            )
            self.assertEqual(code, 2)
            self.assertIn("requires --result-stdin", failure["error"])
            self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], "prepared")

    def test_overview_preserves_closed_delegation_and_intervention_recommendations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root, name="Overview recommendation CLI")
            report = {
                "goal": None,
                "goals": [{
                    "id": "goal-one",
                    "recommendations": [
                        {"kind": "read-only-command", "argv": ["tasktra", "delegation", "unresolved", "--goal-id", "goal-one"],
                         "detail": "Inspect unresolved Codex workers."},
                        {"kind": "read-only-command", "command": "tasktra intervention list --goal-id goal-one"},
                    ],
                }],
            }
            with patch("tasktra.cli.orchestration_overview", return_value=report):
                code, output = invoke("overview", "--root", str(root), "--json")
            recommendations = output["goals"][0]["recommendations"]
            self.assertEqual(code, 0)
            self.assertEqual(recommendations[0]["argv"], [
                "tasktra", "delegation", "--root", str(root.resolve()), "unresolved", "--goal-id", "goal-one",
            ])
            self.assertEqual(recommendations[1]["argv"], [
                "tasktra", "intervention", "--root", str(root.resolve()), "list", "--goal-id", "goal-one",
            ])

    def test_deep_json_and_non_utf8_are_bounded_errors_without_writes(self):
        with fixture() as case:
            run = prepare(case, "closed-json-transport")
            before = case.store.path.read_bytes()
            inputs = {
                "nested": b"[" * 1500 + b'"private-deep-input"' + b"]" * 1500,
                "utf16": json.dumps(observation(run)).encode("utf-16"),
                "utf32": json.dumps(observation(run)).encode("utf-32"),
            }
            for name, payload in inputs.items():
                with self.subTest(name=name):
                    path = case.root / (name + ".json")
                    path.write_bytes(payload)
                    code, failure = invoke(
                        "delegation", "--root", str(case.root), "reconcile", run["run_id"],
                        "--actor", "observer", "--observation", str(path),
                    )
                    self.assertEqual(code, 2)
                    self.assertNotIn("private-deep-input", json.dumps(failure))
                    self.assertNotIn("Traceback", json.dumps(failure))
                    self.assertEqual(case.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
