"""CLI wiring for the read-only work-unit inspection projection."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.cli import build_parser, main
from tests import test_work_plan_cli as plans


def invoke(*arguments: str) -> tuple[int, dict[str, object], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    text = stdout.getvalue() or stderr.getvalue()
    return code, json.loads(text), text


class WorkInspectionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "operator root"
        self.root.mkdir()
        self.store = plans.make_project(self.root)
        manifest = plans.example_plan()
        preview = self.store.preview_work_plan(manifest)
        self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.command = ("work", "--root", str(self.root), "inspect", "goal-1", "service")

    def test_parser_accepts_only_the_new_bounded_inspection_arguments(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args((*self.command, "--limit", "50", "--before-sequence", "12", "--before-attempt-no", "3"))
        self.assertEqual(
            (parsed.work_command, parsed.goal_id, parsed.work_unit_id, parsed.limit,
             parsed.before_sequence, parsed.before_attempt_no),
            ("inspect", "goal-1", "service", 50, 12, 3),
        )
        with self.assertRaises(SystemExit):
            parser.parse_args((*self.command, "--unknown"))

    def test_wrapper_passes_one_resolved_root_and_emits_the_closed_action_envelope(self) -> None:
        inspection = {
            "kind": "tasktra.work-unit-inspection",
            "version": 1,
            "goal_id": "goal-1",
            "work_unit_id": "service",
            "read_only": True,
        }
        with patch("tasktra.work_inspection.inspect_work_unit", return_value=inspection) as inspect:
            code, result, text = invoke(
                *self.command,
                "--limit", "7",
                "--before-sequence", "21",
                "--before-attempt-no", "8",
            )

        self.assertEqual(code, 0, result)
        self.assertEqual(result, {"ok": True, "action": "work-inspect", "inspection": inspection})
        self.assertEqual(text, json.dumps(result, indent=2, sort_keys=True) + "\n")
        positional, keyword = inspect.call_args
        self.assertEqual(len(positional), 1)
        self.assertEqual(positional[0].path, self.store.path)
        self.assertEqual(
            keyword,
            {
                "project_root": self.root.resolve(),
                "goal_id": "goal-1",
                "work_unit_id": "service",
                "limit": 7,
                "before_sequence": 21,
                "before_attempt_no": 8,
            },
        )

    def test_existing_readiness_command_keeps_its_public_envelope(self) -> None:
        code, result, _ = invoke("work", "--root", str(self.root), "readiness", "goal-1", "--limit", "1")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["action"], "work-readiness")
        self.assertIn("readiness", result)
        self.assertNotIn("inspection", result)


if __name__ == "__main__":
    unittest.main()
