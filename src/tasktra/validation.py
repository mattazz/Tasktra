"""Previewable, bounded execution of canonical project validation argv values."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
from threading import Thread
from time import monotonic
from typing import Iterable, Sequence


# Captures are deliberately bounded while a command is still running. Keeping
# the first bytes gives a useful failure prefix without unbounded memory use.
MAX_CAPTURE_CHARS = 12_000
_READ_CHUNK_BYTES = 4_096
_TERMINATION_GRACE_SECONDS = 2


class ValidationError(ValueError):
    """Raised when a validation plan is unsafe or cannot be executed."""


ValidationArgv = tuple[str, ...]


@dataclass(frozen=True)
class ValidationResult:
    argv: ValidationArgv
    status: str
    exit_code: int | None
    elapsed_ms: int
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "elapsed_ms": self.elapsed_ms,
            "exit_code": self.exit_code,
            "status": self.status,
            "stderr": self.stderr,
            "stdout": self.stdout,
        }


class _BoundedCapture:
    """Drain one pipe without retaining more than the configured byte cap."""

    def __init__(self) -> None:
        self._prefix = bytearray()
        self._total_bytes = 0

    def append(self, data: bytes) -> None:
        self._total_bytes += len(data)
        remaining = MAX_CAPTURE_CHARS - len(self._prefix)
        if remaining > 0:
            self._prefix.extend(data[:remaining])

    def render(self) -> str:
        text = bytes(self._prefix).decode(errors="replace")
        omitted = self._total_bytes - len(self._prefix)
        if omitted <= 0:
            return text
        suffix = f"\n... {omitted} bytes omitted"
        return text[: max(0, MAX_CAPTURE_CHARS - len(suffix))] + suffix


@dataclass
class _RunningProcess:
    process: subprocess.Popen[bytes]
    windows_job: "_WindowsJob | None" = None


class _WindowsJob:
    """A kill-on-close Windows Job Object for reliable descendant cleanup."""

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


def parse_command(command: str) -> ValidationArgv:
    """Reject the former string command format without trying to parse it.

    Shell-like strings cannot represent Windows quoting and trailing backslashes
    portably. Configuration and runtime callers must pass an argv array.
    """
    del command
    raise ValidationError(
        "string validation commands are unsupported; use an argv array such as "
        '["python", "-m", "unittest"]'
    )


def normalize_argv(command: Sequence[str]) -> ValidationArgv:
    """Validate and freeze one direct-execution argument vector."""
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise ValidationError("validation command must be a non-empty argv array of strings")
    if not command:
        raise ValidationError("validation command must contain an executable")
    argv = tuple(command)
    if not all(isinstance(item, str) and item for item in argv):
        raise ValidationError("validation argv items must be non-empty strings")
    if any("\x00" in item for item in argv):
        raise ValidationError("validation argv items must not contain NUL bytes")
    return argv


def validation_plan(commands: Iterable[Sequence[str]]) -> tuple[ValidationResult, ...]:
    return tuple(
        ValidationResult(normalize_argv(command), "planned", None, 0)
        for command in commands
    )


def run_validations(
    root: Path | str,
    commands: Iterable[Sequence[str]],
    *,
    timeout_seconds: int = 300,
) -> tuple[ValidationResult, ...]:
    """Run direct argv commands in order, stopping at the first non-pass result."""
    if timeout_seconds < 1:
        raise ValidationError("validation timeout must be positive")
    project = Path(root).resolve()
    if not project.is_dir():
        raise ValidationError(f"validation root is not a directory: {project}")
    results: list[ValidationResult] = []
    for item in validation_plan(commands):
        results.append(_run_one(project, item.argv, timeout_seconds))
        if results[-1].status != "passed":
            break
    return tuple(results)


def _run_one(project: Path, argv: ValidationArgv, timeout_seconds: int) -> ValidationResult:
    started = monotonic()
    try:
        running = _start_process(argv, project)
    except OSError as error:
        return ValidationResult(argv, "unavailable", None, int((monotonic() - started) * 1000), stderr=str(error))

    stdout, stderr = _BoundedCapture(), _BoundedCapture()
    readers = _start_readers(running.process, stdout, stderr)
    status = "passed"
    exit_code: int | None = None
    try:
        exit_code = running.process.wait(timeout=timeout_seconds)
        status = "passed" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        status = "timed_out"
        _terminate_process_tree(running)
    finally:
        _finish_readers(running, readers)

    return ValidationResult(
        argv,
        status,
        exit_code,
        int((monotonic() - started) * 1000),
        stdout.render(),
        stderr.render(),
    )


def _start_process(argv: ValidationArgv, project: Path) -> _RunningProcess:
    kwargs: dict[str, object] = {
        "cwd": project,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # A separate process group makes CTRL_BREAK available as a graceful
        # fallback. taskkill /T below is still the authoritative tree cleanup.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    if os.name != "nt":
        return _RunningProcess(process)
    try:
        return _RunningProcess(process, _WindowsJob.assign(process))
    except OSError:
        # Some managed hosts disallow assigning nested jobs. taskkill remains
        # available as the standard-system fallback for those environments.
        return _RunningProcess(process)


def _start_readers(
    process: subprocess.Popen[bytes], stdout: _BoundedCapture, stderr: _BoundedCapture
) -> tuple[Thread, Thread]:
    assert process.stdout is not None
    assert process.stderr is not None
    output_thread = Thread(target=_drain, args=(process.stdout, stdout), daemon=True)
    error_thread = Thread(target=_drain, args=(process.stderr, stderr), daemon=True)
    output_thread.start()
    error_thread.start()
    return output_thread, error_thread


def _drain(stream: object, capture: _BoundedCapture) -> None:
    # Binary reads avoid platform text transcoding and let the capture cap apply
    # to the memory actually retained.
    while True:
        data = stream.read(_READ_CHUNK_BYTES)  # type: ignore[attr-defined]
        if not data:
            return
        capture.append(data)


def _finish_readers(running: _RunningProcess, readers: tuple[Thread, Thread]) -> None:
    # A descendant intentionally detached from the group can retain a pipe.
    # Never let that keep the coordinator blocked; normal group termination
    # closes both streams before this short join expires.
    for reader in readers:
        reader.join(timeout=1)
    process = running.process
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if running.windows_job is not None:
        running.windows_job.close()


def _terminate_process_tree(running: _RunningProcess) -> None:
    """Terminate a timed-out command and ordinary descendants before returning."""
    process = running.process
    if process.poll() is not None:
        return
    if os.name != "nt":
        _terminate_posix_group(process)
        return
    _terminate_windows_tree(process, running.windows_job)


def _terminate_posix_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)


def _terminate_windows_tree(process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    # taskkill is the standard Windows facility that walks the descendant tree.
    # It is invoked with an argv list (never a shell) and has a bounded wait.
    if job is not None:
        job.close()
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_TERMINATION_GRACE_SECONDS + 3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        # A minimal fallback if taskkill is unavailable or interrupted.
        pass
    if process.poll() is None:
        process.kill()
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
