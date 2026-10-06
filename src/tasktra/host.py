"""Bounded Codex CLI execution behind a small, observable host interface.

The host supplies execution evidence, never authority. Prompts and raw events
remain in memory; the caller decides which bounded result to retain.
"""
from __future__ import annotations

from dataclasses import dataclass
import codecs
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping

from .contracts import require_valid
from .processes import ArgvProcessRunner
from .worker_profiles import CliCapabilities, HostLaunchProfile, WorkerContext

MAX_EVENT_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 128 * 1024
STAGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "summary", "findings", "changed_paths"],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "Stage verdict and evidence, including successful checks and observations."},
        "findings": {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 1000},
                     "description": "Unresolved defects or blockers only. Empty when there are none; never include successful checks or informational observations."},
        "changed_paths": {"type": "array", "maxItems": 128, "items": {"type": "string", "maxLength": 240}},
    },
}

# Saved CLI authentication is resolved by Codex itself. Provider credentials,
# lease capabilities, proxies, and arbitrary caller variables are not inherited.
_ENVIRONMENT_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "CODEX_HOME",
})


def host_environment() -> dict[str, str]:
    """Minimal platform environment for a CLI using its saved authentication."""
    return {key: value for key, value in os.environ.items() if key.upper() in _ENVIRONMENT_KEYS}


class HostError(ValueError):
    """Host execution is unavailable, incomplete, or cannot be attributed."""


@dataclass(frozen=True)
class HostResult:
    thread_id: str
    response: Mapping[str, Any]
    usage: Mapping[str, int | None] | None
    launch_profile: Mapping[str, Any] | None = None


class CodexEventStream:
    """Parse bounded JSONL and accept exactly one thread and completed turn."""

    def __init__(self, on_started: Callable[[str], None]) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._pending = ""
        self._on_started = on_started
        self.thread_id: str | None = None
        self.message: str | None = None
        self.usage: dict[str, int | None] | None = None
        self.completed = False
        self.failed = False

    def feed(self, chunk: bytes) -> None:
        try:
            self._pending += self._decoder.decode(chunk)
        except UnicodeDecodeError as error:
            raise HostError("Codex emitted invalid UTF-8") from error
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._event(line)
        if len(self._pending.encode("utf-8")) > MAX_EVENT_BYTES:
            raise HostError("Codex event exceeds the byte bound")

    def _event(self, line: str) -> None:
        if not line.strip():
            return
        if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
            raise HostError("Codex event exceeds the byte bound")
        try:
            event = json.loads(line)
        except (ValueError, TypeError) as error:
            raise HostError("Codex emitted invalid JSONL") from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise HostError("Codex emitted an invalid event")
        kind = event["type"]
        if self.completed:
            raise HostError("Codex emitted events after turn completion")
        if kind == "thread.started":
            thread = event.get("thread_id")
            if self.thread_id is not None or not isinstance(thread, str) or not 1 <= len(thread) <= 128:
                raise HostError("Codex emitted conflicting thread identity")
            self.thread_id = thread
            self._on_started(thread)
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                if not isinstance(item.get("text"), str):
                    raise HostError("Codex final message is invalid")
                self.message = item["text"]
        elif kind in {"turn.failed", "error"}:
            self.failed = True
        elif kind == "turn.completed":
            if self.thread_id is None or self.failed:
                raise HostError("Codex turn cannot be accepted as completed")
            raw = event.get("usage")
            if raw is not None:
                if not isinstance(raw, dict):
                    raise HostError("Codex usage is invalid")
                values = {}
                for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"):
                    value = raw.get(key)
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                        raise HostError("Codex usage is invalid")
                    values[key] = value
                if values["input_tokens"] is not None and values["output_tokens"] is not None:
                    values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
                    self.usage = values
            self.completed = True

    def finish(self, *, launch_profile: HostLaunchProfile | None = None) -> HostResult:
        try:
            self._pending += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise HostError("Codex emitted incomplete UTF-8") from error
        if self._pending.strip():
            self._event(self._pending)
        if not self.completed or self.failed or self.thread_id is None or self.message is None:
            raise HostError("Codex did not produce an attributable completed result")
        try:
            response = json.loads(self.message)
            require_valid(response, STAGE_SCHEMA, label="host stage result")
        except ValueError as error:
            raise HostError("Codex stage result failed validation") from error
        return HostResult(self.thread_id, response, self.usage,
                          None if launch_profile is None else launch_profile.preview())


