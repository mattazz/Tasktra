import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tasktra.host import CodexEventStream, CodexHostAdapter, HostError, host_environment
from tasktra.worker_profiles import CliCapabilities, WorkerContext


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

    def test_explicit_focused_context_adds_only_bounded_server_overrides_and_receipt(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            feed = kwargs["on_stdout_chunk"]
            feed(event({"type": "thread.started", "thread_id": "thread-profile"}))
            feed(event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                "status": "completed", "summary": "Done.", "findings": [], "changed_paths": [],
            })}}))
            feed(event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
            return SimpleNamespace(returncode=0, timed_out=False, output_limited=False, input_uncertain=False, dispatched=True)

        context = WorkerContext(focused=True, observed_mcp_servers=("unused",), disable_mcp_servers=("unused",))
        host = CodexHostAdapter(
            runner_factory=lambda **kwargs: SimpleNamespace(run=run), worker_context=context,
            cli_capabilities=CliCapabilities(checked=True, supports_config_overrides=True),
        )
        with TemporaryDirectory() as temporary, patch("tasktra.host.shutil.which", return_value="codex"):
            result = host.run(prompt="Inspect the artifact.", workspace=Path(temporary), model=None, effort=None,
                              sandbox="read-only", timeout_seconds=5, on_started=lambda _: None, on_tick=lambda: None)
        self.assertIn("mcp_servers.unused.enabled=false", calls[0])
        self.assertEqual(result.launch_profile["requested_config_overrides"], ["mcp_servers.unused.enabled=false"])
        self.assertEqual(result.launch_profile["effective_tool_reduction"], "unverified")

    def test_unsupported_named_profile_fails_before_worker_dispatch(self):
        host = CodexHostAdapter(
            worker_context=WorkerContext(user_profile="focused"), cli_capabilities=CliCapabilities(checked=True),
        )
        with TemporaryDirectory() as temporary, patch("tasktra.host.shutil.which", return_value="codex"):
            with self.assertRaisesRegex(HostError, "named-user-profile"):
                host.run(prompt="Inspect the artifact.", workspace=Path(temporary), model=None, effort=None,
                         sandbox="read-only", timeout_seconds=5, on_started=lambda _: None, on_tick=lambda: None)

    def test_capability_help_is_checked_once_per_adapter_before_named_profile_launches(self):
        help_calls, worker_calls = [], []

        def help_run(argv, **kwargs):
            help_calls.append(argv)
            return SimpleNamespace(returncode=0, timed_out=False, output_limited=False, dispatched=True,
                                   stdout=b"--profile --config")

        def worker_run(argv, **kwargs):
            worker_calls.append(argv)
            feed = kwargs["on_stdout_chunk"]
            feed(event({"type": "thread.started", "thread_id": f"thread-{len(worker_calls)}"}))
            feed(event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
                "status": "completed", "summary": "Done.", "findings": [], "changed_paths": [],
            })}}))
            feed(event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
            return SimpleNamespace(returncode=0, timed_out=False, output_limited=False, input_uncertain=False, dispatched=True)

        host = CodexHostAdapter(runner_factory=lambda **kwargs: SimpleNamespace(run=worker_run),
                                worker_context=WorkerContext(user_profile="focused"))
        with TemporaryDirectory() as temporary, patch("tasktra.host.shutil.which", return_value="codex"), \
                patch("tasktra.host.ArgvProcessRunner", side_effect=lambda **kwargs: SimpleNamespace(run=help_run)):
            for _ in range(2):
                host.run(prompt="Inspect the artifact.", workspace=Path(temporary), model=None, effort=None,
                         sandbox="read-only", timeout_seconds=5, on_started=lambda _: None, on_tick=lambda: None)
        self.assertEqual(help_calls, [["codex", "--help"], ["codex", "exec", "--help"]])
        self.assertTrue(all("--profile" in argv for argv in worker_calls))

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
