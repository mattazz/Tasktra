import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tasktra.host import CodexEventStream, CodexHostAdapter, HostError, host_environment


def event(value):
    return (json.dumps(value) + "\n").encode("utf-8")


class HostStreamTests(unittest.TestCase):
    def test_cli_disables_custom_project_instruction_fallback_names(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            feed = kwargs["on_stdout_chunk"]
            feed(event({"type": "thread.started", "thread_id": "thread-one"}))
            feed(event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                "status": "completed", "summary": "Done.", "findings": [], "changed_paths": [],
            })}}))
            feed(event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
            return SimpleNamespace(returncode=0, timed_out=False, output_limited=False, input_uncertain=False, dispatched=True)

        host = CodexHostAdapter(runner_factory=lambda **kwargs: SimpleNamespace(run=run))
        with TemporaryDirectory() as temporary, patch("tasktra.host.shutil.which", return_value="codex"):
            result = host.run(prompt="Inspect the artifact.", workspace=Path(temporary), model=None, effort=None,
                              sandbox="read-only", timeout_seconds=5, on_started=lambda _: None, on_tick=lambda: None)
        self.assertEqual(result.thread_id, "thread-one")
        configurations = [calls[0][index + 1] for index, argument in enumerate(calls[0][:-1]) if argument == "-c"]
        self.assertIn("project_doc_fallback_filenames=[]", configurations)

    def test_host_environment_drops_credentials_leases_and_arbitrary_capabilities(self):
        supplied = {
            "PATH": "runtime-bin", "HOME": "saved-login-home", "SystemRoot": "platform-root",
            "GH_TOKEN": "github-secret", "AWS_SECRET": "aws-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-access-secret", "OPENAI_API_KEY": "api-secret",
            "TASKTRA_LEASE": "lease-capability", "TASKTRA_LEASE_TOKEN": "lease-secret",
            "CUSTOM_CAPABILITY_HANDLE": "opaque-capability", "HTTPS_PROXY": "credentialed-proxy",
            "PYTHONPATH": "injected-import-directory",
        }
        with patch("tasktra.host.os.environ", supplied):
            actual = host_environment()
        self.assertEqual(actual, {name: supplied[name] for name in ("PATH", "HOME", "SystemRoot")})
        self.assertEqual(supplied["TASKTRA_LEASE_TOKEN"], "lease-secret")

    def test_platform_environment_allowlist_is_case_insensitive(self):
        supplied = {"pAtH": "runtime-bin", "systemRoot": "platform-root", "home": "saved-login-home", "gh_token": "secret"}
        with patch("tasktra.host.os.environ", supplied):
            actual = host_environment()
        self.assertEqual(actual, {"pAtH": "runtime-bin", "systemRoot": "platform-root", "home": "saved-login-home"})

    def test_complete_stream_binds_one_thread_result_and_usage(self):
        started = []
        stream = CodexEventStream(started.append)
        stream.feed(event({"type": "thread.started", "thread_id": "thread-one"}))
        stream.feed(event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
            "status": "completed", "summary": "Done.", "findings": [], "changed_paths": []
        })}}))
        stream.feed(event({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 5}}))
        result = stream.finish()
        self.assertEqual(started, ["thread-one"])
        self.assertEqual(result.usage["total_tokens"], 8)

    def test_malformed_or_out_of_order_stream_is_rejected(self):
        with self.assertRaisesRegex(HostError, "JSONL"):
            CodexEventStream(lambda _: None).feed(b"not-json\n")
        stream = CodexEventStream(lambda _: None)
        with self.assertRaisesRegex(HostError, "cannot be accepted"):
            stream.feed(event({"type": "turn.completed"}))

    def test_no_agent_result_and_missing_usage_are_distinguished(self):
        stream = CodexEventStream(lambda _: None)
        stream.feed(event({"type": "thread.started", "thread_id": "thread-one"}))
        stream.feed(event({"type": "turn.completed"}))
        with self.assertRaisesRegex(HostError, "attributable completed result"):
            stream.finish()

        stream = CodexEventStream(lambda _: None)
        stream.feed(event({"type": "thread.started", "thread_id": "thread-two"}))
        stream.feed(event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
            "status": "completed", "summary": "Done.", "findings": [], "changed_paths": []
        })}}))
        stream.feed(event({"type": "turn.completed"}))
        self.assertIsNone(stream.finish().usage)
