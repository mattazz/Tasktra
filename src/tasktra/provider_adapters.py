"""Replaceable, bounded Git, GitHub CLI, and Jira provider adapters.

Adapters retain no credentials.  Hosts supply a runner or connector; this
module turns only validated descriptors, scopes, and short JSON into fixed
argument arrays or structured connector calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .providers import (
    MAX_PROVIDER_JSON_BYTES, OperationDescriptor, ProviderError, ProviderHealth,
    ProviderResult, READ_ONLY, ResourceScope, _bounded_json, load_bounded_provider_json,
)

MAX_COMMAND_OUTPUT_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 15
_RELATIVE_PATH = re.compile(
    r"^(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9.][A-Za-z0-9._/ -]*$"
)
_FULL_REF = re.compile(r"^refs/(?:heads|tags|remotes)/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_OID = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GIT_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_GITHUB_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_GITHUB_LIST_STATES = frozenset({"open", "closed", "all"})
MAX_GITHUB_BODY_CHARS = 4000
MAX_GITHUB_TITLE_CHARS = 256
MAX_GITHUB_LIST_LIMIT = 100
MAX_GITHUB_RECONCILIATION_LIMIT = 1000
_GITHUB_READS = frozenset({"github-issue-get", "github-pr-get", "github-issue-list", "github-pr-list", "github-status-get"})
_GITHUB_EFFECTS = {
    "github-repo-create": "remote-mutation",
    "github-issue-comment": "external-communication",
    "github-pr-comment": "external-communication",
    "github-pr-review": "external-communication",
    "github-issue-create": "remote-mutation",
    "github-pr-create": "remote-mutation",
    "github-status-set": "remote-mutation",
}
_JIRA_READS = frozenset({"jira-issue-get", "jira-issue-discovery"})
_JIRA_EFFECTS = {"jira-comment": "external-communication", "jira-transition": "remote-mutation"}
_JIRA_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_JIRA_ISSUE = re.compile(r"^([A-Z][A-Z0-9_]{0,31})-([1-9][0-9]{0,9})$")
MAX_JIRA_BODY_CHARS = 4000
MAX_JIRA_TRANSITION_CHARS = 100
MAX_JIRA_QUERY_CHARS = 500
_UNTRUSTED_GIT_TRANSPORT_CONFIG = (
    r"^(url\..*|credential(\..*)?|http\..*|core\.(sshcommand|gitproxy)|"
    r"remote\..*\.(proxy|vcs)|include(if)?\..*)$"
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    output_limited: bool = False
    input_uncertain: bool = False
    dispatched: bool = True


class _WindowsJob:
    """Kill-on-close Windows Job Object assigned immediately after spawn."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: object) -> None:
        self._handle = handle

    @classmethod
    def assign(cls, process: subprocess.Popen[bytes]) -> "_WindowsJob":
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_longlong),
                ("per_job_user_time_limit", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_operation_count", "write_operation_count", "other_operation_count",
                "read_transfer_count", "write_transfer_count", "other_transfer_count",
            )]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", _BasicLimitInformation),
                ("io_info", _IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimitInformation()
        info.basic_limit_information.limit_flags = cls._KILL_ON_JOB_CLOSE
        try:
            if not kernel32.SetInformationJobObject(
                handle, cls._EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
        return cls(handle)

    def close(self) -> None:
        if self._handle is None:
            return
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
        self._handle = None


class BoundedArgvRunner:
    """Run argument arrays only, with a small output budget and no shell."""

    def __init__(self, invoke: Callable[..., CommandResult] | None = None, *, timeout: int = DEFAULT_TIMEOUT_SECONDS,
                 output_limit: int = MAX_COMMAND_OUTPUT_BYTES) -> None:
        self._invoke, self.timeout, self.output_limit = invoke, timeout, output_limit

    def run(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], stdin: bytes = b"") -> CommandResult:
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ProviderError("command must be a non-empty argv array")
        if self._invoke is not None:
            raw = self._invoke(argv=tuple(argv), cwd=cwd, env=dict(env), stdin=stdin, timeout=self.timeout, shell=False)
        else:
            raw = self._run_capped(argv, cwd=cwd, env=env, stdin=stdin)
        if not isinstance(raw, CommandResult):
            raise ProviderError("runner returned an invalid command result")
        stdout, stderr = bytes(raw.stdout), bytes(raw.stderr)
        limited = raw.output_limited or len(stdout) > self.output_limit or len(stderr) > self.output_limit
        return CommandResult(
            raw.returncode, stdout[:self.output_limit], stderr[:self.output_limit], raw.timed_out, limited,
            raw.input_uncertain, raw.dispatched,
        )

    def _run_capped(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], stdin: bytes) -> CommandResult:
        """Capture at most the configured budget per stream while the child runs.

        Pipes are read in two small reader threads because Windows anonymous
        pipes cannot be selected portably.  Reaching either budget terminates
        the process group: keeping a child alive after discarding its output
        would merely move the unbounded-buffer problem into the OS pipe.
        """
        startup: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                    "cwd": cwd, "env": dict(env), "shell": False, "bufsize": 0}
        if os.name == "nt":
            startup["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        else:
            startup["start_new_session"] = True
        try:
            process = subprocess.Popen(argv, **startup)
        except OSError:
            return CommandResult(127, dispatched=False)
        windows_job: _WindowsJob | None = None
        if os.name == "nt":
            try:
                windows_job = _WindowsJob.assign(process)
                self._resume_windows_process(process)
            except OSError:
                # Fail closed while the root is still suspended.  A provider
                # command is never allowed to run without job ownership.
                if windows_job is not None:
                    windows_job.close()
                    windows_job = None
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                return CommandResult(127, dispatched=False)

        deadline = time.monotonic() + self.timeout

        stdout, stderr = bytearray(), bytearray()
        overflow = threading.Event()
        writer_failed = threading.Event()
        input_uncertain = threading.Event()
        terminate_once = threading.Event()

        def terminate() -> None:
            nonlocal windows_job
            if terminate_once.is_set():
                return
            terminate_once.set()
            if windows_job is not None:
                windows_job.close()
                windows_job = None
            self._terminate_process_tree(process)

        def reader(pipe: Any, sink: bytearray) -> None:
            try:
                while True:
                    # One extra byte distinguishes a full legitimate stream
                    # from an overflow without retaining unbounded content.
                    remaining = self.output_limit - len(sink)
                    chunk = pipe.read(min(4096, max(1, remaining + 1)))
                    if not chunk:
                        return
                    if len(chunk) > remaining:
                        sink.extend(chunk[:max(0, remaining)])
                        overflow.set()
                        terminate()
                        return
                    sink.extend(chunk)
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        def writer() -> None:
            try:
                assert process.stdin is not None
                if stdin:
                    written = process.stdin.write(stdin)
                    if written is not None and written != len(stdin):
                        input_uncertain.set()
                        terminate()
            except (OSError, BrokenPipeError):
                writer_failed.set()
                input_uncertain.set()
                if process.poll() is None:
                    terminate()
            finally:
                try:
                    assert process.stdin is not None
                    process.stdin.close()
                except OSError:
                    pass

        try:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            readers = [threading.Thread(target=reader, args=(process.stdout, stdout), daemon=True),
                       threading.Thread(target=reader, args=(process.stderr, stderr), daemon=True)]
            input_writer = threading.Thread(target=writer, daemon=True)
            for thread in readers:
                thread.start()
            input_writer.start()
            timed_out = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    terminate()
                    break
                time.sleep(0.01)
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(process)
                returncode = process.wait(timeout=2)
            # Kill-on-close applies even after a normal parent exit: a command
            # may otherwise detach descendants that retain inherited handles
            # and outlive the bounded provider operation.
            if windows_job is not None:
                windows_job.close()
                windows_job = None
            input_writer.join(timeout=2)
            if input_writer.is_alive():
                input_uncertain.set()
                terminate()
            for thread in readers:
                thread.join(timeout=2)
            if writer_failed.is_set() and returncode == 0:
                returncode = 127
            return CommandResult(
                returncode, bytes(stdout), bytes(stderr), timed_out, overflow.is_set(),
                input_uncertain.is_set(), True,
            )
        except (OSError, BrokenPipeError):
            terminate()
            return CommandResult(127, bytes(stdout), bytes(stderr), False, overflow.is_set(), True, True)
        finally:
            if windows_job is not None:
                windows_job.close()

    @staticmethod
    def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
        """Resume a suspended process only after Job Object assignment."""
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(wintypes.HANDLE(process._handle)))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xffffffff:08x}")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        """Terminate the process group without invoking a shell."""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                # CTRL_BREAK_EVENT is unreliable for non-console children;
                # taskkill /T is the portable process-tree fallback.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], shell=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                if process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], shell=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                if process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass


def _git_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_OPTIONAL_LOCKS": "0"}


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not _RELATIVE_PATH.fullmatch(value) or "\\" in value:
        raise ProviderError("path must be a contained portable relative path")
    return value


def _full_ref(value: Any) -> str:
    if not isinstance(value, str) or not _FULL_REF.fullmatch(value) or ".." in value or "@{" in value:
        raise ProviderError("ref must be a validated full ref")
    return value


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise ProviderError("oid must be a hexadecimal Git object id")
    return value


def _full_oid(value: Any) -> str:
    if not isinstance(value, str) or not _FULL_OID.fullmatch(value):
        raise ProviderError("push object ids must be full 40- or 64-character hexadecimal Git object ids")
    return value


class GitAdapter:
    """Repository-contained Git adapter.  Push exists only as ``git-push``."""

    def __init__(self, repository: str | Path, runner: BoundedArgvRunner | None = None) -> None:
        self.repository = Path(repository).resolve()
        self.runner = runner or BoundedArgvRunner()
        # A process-owned empty directory overrides repository/global hook
        # configuration for every Git command, including update-ref's
        # reference-transaction hook.  Holding the TemporaryDirectory keeps it
        # private and alive for exactly the adapter lifetime.
        self._hook_sandbox = TemporaryDirectory(prefix="tasktra-git-hooks-")
        self._hooks_path = Path(self._hook_sandbox.name).resolve()

    def _run(self, args: list[str], stdin: bytes = b"") -> CommandResult:
        return self.runner.run([
            "git", "-c", f"safe.directory={self.repository.as_posix()}",
            "-c", f"core.hooksPath={self._hooks_path}", "-c", "core.fsmonitor=false",
            "-c", "credential.helper=", "-c", "core.sshCommand=",
            "-C", str(self.repository), *args,
        ], cwd=self.repository, env=_git_env(), stdin=stdin)

    def close(self) -> None:
        """Remove the private hook sandbox eagerly when the adapter is done."""
        sandbox = getattr(self, "_hook_sandbox", None)
        if sandbox is not None:
            sandbox.cleanup()

    def __del__(self) -> None:
        self.close()

    def _verify_repository(self) -> bool:
        result = self._run(["rev-parse", "--show-toplevel"])
        if result.returncode != 0 or result.timed_out:
            return False
        try:
            return Path(result.stdout.decode("utf-8").strip()).resolve() == self.repository
        except UnicodeDecodeError:
            return False

    def discover(self) -> ProviderHealth:
        return ProviderHealth("git", "available", "Git repository is available") if self._verify_repository() else ProviderHealth("git", "unavailable", "Git repository is unavailable")

    def read(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult:
        if descriptor.effect_class != READ_ONLY or descriptor.provider != "git":
            raise ProviderError("Git read descriptor has the wrong provider or effect class")
        self._validate_scope(scope)
        if not self._verify_repository():
            return ProviderResult("unavailable", "Git repository is unavailable")
        if descriptor.request_kind == "git-status":
            if scope.ref is not None:
                raise ProviderError("Git status requires a repository scope without a ref")
            result = self._run(["status", "--porcelain=v1", "--branch", "-z"])
        elif descriptor.request_kind == "git-diff":
            path = _relative_path(request.get("path"))
            base, head = request.get("base"), request.get("head")
            if base is None and head is None:
                if scope.ref is not None:
                    raise ProviderError("Git working-tree diff requires a repository scope without a ref")
                result = self._run(["diff", "--no-ext-diff", "--", path])
            elif base is not None and head is not None:
                base_oid, head_oid = _full_oid(base), _full_oid(head)
                if scope.ref != head_oid:
                    raise ProviderError("Git object diff head differs from the authorized scope ref")
                result = self._run(["diff", "--no-ext-diff", base_oid, head_oid, "--", path])
            else:
                raise ProviderError("Git diff requires both base and head object ids")
        elif descriptor.request_kind == "git-log":
            ref = _full_ref(scope.ref)
            if request.get("ref", ref) != ref:
                raise ProviderError("Git log ref differs from the authorized scope")
            result = self._run(["log", "--no-decorate", "--format=%H%x00%s", "-n", "20", ref])
        else:
            raise ProviderError("unsupported Git read operation")
        return _command_result(result, "Git read")

    def execute(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        if descriptor.provider != "git" or descriptor.effect_class != "repository-history":
            raise ProviderError("Git effect descriptor has the wrong provider or effect class")
        self._validate_scope(scope)
        if not self._verify_repository():
            return ProviderResult("unavailable", "Git repository is unavailable")
        if descriptor.request_kind == "git-commit":
            branch_ref = _full_ref(scope.ref)
            if not branch_ref.startswith("refs/heads/"):
                raise ProviderError("Git commit requires an authorized branch ref")
            expected_parent_value = request.get("expected_parent_oid")
            root_commit = expected_parent_value is None
            expected_parent = None if root_commit else _full_oid(expected_parent_value)
            paths = request.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ProviderError("Git commit requires non-empty paths")
            wanted = sorted({_relative_path(path) for path in paths})
            staged = self._run(["diff", "--cached", "--name-only", "-z"])
            actual = sorted(item for item in staged.stdout.decode("utf-8", "replace").split("\0") if item)
            if staged.returncode != 0 or actual != wanted:
                raise ProviderError("Git commit refuses unrelated or unstaged work")
            message = request.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 500:
                raise ProviderError("Git commit message must be bounded text")
            self._verify_commit_head(branch_ref, expected_parent)
            # Build immutable objects first, verify their exact path/parent
            # content, then publish with update-ref's compare-and-swap.  The
            # authorized branch can never be advanced from a different parent.
            tree_result = self._run(["write-tree"])
            tree_oid = _git_object_result(tree_result, "Git commit tree", write=True)
            if isinstance(tree_oid, ProviderResult):
                return tree_oid
            changed = self._run(
                ["ls-tree", "-r", "--name-only", "-z", tree_oid]
                if root_commit else
                ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", expected_parent, tree_oid]
            )
            if changed.returncode != 0 or changed.timed_out or changed.output_limited:
                return ProviderResult("failed", "Git commit tree could not be verified")
            actual_tree_paths = sorted(item for item in changed.stdout.decode("utf-8", "replace").split("\0") if item)
            if actual_tree_paths != wanted:
                raise ProviderError("Git commit tree contains unrelated work")
            commit_args = ["-c", "commit.gpgSign=false", "commit-tree", tree_oid]
            if expected_parent is not None:
                commit_args.extend(["-p", expected_parent])
            commit_args.extend(["-F", "-"])
            object_result = self._run(commit_args, message.encode("utf-8"))
            new_oid = _git_object_result(object_result, "Git commit object", write=True)
            if isinstance(new_oid, ProviderResult):
                return new_oid
            if root_commit:
                parent_check = self._run(["rev-list", "--parents", "-n", "1", new_oid])
                parent_outcome = _command_result(parent_check, "Git root commit parent verification")
                try:
                    parent_tokens = parent_check.stdout.decode("ascii").strip().split()
                except UnicodeDecodeError:
                    parent_tokens = []
                if parent_outcome.state != "succeeded" or parent_tokens != [new_oid]:
                    return ProviderResult("indeterminate", "Git root commit object has an unexpected parent")
                expected_ref = "0" * len(new_oid)
            else:
                parent_check = self._run(["rev-parse", "--verify", f"{new_oid}^1"])
                parent_oid = _git_object_result(parent_check, "Git commit parent verification")
                if isinstance(parent_oid, ProviderResult) or parent_oid != expected_parent:
                    return ProviderResult("indeterminate", "Git commit object has an unexpected parent")
                expected_ref = expected_parent
            publish = self._run(["update-ref", branch_ref, new_oid, expected_ref])
            published = _write_command_result(publish, "Git commit reference update")
            if published.state != "succeeded":
                return published
            return ProviderResult("succeeded", "Git commit succeeded", ({"oid": new_oid, "parent_oid": expected_parent},))
        if descriptor.request_kind == "git-push":
            remote, ref, expected_old, new_oid = self._push_request(scope, request)
            target = self._push_target(scope, remote)
            if isinstance(target, ProviderResult):
                return target
            remote_url, _, _ = target
            absent_expected = set(expected_old) == {"0"}
            before = self._remote_ref(remote_url, ref, allow_absent=absent_expected)
            if before.state == "reconciled":
                return before
            if before.state != "succeeded":
                return before
            current = before.items[0]["oid"]
            if current == new_oid:
                return ProviderResult("reconciled", "Git push already reached the expected remote object")
            if current != expected_old:
                return ProviderResult("failed", "Git push conflicts with the expected remote object")
            # The explicit source object and force-with-lease bind the exact
            # effect.  There is deliberately no retry path in this adapter.
            transport = self._verify_transport_url(remote_url)
            if isinstance(transport, ProviderResult):
                return transport
            lease_expectation = "" if absent_expected else expected_old
            result = self._run([
                "push", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
                "--no-push-option", "--no-signed", "--no-force-if-includes", "--porcelain",
                f"--force-with-lease={ref}:{lease_expectation}", remote_url,
                                f"{new_oid}:{ref}"])
            return _write_command_result(result, "Git push")
        raise ProviderError("Git effects may only be explicit commit or push operations")

    def reconcile(self, descriptor: OperationDescriptor, scope: ResourceScope,
                  request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        """Read-only operation-aware reconciliation for a protected Git write."""
        del idempotency_key  # Git push is bound by exact refs and object ids.
        if descriptor.provider != "git" or descriptor.request_kind != "git-push" or descriptor.effect_class != "repository-history":
            raise ProviderError("Git operation does not support reconciliation")
        return self.reconcile_push(scope, request)

    def reconcile_push(self, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult:
        """Read-only exact-object reconciliation for an attempted Git push."""
        self._validate_scope(scope)
        if not self._verify_repository():
            return ProviderResult("unavailable", "Git repository is unavailable")
        remote, ref, expected_old, new_oid = self._push_request(scope, request)
        target = self._push_target(scope, remote)
        if isinstance(target, ProviderResult):
            return target
        remote_url, _, _ = target
        result = self._remote_ref(remote_url, ref, allow_absent=set(expected_old) == {"0"})
        if result.state != "succeeded":
            return result
        current = result.items[0]["oid"]
        if current == new_oid:
            return ProviderResult("reconciled", "Git push reached the expected remote object")
        if current == expected_old:
            return ProviderResult("absent", "Git push is absent at the expected remote object")
        return ProviderResult("conflict", "Git push conflicts with the remote object")

    def _push_request(self, scope: ResourceScope, request: Mapping[str, Any]) -> tuple[str, str, str, str]:
        remote, ref = request.get("remote"), request.get("ref")
        if not isinstance(remote, str) or not _REMOTE.fullmatch(remote):
            raise ProviderError("Git push remote is invalid")
        ref = _full_ref(ref)
        if not ref.startswith("refs/heads/"):
            raise ProviderError("Git push may target only a branch ref")
        if scope.ref != ref:
            raise ProviderError("Git push ref differs from the authorized scope")
        return remote, ref, _full_oid(request.get("expected_old_oid")), _full_oid(request.get("new_oid"))

    def _push_target(self, scope: ResourceScope, remote: str) -> tuple[str, str, str] | ProviderResult:
        """Resolve once and bind the local repository and immutable remote target.

        The remote *name* is only a local configuration lookup.  The resolved
        URL is compared to the authorized host/container and then passed to
        both `ls-remote` and `push`, preventing a later config lookup from
        redirecting a previously authorized effect.
        """
        result = self._run(["remote", "get-url", "--push", remote])
        if result.timed_out:
            return ProviderResult("indeterminate", "Git remote URL resolution timed out")
        if result.output_limited:
            return ProviderResult("failed", "Git remote URL resolution exceeded its output budget")
        if result.returncode != 0:
            return ProviderResult("failed", "Git remote URL resolution failed")
        try:
            remote_url = result.stdout.decode("utf-8").strip()
            host, container = _git_remote_destination(remote_url)
        except (UnicodeDecodeError, ProviderError):
            return ProviderResult("failed", "Git remote URL resolution returned an invalid destination")
        if scope.host != host or scope.container != container:
            raise ProviderError("Git remote destination differs from the authorized scope")
        transport = self._verify_transport_url(remote_url)
        if isinstance(transport, ProviderResult):
            return transport
        return remote_url, host, container

    def _verify_transport_url(self, remote_url: str) -> str | ProviderResult:
        """Reject repository-controlled transport behavior before network access.

        Stage 4 deliberately permits only plain HTTPS transport.  Authentication
        must come from a trusted injected runner/host integration; arbitrary
        repository credential helpers, proxies, headers, and URL rewrites are
        not executed by the default adapter.
        """
        config = self._run([
            "config", "--local", "--no-includes", "--name-only", "--get-regexp",
            _UNTRUSTED_GIT_TRANSPORT_CONFIG,
        ])
        if config.timed_out:
            return ProviderResult("indeterminate", "Git transport configuration verification timed out")
        if config.output_limited:
            return ProviderResult("failed", "Git transport configuration verification exceeded its output budget")
        if config.returncode == 0 or config.stdout.strip():
            return ProviderResult("failed", "Git repository contains untrusted transport configuration")
        if config.returncode != 1:
            return ProviderResult("failed", "Git transport configuration could not be verified")
        result = self._run(["ls-remote", "--get-url", remote_url])
        if result.timed_out:
            return ProviderResult("indeterminate", "Git transport URL verification timed out")
        if result.output_limited or result.returncode != 0:
            return ProviderResult("failed", "Git transport URL could not be verified")
        try:
            resolved = result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            return ProviderResult("failed", "Git transport URL verification returned malformed data")
        if resolved != remote_url:
            return ProviderResult("failed", "Git transport URL was rewritten by untrusted configuration")
        return resolved

    def _validate_scope(self, scope: ResourceScope) -> None:
        """Bind every Git operation to this local repository and scope identity.

        Read and commit operations remain entirely local: their scope's
        host/container are part of the approved repository identity, rather
        than a remote lookup.  Push performs the additional destination
        lookup in ``_push_target`` because it can affect another repository.
        """
        if scope.provider != "git" or scope.resource_kind != "repository" or scope.resource is None:
            raise ProviderError("Git operation requires a fingerprinted repository scope")
        _validate_git_destination(scope.host, scope.container)
        if scope.container.endswith(".git"):
            raise ProviderError("Git scope must name a canonical repository container")
        expected = git_scope_fingerprint(self.repository, scope.host, scope.container)
        if scope.resource != expected:
            raise ProviderError("Git repository fingerprint does not match the authorized scope")

    def _verify_commit_head(self, branch_ref: str, expected_parent: str | None) -> None:
        branch = self._run(["symbolic-ref", "-q", "HEAD"])
        parent = self._run(["rev-parse", "--verify", "HEAD^{commit}"])
        if branch.returncode != 0 or branch.timed_out or branch.output_limited:
            raise ProviderError("Git commit requires an attached authorized branch")
        if parent.timed_out or parent.output_limited:
            raise ProviderError("Git commit could not verify the expected parent")
        try:
            actual_branch = branch.stdout.decode("ascii").strip()
            actual_parent = None if parent.returncode != 0 else _full_oid(parent.stdout.decode("ascii").strip())
        except (UnicodeDecodeError, ProviderError) as error:
            raise ProviderError("Git commit head verification returned malformed data") from error
        if actual_branch != branch_ref:
            raise ProviderError("Git commit branch differs from the authorized scope")
        if expected_parent is None and actual_parent is not None:
            raise ProviderError("Git root commit requires an unborn branch")
        if expected_parent is not None and actual_parent != expected_parent:
            raise ProviderError("Git commit parent differs from the expected parent")

    def _remote_ref(self, remote: str, ref: str, *, allow_absent: bool = False) -> ProviderResult:
        result = self._run(["ls-remote", "--refs", remote, ref])
        if result.timed_out:
            return ProviderResult("indeterminate", "Git remote reconciliation timed out")
        if result.output_limited:
            return ProviderResult("indeterminate", "Git remote reconciliation exceeded its output budget")
        if result.returncode != 0:
            return ProviderResult("indeterminate", "Git remote reconciliation failed")
        try:
            lines = [line for line in result.stdout.decode("ascii").splitlines() if line]
        except UnicodeDecodeError:
            return ProviderResult("indeterminate", "Git remote reconciliation returned malformed data")
        if not lines and allow_absent:
            return ProviderResult("succeeded", "Git remote ref is absent as expected", ({"oid": "0" * 40},))
        if len(lines) != 1 or "\t" not in lines[0]:
            return ProviderResult("indeterminate", "Git remote reconciliation returned an absent or malformed ref")
        oid, actual_ref = lines[0].split("\t", 1)
        try:
            oid = _full_oid(oid)
        except ProviderError:
            return ProviderResult("indeterminate", "Git remote reconciliation returned an invalid object")
        if actual_ref != ref:
            return ProviderResult("indeterminate", "Git remote reconciliation returned a different ref")
        return ProviderResult("succeeded", "Git remote reconciliation succeeded", ({"oid": oid},))


def git_scope_fingerprint(repository: str | Path, host: str, container: str) -> str:
    """Return the non-secret v1 identity for one resolved local/remote Git pair."""
    resolved = str(Path(repository).resolve())
    payload = f"tasktra.git-scope.v1\0{resolved}\0{host.casefold()}\0{container}".encode("utf-8")
    return sha256(payload).hexdigest()


def _validate_git_destination(host: str, container: str) -> None:
    labels = host.split(".")
    if (host != host.casefold() or len(host) > 253 or not labels
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   or not re.fullmatch(r"[a-z0-9-]+", label) for label in labels)):
        raise ProviderError("Git scope host must be a canonical host name")
    if (not _GIT_CONTAINER.fullmatch(container) or "//" in container
            or ".." in container.split("/") or container.startswith("/") or container.endswith("/")):
        raise ProviderError("Git scope must name a canonical repository container")


def _git_remote_destination(value: str) -> tuple[str, str]:
    """Parse a credential-free HTTPS remote into its canonical destination."""
    if (not value or len(value) > 2048 or any(ord(char) < 32 for char in value)
            or "?" in value or "#" in value or "@" in value):
        raise ProviderError("Git remote URL is invalid")
    parsed = urlparse(value)
    if "://" in value:
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ProviderError("Git push supports only canonical HTTPS remote URLs")
        try:
            port = parsed.port
        except ValueError as error:
            raise ProviderError("Git remote URL has an invalid port") from error
        if parsed.username is not None or parsed.password is not None or port is not None:
            raise ProviderError("Git remote URL contains userinfo or an explicit port")
        if parsed.query or parsed.fragment or parsed.params:
            raise ProviderError("Git remote URL must not contain parameters, a query, or a fragment")
        host, path = parsed.hostname.casefold(), parsed.path.lstrip("/")
    else:
        raise ProviderError("Git push supports only canonical HTTPS remote URLs")
    container = path[:-4] if path.endswith(".git") else path
    _validate_git_destination(host, container)
    return host, container


def _command_result(result: CommandResult, label: str) -> ProviderResult:
    if result.timed_out:
        return ProviderResult("indeterminate", f"{label} timed out")
    if result.output_limited:
        return ProviderResult("failed", f"{label} exceeded its output budget")
    if result.returncode != 0:
        return ProviderResult("failed", f"{label} failed")
    text = result.stdout.decode("utf-8", "replace")
    return ProviderResult("succeeded", f"{label} succeeded", ({"output": text},))


def _git_object_result(result: CommandResult, label: str, *, write: bool = False) -> str | ProviderResult:
    outcome = _write_command_result(result, label) if write else _command_result(result, label)
    if outcome.state != "succeeded":
        return outcome
    try:
        return _full_oid(result.stdout.decode("ascii").strip())
    except (UnicodeDecodeError, ProviderError):
        state = "indeterminate" if write else "failed"
        return ProviderResult(state, f"{label} returned an invalid object id")


def _write_command_result(result: CommandResult, label: str) -> ProviderResult:
    """Conservative write classification: uncertainty is never retry-safe."""
    if not result.dispatched:
        return ProviderResult("failed", f"{label} was not dispatched")
    if result.timed_out or result.output_limited or result.input_uncertain:
        return ProviderResult("indeterminate", f"{label} outcome is indeterminate")
    if result.returncode != 0:
        return ProviderResult("indeterminate", f"{label} post-dispatch outcome is indeterminate")
    return _command_result(result, label)


class GitHubCliAdapter:
    """``gh`` adapter with fixed argv, stdin write bodies, and marker lookup."""

    def __init__(self, runner: BoundedArgvRunner | None = None) -> None:
        self.runner = runner

    def discover(self) -> ProviderHealth:
        return ProviderHealth("github", "available", "GitHub CLI adapter is configured") if self.runner else ProviderHealth("github", "unavailable", "GitHub CLI adapter is not configured")

    def _run(self, args: list[str], stdin: bytes = b"", *, host: str | None = None) -> CommandResult | None:
        if self.runner is None:
            return None
        environment = _github_env()
        if host is not None:
            environment["GH_HOST"] = host
        return self.runner.run(["gh", *args], cwd=Path.cwd(), env=environment, stdin=stdin)

    def read(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult:
        self._assert_read_descriptor(descriptor)
        repository = _github_repository(scope)
        if descriptor.request_kind == "github-issue-get":
            _github_resource_scope(scope, "issue")
            number = _github_number(scope, request)
            result = self._run(["issue", "view", number, "--repo", repository, "--json", "number,title,state,url"])
        elif descriptor.request_kind == "github-pr-get":
            _github_resource_scope(scope, "pull-request")
            number = _github_number(scope, request)
            result = self._run(["pr", "view", number, "--repo", repository, "--json", "number,title,state,url"])
        elif descriptor.request_kind == "github-issue-list":
            _github_collection_scope(scope, "issue")
            state, limit = _github_list_request(request)
            result = self._run(["issue", "list", "--repo", repository, "--state", state, "--limit", str(limit),
                                "--json", "number,title,state,url"])
        elif descriptor.request_kind == "github-pr-list":
            _github_collection_scope(scope, "pull-request")
            state, limit = _github_list_request(request)
            result = self._run(["pr", "list", "--repo", repository, "--state", state, "--limit", str(limit),
                                "--json", "number,title,state,url"])
        elif descriptor.request_kind == "github-status-get":
            oid = _github_status_scope(scope)
            result = self._run(["api", "--hostname", scope.host, f"repos/{scope.container}/commits/{oid}/status"])
        else:
            raise ProviderError("unsupported GitHub read operation")
        return _json_command_result(result, "GitHub read")

    def execute(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        self._assert_effect_descriptor(descriptor)
        repository = _github_repository(scope)
        if descriptor.request_kind == "github-repo-create":
            _github_repository_scope(scope)
            if set(request) != {"visibility", "description"}:
                raise ProviderError("GitHub repository creation request must contain only visibility and description")
            visibility = request.get("visibility")
            description = request.get("description")
            if visibility not in {"private", "public"}:
                raise ProviderError("GitHub repository visibility must be private or public")
            if not isinstance(description, str) or len(description) > 350 or any(ord(char) < 32 for char in description):
                raise ProviderError("GitHub repository description must be bounded text")
            # Unlike ``--repo`` consumers, ``gh repo create`` documents a
            # NAME/OWNER-NAME argument rather than HOST/OWNER/NAME.  Pin the
            # host in the environment and pass the exact authorized container.
            result = self._run(
                ["repo", "create", scope.container, f"--{visibility}", "--description", description],
                host=scope.host,
            )
            return _write_command_result(result, "GitHub repository creation") if result is not None else ProviderResult("unavailable", "GitHub CLI adapter is not configured")
        marker = _github_marker(idempotency_key)
        body = _github_body(request.get("body", ""))
        payload = (body + "\n\n" + marker + "\n").encode("utf-8")
        if descriptor.request_kind == "github-issue-comment":
            _github_resource_scope(scope, "issue")
            number = _github_number(scope, request)
            result = self._run(["issue", "comment", number, "--repo", repository, "--body-file", "-"], payload)
        elif descriptor.request_kind == "github-pr-review":
            _github_resource_scope(scope, "pull-request")
            number = _github_number(scope, request)
            result = self._run(["pr", "review", number, "--repo", repository, "--comment", "--body-file", "-"], payload)
        elif descriptor.request_kind == "github-pr-comment":
            _github_resource_scope(scope, "pull-request")
            number = _github_number(scope, request)
            result = self._run(["pr", "comment", number, "--repo", repository, "--body-file", "-"], payload)
        elif descriptor.request_kind == "github-issue-create":
            _github_collection_scope(scope, "issue")
            title = _github_title(request.get("title"))
            result = self._run(["issue", "create", "--repo", repository, "--title", title, "--body-file", "-"], payload)
        elif descriptor.request_kind == "github-pr-create":
            _github_collection_scope(scope, "pull-request")
            title = _github_title(request.get("title"))
            head, base = _github_branches(request)
            result = self._run(["pr", "create", "--repo", repository, "--title", title, "--head", head,
                                "--base", base, "--body-file", "-"], payload)
        elif descriptor.request_kind == "github-status-set":
            oid = _github_status_scope(scope)
            state, context = _github_status_request(request)
            status = {"state": state, "context": context, "description": (body + " " + marker).strip()}
            _bounded_json(status, label="GitHub status request")
            result = self._run(["api", "--hostname", scope.host, "-X", "POST", f"repos/{scope.container}/statuses/{oid}", "--input", "-"], json.dumps(status, separators=(",", ":")).encode("utf-8"))
        else:
            raise ProviderError("unsupported GitHub protected operation")
        return _write_command_result(result, "GitHub write") if result is not None else ProviderResult("unavailable", "GitHub CLI adapter is not configured")

    def reconcile(self, descriptor: OperationDescriptor, scope: ResourceScope,
                  request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        """Find one exact marker for a prior GitHub effect without mutating.

        The descriptor keeps reconciliation operation-aware, allowing retries
        only after one exact marker lookup completes with no matching effect.
        """
        _github_repository(scope)
        self._assert_effect_descriptor(descriptor)
        kind = descriptor.request_kind
        if kind == "github-repo-create":
            _github_repository_scope(scope)
            visibility = request.get("visibility")
            if visibility not in {"private", "public"}:
                raise ProviderError("GitHub repository visibility must be private or public")
            owner, name = scope.container.split("/", 1)
            query = (
                "query($owner:String!,$name:String!){"
                "repository(owner:$owner,name:$name){nameWithOwner visibility url}}"
            )
            result = self._run([
                "api", "graphql", "--hostname", scope.host,
                "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}",
            ])
            if result is None or result.timed_out or result.output_limited:
                return ProviderResult("indeterminate", "GitHub repository reconciliation did not complete")
            try:
                response = load_bounded_provider_json(result.stdout[:MAX_PROVIDER_JSON_BYTES])
            except ProviderError:
                return ProviderResult("indeterminate", "GitHub repository reconciliation returned malformed data")
            if not isinstance(response, Mapping) or not isinstance(response.get("data"), Mapping):
                return ProviderResult("indeterminate", "GitHub repository reconciliation returned malformed data")
            item = response["data"].get("repository")
            if item is None:
                errors = response.get("errors", [])
                exact_not_found = (
                    isinstance(errors, list)
                    and all(
                        isinstance(error, Mapping)
                        and error.get("type") == "NOT_FOUND"
                        and error.get("path") == ["repository"]
                        for error in errors
                    )
                )
                if result.returncode == 0 or (errors and exact_not_found):
                    return ProviderResult("absent", "GitHub repository is absent")
                return ProviderResult("indeterminate", "GitHub repository absence was not proven")
            if result.returncode != 0:
                return ProviderResult("indeterminate", "GitHub repository reconciliation did not complete")
            if not isinstance(item, Mapping):
                return ProviderResult("indeterminate", "GitHub repository reconciliation returned malformed data")
            expected_name = scope.container.casefold()
            expected_visibility = visibility.casefold()
            if str(item.get("nameWithOwner", "")).casefold() != expected_name:
                return ProviderResult("conflict", "GitHub repository reconciliation found a different repository")
            if str(item.get("visibility", "")).casefold() != expected_visibility:
                return ProviderResult("conflict", "GitHub repository reconciliation found different visibility")
            return ProviderResult("reconciled", "GitHub repository exists with the expected visibility", (item,))
        marker = _github_marker(idempotency_key)
        safe_request = dict(request)
        if kind in {"github-issue-comment", "github-pr-comment"}:
            _github_resource_scope(scope, "issue" if kind == "github-issue-comment" else "pull-request")
            number = _github_number(scope, safe_request)
            result = self._run(["api", "--hostname", scope.host, "--paginate", "--slurp",
                                f"repos/{scope.container}/issues/{number}/comments?per_page=100"])
            parsed = _json_command_result(result, "GitHub comment reconciliation")
            matches = _slurped_marker_matches(parsed, marker, field="body")
            compatible = matches
        elif kind == "github-pr-review":
            _github_resource_scope(scope, "pull-request")
            number = _github_number(scope, safe_request)
            result = self._run(["api", "--hostname", scope.host, "--paginate", "--slurp",
                                f"repos/{scope.container}/pulls/{number}/reviews?per_page=100"])
            parsed = _json_command_result(result, "GitHub review reconciliation")
            matches = _slurped_marker_matches(parsed, marker, field="body")
            compatible = matches
        elif kind == "github-issue-create":
            _github_collection_scope(scope, "issue")
            title = _github_title(safe_request.get("title"))
            result = self._run(["issue", "list", "--repo", _github_repository(scope), "--state", "all", "--limit",
                                str(MAX_GITHUB_RECONCILIATION_LIMIT), "--search", f"{marker} in:body",
                                "--json", "number,title,state,url,body"])
            parsed = _json_command_result(result, "GitHub issue reconciliation")
            matches = _marker_matches(parsed, marker, field="body")
            compatible = [item for item in matches if item.get("title") == title]
        elif kind == "github-pr-create":
            _github_collection_scope(scope, "pull-request")
            title = _github_title(safe_request.get("title"))
            head, base = _github_branches(safe_request)
            result = self._run(["pr", "list", "--repo", _github_repository(scope), "--state", "all", "--limit",
                                str(MAX_GITHUB_RECONCILIATION_LIMIT), "--search", f"{marker} in:body",
                                "--json", "number,title,state,url,body,headRefName,baseRefName"])
            parsed = _json_command_result(result, "GitHub pull-request reconciliation")
            matches = _marker_matches(parsed, marker, field="body")
            compatible = [item for item in matches if item.get("title") == title
                          and item.get("headRefName") == head and item.get("baseRefName") == base]
        elif kind == "github-status-set":
            oid = _github_status_scope(scope)
            state, context = _github_status_request(safe_request)
            result = self._run(["api", "--hostname", scope.host, "--paginate", "--slurp",
                                f"repos/{scope.container}/commits/{oid}/statuses?per_page=100"])
            parsed = _json_command_result(result, "GitHub status reconciliation")
            matches = _slurped_marker_matches(parsed, marker, field="description")
            compatible = [item for item in matches
                          if item.get("state") == state and item.get("context") == context]
        else:
            raise ProviderError("GitHub operation does not support marker reconciliation")
        if result is None:
            return ProviderResult("unavailable", "GitHub CLI adapter is not configured")
        # Do not report an absence after an unavailable, malformed, limited, or
        # failed lookup.  That would turn a transport failure into a duplicate.
        if parsed.state != "succeeded":
            return ProviderResult("indeterminate", "GitHub reconciliation did not complete")
        if len(matches) > 1:
            return ProviderResult("conflict", "GitHub reconciliation found duplicate idempotency markers", tuple(matches[:2]))
        if len(matches) == 1 and len(compatible) == 1:
            return ProviderResult("reconciled", "GitHub idempotency marker was found", tuple(compatible))
        if len(matches) == 1:
            return ProviderResult("conflict", "GitHub idempotency marker belongs to a different operation", tuple(matches))
        # A bounded remote snapshot can prove presence but cannot prove that a
        # non-idempotent write is absent.  Only provider-specific durable
        # absence evidence may unlock a retry.
        return ProviderResult("indeterminate", "GitHub reconciliation found no marker in the bounded history")

    @staticmethod
    def _assert_read_descriptor(descriptor: OperationDescriptor) -> None:
        if (descriptor.provider != "github" or descriptor.request_kind not in _GITHUB_READS
                or descriptor.effect_class != READ_ONLY):
            raise ProviderError("GitHub read descriptor has the wrong provider, capability, or effect class")

    @staticmethod
    def _assert_effect_descriptor(descriptor: OperationDescriptor) -> None:
        expected = _GITHUB_EFFECTS.get(descriptor.request_kind)
        if descriptor.provider != "github" or expected is None or descriptor.effect_class != expected:
            raise ProviderError("GitHub effect descriptor has the wrong provider, capability, or effect class")


def _github_repository(scope: ResourceScope) -> str:
    if scope.provider != "github" or not _GITHUB_REPOSITORY.fullmatch(scope.container):
        raise ProviderError("GitHub scope must name a canonical owner/repository container")
    # ``gh --repo`` accepts HOST/OWNER/REPO.  Including the host avoids a
    # silently redirected operation when the current GH_HOST is different.
    return f"{scope.host}/{scope.container}"


def _github_repository_scope(scope: ResourceScope) -> None:
    if scope.resource_kind != "repository" or scope.resource is not None or scope.ref is not None:
        raise ProviderError("GitHub repository operation requires an exact repository scope")


def _github_resource_scope(scope: ResourceScope, expected_kind: str) -> None:
    if scope.resource_kind != expected_kind or scope.ref is not None:
        raise ProviderError(f"GitHub operation requires an exact {expected_kind} resource scope")
    if scope.resource is None or not scope.resource.isdigit() or int(scope.resource) < 1:
        raise ProviderError(f"GitHub {expected_kind} scope requires a positive numeric resource")


def _github_status_scope(scope: ResourceScope) -> str:
    if scope.resource_kind != "commit-status" or scope.resource is not None:
        raise ProviderError("GitHub status operation requires a commit-status scope without a resource")
    return _full_oid(scope.ref)


def _github_env() -> dict[str, str]:
    """Pass only established GitHub CLI configuration/authentication through.

    The adapter neither discovers nor stores credentials.  ``GITHUB_TOKEN``
    is deliberately mapped only when the CLI-native ``GH_TOKEN`` is absent,
    so an explicitly configured GH token always wins.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "GH_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_NO_UPDATE_NOTIFIER": "1",
    }
    # Windows command-line clients require these runtime paths for DNS, TLS,
    # and temporary files. They are non-secret host plumbing; unrelated user
    # variables and credentials remain excluded.
    for output_name, candidates in {
        "SystemRoot": ("SystemRoot", "SYSTEMROOT"),
        "WINDIR": ("WINDIR", "windir"),
        "TEMP": ("TEMP",),
        "TMP": ("TMP",),
    }.items():
        value = next((os.environ.get(name) for name in candidates if os.environ.get(name)), None)
        if value:
            environment[output_name] = value
    github_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if github_token:
        environment["GH_TOKEN"] = github_token
    for name in ("GH_ENTERPRISE_TOKEN", "GH_HOST", "GH_CONFIG_DIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _github_number(scope: ResourceScope, request: Mapping[str, Any]) -> str:
    number = scope.resource if scope.resource is not None else request.get("number")
    requested = request.get("number")
    if (not isinstance(number, str) or not number.isdigit() or int(number) < 1
            or (scope.resource is not None and requested is not None and requested != scope.resource)):
        raise ProviderError("GitHub operation requires one matching positive numeric resource")
    return number


def _github_collection_scope(scope: ResourceScope, expected_kind: str) -> None:
    if scope.resource_kind != expected_kind or scope.resource is not None or scope.ref is not None:
        raise ProviderError(f"GitHub {expected_kind} collection operation requires an unbound {expected_kind} scope")


def _github_list_request(request: Mapping[str, Any]) -> tuple[str, int]:
    state = request.get("state", "open")
    limit = request.get("limit", 20)
    if state not in _GITHUB_LIST_STATES:
        raise ProviderError("GitHub list state must be open, closed, or all")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_GITHUB_LIST_LIMIT:
        raise ProviderError(f"GitHub list limit must be an integer from 1 to {MAX_GITHUB_LIST_LIMIT}")
    return state, limit


def _github_marker(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ProviderError("GitHub idempotency key must be bounded canonical text")
    return f"<!-- tasktra-idempotency:{idempotency_key} -->"


def _jira_marker(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ProviderError("Jira idempotency key must be bounded canonical text")
    return f"tasktra-idempotency:{idempotency_key}"


def _github_body(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_GITHUB_BODY_CHARS:
        raise ProviderError("GitHub write body must be bounded text")
    _bounded_json({"body": value}, label="GitHub write body")
    return value


def _github_title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_GITHUB_TITLE_CHARS:
        raise ProviderError("GitHub title must be non-empty bounded text")
    _bounded_json({"title": value}, label="GitHub title")
    return value


def _github_branch(value: Any, *, label: str) -> str:
    if (not isinstance(value, str) or not _GITHUB_BRANCH.fullmatch(value) or value.startswith("refs/")
            or ".." in value or "@{" in value or value.endswith((".", "/"))):
        raise ProviderError(f"GitHub {label} branch must be a bounded canonical branch name")
    return value


def _github_branches(request: Mapping[str, Any]) -> tuple[str, str]:
    return _github_branch(request.get("head"), label="head"), _github_branch(request.get("base"), label="base")


def _github_status_request(request: Mapping[str, Any]) -> tuple[str, str]:
    state, context = request.get("state"), request.get("context")
    if (state not in {"error", "failure", "pending", "success"}
            or not isinstance(context, str) or not context.strip() or len(context) > 100):
        raise ProviderError("GitHub status requires a valid state and context")
    return state, context


def _marker_matches(result: ProviderResult, marker: str, *, field: str) -> list[dict[str, Any]]:
    """Flatten bounded result items and return exact marker-bearing records."""
    if result.state != "succeeded":
        return []
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            candidate = value.get(field)
            if isinstance(candidate, str) and marker in candidate:
                found.append(dict(value))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for item in result.items:
        visit(item)
    return found


def _slurped_marker_matches(result: ProviderResult, marker: str, *, field: str) -> list[dict[str, Any]]:
    """Validate ``gh api --paginate --slurp`` shape before trusting history.

    Each top-level item is one complete page.  A flat array/object indicates
    that pagination/slurping was bypassed or malformed and therefore cannot
    support a reconciliation decision.
    """
    if result.state != "succeeded":
        return []
    if (not result.items or any(not isinstance(page, list) for page in result.items)
            or any(not isinstance(record, Mapping) for page in result.items for record in page)):
        return []
    return _marker_matches(result, marker, field=field)


def _json_command_result(result: CommandResult | None, label: str) -> ProviderResult:
    if result is None:
        return ProviderResult("unavailable", f"{label} adapter is not configured")
    if result.timed_out:
        return ProviderResult("indeterminate", f"{label} timed out")
    if result.output_limited:
        return ProviderResult("failed", f"{label} exceeded its output budget")
    if result.returncode != 0:
        return ProviderResult("failed", f"{label} failed")
    try:
        value = load_bounded_provider_json(result.stdout[:MAX_PROVIDER_JSON_BYTES])
        items = tuple(value if isinstance(value, list) else [value])
        return ProviderResult("succeeded", f"{label} succeeded", items)
    except ProviderError:
        return ProviderResult("failed", f"{label} returned malformed data")


class JiraConnectorAdapter:
    """Structured, injected Jira connector.  No session or credentials are stored."""

    def __init__(self, connector: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self._connector = connector

    def discover(self) -> ProviderHealth:
        return ProviderHealth("jira", "available", "Jira connector is configured") if self._connector else ProviderHealth("jira", "unavailable", "Jira connector is not configured")

    def read(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult:
        self._assert_read_descriptor(descriptor, scope)
        if descriptor.request_kind == "jira-issue-get":
            _jira_issue_scope(scope, request)
        else:
            _jira_project_scope(scope, request)
        return self._call(descriptor, scope, request, None)

    def execute(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        self._assert_effect_descriptor(descriptor, scope)
        _jira_issue_scope(scope, request)
        if descriptor.request_kind == "jira-comment":
            _jira_body(request.get("body"))
        else:
            _jira_transition(request.get("transition"))
        return self._call(descriptor, scope, request, _jira_marker(idempotency_key))

    def reconcile(self, descriptor: OperationDescriptor, scope: ResourceScope,
                  request: Mapping[str, Any], idempotency_key: str) -> ProviderResult:
        """Ask the injected connector for a marker-only reconciliation.

        This adapter has no network client of its own.  The explicit flag makes
        its structured connector call read-only by contract while preserving a
        connector's typed applied/absent/conflict/indeterminate result.
        """
        self._assert_effect_descriptor(descriptor, scope)
        _jira_issue_scope(scope, request)
        if descriptor.request_kind == "jira-comment":
            _jira_body(request.get("body"))
        else:
            _jira_transition(request.get("transition"))
        outcome = self._call(descriptor, scope, request, _jira_marker(idempotency_key), reconcile_only=True)
        if outcome.state == "absent":
            return ProviderResult(
                "indeterminate",
                "Jira bounded reconciliation cannot prove retry-safe absence for a non-idempotent write",
            )
        return outcome

    @staticmethod
    def _assert_read_descriptor(descriptor: OperationDescriptor, scope: ResourceScope) -> None:
        if (descriptor.provider != "jira" or scope.provider != "jira" or descriptor.request_kind not in _JIRA_READS
                or descriptor.effect_class != READ_ONLY):
            raise ProviderError("Jira read descriptor has the wrong provider, capability, or effect class")

    @staticmethod
    def _assert_effect_descriptor(descriptor: OperationDescriptor, scope: ResourceScope) -> None:
        expected = _JIRA_EFFECTS.get(descriptor.request_kind)
        if descriptor.provider != "jira" or scope.provider != "jira" or expected is None or descriptor.effect_class != expected:
            raise ProviderError("Jira effect descriptor has the wrong provider, capability, or effect class")

    def _call(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], marker: str | None,
              *, reconcile_only: bool = False) -> ProviderResult:
        if self._connector is None:
            return ProviderResult("unavailable", "Jira connector is not configured")
        payload: dict[str, Any] = {"operation": descriptor.to_dict(), "scope": scope.to_dict(), "request": dict(request)}
        if marker:
            payload["idempotency_marker"] = marker
        if reconcile_only:
            payload["reconcile_only"] = True
        _bounded_json(payload, label="Jira connector request")
        try:
            raw = self._connector(payload)
        except (TimeoutError, ConnectionError, OSError):
            return ProviderResult("indeterminate", "Jira connector transport outcome is indeterminate")
        except Exception:
            return ProviderResult("indeterminate", "Jira connector outcome is indeterminate")
        protected = marker is not None
        malformed_state = "indeterminate" if protected else "failed"
        malformed_summary = (
            "Jira protected connector returned malformed data after dispatch"
            if protected else "Jira connector returned malformed data"
        )
        if not isinstance(raw, Mapping):
            return ProviderResult(malformed_state, malformed_summary)
        try:
            return ProviderResult.from_mapping(raw)
        except (ProviderError, TypeError, ValueError):
            return ProviderResult(malformed_state, malformed_summary)
        except Exception:
            return ProviderResult("indeterminate", "Jira connector result could not be classified")


def _jira_project_scope(scope: ResourceScope, request: Mapping[str, Any]) -> None:
    if (scope.provider != "jira" or scope.resource_kind != "project" or scope.resource is not None
            or scope.ref is not None or not _JIRA_PROJECT.fullmatch(scope.container)):
        raise ProviderError("Jira discovery requires an exact project scope")
    requested = request.get("project")
    if requested is not None and requested != scope.container:
        raise ProviderError("Jira request project differs from the authorized scope")
    query = request.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > MAX_JIRA_QUERY_CHARS):
        raise ProviderError("Jira discovery query must be bounded text")


def _jira_issue_scope(scope: ResourceScope, request: Mapping[str, Any]) -> None:
    issue = scope.resource
    match = _JIRA_ISSUE.fullmatch(issue or "")
    if (scope.provider != "jira" or scope.resource_kind != "issue" or scope.ref is not None
            or match is None or not _JIRA_PROJECT.fullmatch(scope.container)
            or match.group(1) != scope.container):
        raise ProviderError("Jira operation requires an exact issue scope within its project")
    for key in ("issue", "issue_key"):
        requested = request.get(key)
        if requested is not None and requested != issue:
            raise ProviderError("Jira request issue differs from the authorized scope")
    requested_project = request.get("project")
    if requested_project is not None and requested_project != scope.container:
        raise ProviderError("Jira request project differs from the authorized scope")


def _jira_body(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_JIRA_BODY_CHARS:
        raise ProviderError("Jira comment body must be non-empty bounded text")
    _bounded_json({"body": value}, label="Jira comment body")
    return value


def _jira_transition(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > MAX_JIRA_TRANSITION_CHARS or any(ord(char) < 32 for char in value)):
        raise ProviderError("Jira transition must be canonical bounded text")
    return value
