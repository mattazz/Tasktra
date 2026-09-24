"""Public CLI contract for opt-in agent execution records."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.config import initialize_project


def invoke(*arguments: str) -> tuple[int, dict]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class ExecutionCliTests(unittest.TestCase):
    def test_enabled_role_records_host_receipt_and_unknown_usage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root, name="Execution fixture")
            prefix = ("execution", "--root", str(root))
            code, planned = invoke(*prefix, "plan", "work-1", "--role", "scout")
            self.assertEqual(code, 0)
            self.assertEqual(planned["record"]["role"], "scout")
            self.assertIsNone(planned["record"]["observed_model"])
            code, started = invoke(
                *prefix, "start", "work-1", "--host", "local", "--thread-id", "child-thread",
                "--agent-id", "/root/scout",
            )
            self.assertEqual(code, 0)
            self.assertEqual(started["record"]["thread_id"], "child-thread")
            self.assertEqual(started["record"]["start_provenance"], "manual-assertion")
            code, finished = invoke(
                *prefix, "finish", "work-1", "--outcome", "succeeded",
                "--unknown-reason", "host-no-usage",
            )
            self.assertEqual(code, 0)
            self.assertEqual(finished["record"]["outcome"], "succeeded")
            self.assertEqual(finished["record"]["finish_provenance"], "manual-assertion")
            code, report = invoke(*prefix, "report")
            self.assertEqual(code, 0)
            self.assertEqual(report["report"]["work_count"], 1)

    def test_project_agent_file_opts_in_without_a_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root, name="Execution fixture")
            agent = root / ".codex" / "agents" / "graphic-designer.toml"
            agent.parent.mkdir(parents=True)
            agent.write_text(
                'name = "graphic-designer"\n'
                'description = "Create artwork for approved product screens."\n'
                'model = "gpt-6-sol"\n'
                'model_reasoning_effort = "high"\n',
                encoding="utf-8",
            )
            code, planned = invoke(
                "execution", "--root", str(root), "plan", "work-art", "--role", "graphic-designer"
            )
            self.assertEqual(code, 0)
            self.assertEqual(planned["record"]["configured_model"], "gpt-6-sol")
            self.assertEqual(planned["record"]["configured_effort"], "high")
            agent.write_text(
                'name = "graphic-designer"\n'
                'description = "Create artwork for approved product screens."\n',
                encoding="utf-8",
            )
            code, inherited = invoke(
                "execution", "--root", str(root), "plan", "work-inherit", "--role", "graphic-designer"
            )
            self.assertEqual(code, 0)
            self.assertIsNone(inherited["record"]["configured_model"])
            self.assertIsNone(inherited["record"]["configured_effort"])
            code, error = invoke(
                "execution", "--root", str(root), "plan", "work-disabled", "--role", "frontend-specialist"
            )
            self.assertEqual(code, 2)
            self.assertIn("not opted in", error["error"])

    def test_finish_imports_child_rollout_usage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_project(root, name="Execution fixture")
            prefix = ("execution", "--root", str(root))
            self.assertEqual(invoke(*prefix, "plan", "work-usage", "--role", "scout")[0], 0)
            self.assertEqual(invoke(
                *prefix, "start", "work-usage", "--host", "local",
                "--thread-id", "child-thread", "--turn-id", "turn-a", "--agent-id", "/root/scout"
            )[0], 0)
            rollout = root / "rollout.jsonl"
            events = [
                {"type": "session_meta", "payload": {
                    "id": "child-thread", "session_id": "parent-host-session",
                    "source": {"subagent": {"thread_spawn": {"agent_path": "/root/scout"}}},
                }},
                {"type": "turn_context", "payload": {
                    "turn_id": "turn-a", "model": "gpt-5.6-luna", "effort": "medium",
                }},
                {"type": "token_usage_record", "payload": {
                    "thread_id": "child-thread", "session_id": "parent-host-session",
                    "turn_id": "turn-a", "response_id": "response-a", "usage": {
                        "input_tokens": 15, "cached_input_tokens": 5, "cache_write_input_tokens": 0,
                        "output_tokens": 4, "reasoning_output_tokens": 2, "total_tokens": 19,
                    },
                }},
            ]
            rollout.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            code, finished = invoke(
                *prefix, "finish", "work-usage", "--outcome", "succeeded", "--rollout", str(rollout)
            )
            self.assertEqual(code, 0)
            self.assertEqual(finished["record"]["usage"]["total_tokens"], 19)
            self.assertEqual(finished["record"]["observed_model"], "gpt-5.6-luna")
            self.assertEqual(finished["record"]["agent_id"], "/root/scout")
            self.assertEqual(finished["record"]["usage_provenance"], "rollout-verified")


if __name__ == "__main__":
    unittest.main()