class CodexHostAdapter:
    """Run a fresh CLI session using saved CLI authentication and bounded I/O."""

    def __init__(self, executable: str = "codex", *, runner_factory: Any = ArgvProcessRunner,
                 worker_context: WorkerContext | None = None,
                 cli_capabilities: CliCapabilities | None = None) -> None:
        self.executable = executable
        self.runner_factory = runner_factory
        self.worker_context = worker_context
        self.cli_capabilities = cli_capabilities
        self._observed_cli_capabilities: CliCapabilities | None = None

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def launch_profile(self, workspace: Path) -> HostLaunchProfile:
        executable = shutil.which(self.executable)
        capabilities = (self._capabilities(executable, workspace)
                        if executable and self.worker_context is not None and self.worker_context.needs_capability_check else None)
        return HostLaunchProfile.from_context(self.worker_context, capabilities)

    def _capabilities(self, executable: str, workspace: Path) -> CliCapabilities:
        """Read only the installed executable's bounded help surfaces."""
        if self.cli_capabilities is not None:
            return self.cli_capabilities
        if self._observed_cli_capabilities is not None:
            return self._observed_cli_capabilities
        runner = ArgvProcessRunner(timeout=5, output_limit=64 * 1024)
        environment = host_environment()
        cli = runner.run([executable, "--help"], cwd=workspace, env=environment)
        command = runner.run([executable, "exec", "--help"], cwd=workspace, env=environment)
        if (cli.returncode != 0 or command.returncode != 0 or cli.timed_out or command.timed_out
                or cli.output_limited or command.output_limited or not cli.dispatched or not command.dispatched):
            self._observed_cli_capabilities = CliCapabilities()
            return self._observed_cli_capabilities
        try:
            self._observed_cli_capabilities = CliCapabilities.from_help(
                cli.stdout.decode("utf-8", "strict"), command.stdout.decode("utf-8", "strict")
            )
        except UnicodeDecodeError:
            self._observed_cli_capabilities = CliCapabilities()
        return self._observed_cli_capabilities

    def run(self, *, prompt: str, workspace: Path, model: str | None, effort: str | None,
            sandbox: str, timeout_seconds: int, on_started: Callable[[str], None],
            on_tick: Callable[[], None]) -> HostResult:
        if sandbox not in {"read-only", "workspace-write"}:
            raise HostError("host execution requires a bounded sandbox")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise HostError("host prompt is empty or exceeds the byte bound")
        executable = shutil.which(self.executable)
        if executable is None:
            raise HostError("Codex CLI is unavailable; install and authenticate it before running work")
        launch = self.launch_profile(workspace)
        if not launch.available:
            unavailable = ", ".join(launch.unavailable_capabilities)
            raise HostError(f"Codex CLI cannot safely apply requested worker context: {unavailable}")
        stream = CodexEventStream(on_started)
        # Schema lives outside the project so model edits cannot change the
        # contract. CLI output remains untrusted and is validated again here.
        with TemporaryDirectory(prefix="tasktra-host-") as temporary:
            schema = Path(temporary) / "stage.json"
            schema.write_text(json.dumps(STAGE_SCHEMA), encoding="utf-8")
            argv = [executable, "exec", "--json", "--color", "never", "--sandbox", sandbox,
                    "--cd", str(workspace), "--output-schema", str(schema),
                    "-c", "project_doc_fallback_filenames=[]"]
            if launch.user_profile is not None:
                argv += ["--profile", launch.user_profile]
            for override in launch.config_overrides:
                argv += ["-c", override]
            if model is not None:
                argv += ["--model", model]
            if effort is not None:
                argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
            argv.append("-")
            env = host_environment()
            runner = self.runner_factory(timeout=timeout_seconds, output_limit=8 * 1024 * 1024)
            result = runner.run(argv, cwd=workspace, env=env, stdin=prompt.encode("utf-8"),
                                on_stdout_chunk=stream.feed, on_tick=on_tick)
        if result.returncode != 0 or result.timed_out or result.output_limited or result.input_uncertain or not result.dispatched:
            raise HostError("Codex execution failed or exceeded a bound; inspect the execution receipt")
        return stream.finish(launch_profile=launch)
