"""Bounded direct-argv process execution with owned descendant cleanup.

This is the one process boundary used by Tasktra command consumers.  It never
invokes a shell, owns a POSIX session or Windows Job before allowing work to
run, and treats a process group as the completion boundary rather than merely
the immediate child's exit code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_OUTPUT_LIMIT = 16 * 1024
_TERMINATION_GRACE_SECONDS = 2
_READ_CHUNK_BYTES = 4096


@dataclass(frozen=True)
class ProcessResult:
    """The bounded evidence retained from one dispatched argv command."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    output_limited: bool = False
    input_uncertain: bool = False
    dispatched: bool = True
    cancelled: bool = False
    stdout_limited: bool = False
    stderr_limited: bool = False


class ProcessError(RuntimeError):
    """The owned process boundary could not be cleaned up conclusively."""


class _WindowsJob:
    """A kill-on-close Job Object assigned while the root remains suspended."""

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
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
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
            if not kernel32.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            raise
        return cls(handle)

    def close(self) -> None:
        if self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(self._handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = None


def posix_group_has_no_live_members(pgid: int, *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    """Check a permission-denied POSIX group kill without trusting pipe EOF.

    macOS can return EPERM for a group containing only zombies.  ``ps`` is a
    separate observation and therefore lets that benign case complete while a
    live, inaccessible descendant remains a failure.
    """
    try:
        result = run(
            ["/bin/ps", "-A", "-o", "pgid=", "-o", "stat="],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False, timeout=_TERMINATION_GRACE_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1] or not fields[1][0].isalpha():
            return False
        if int(fields[0]) == pgid and not fields[1].startswith("Z"):
            return False
    return True


def terminate_posix_group(
    process: subprocess.Popen[bytes], *, probe: Callable[[int], bool] = posix_group_has_no_live_members
) -> None:
    """End every member of the session even when the root already exited."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as error:
            if not probe(process.pid):
                raise ProcessError("process group termination was denied; cleanup is unverified") from error
            # Darwin may report EPERM for zombie-only members.  Still issue
            # the later signal so normal and zombie-only paths share the same
            # bounded cleanup attempt.
            continue
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ProcessError("process root did not exit after process-group cleanup") from error


class ArgvProcessRunner:
    """Run a direct argv command with bounded IO and an owned process boundary.

    ``on_stdout_chunk`` and ``on_stderr_chunk`` receive retained bytes as they
    arrive.  ``on_tick`` is called periodically; returning ``False`` cancels
    the command.  Any callback exception terminates the owned tree and is
    re-raised after its pipes have been drained or closed.
    """

    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, output_limit: int = DEFAULT_OUTPUT_LIMIT,
        terminate_on_output_limit: bool = True,
    ) -> None:
        if timeout <= 0:
            raise ValueError("process timeout must be positive")
        if output_limit < 0:
            raise ValueError("process output limit must not be negative")
        self.timeout = timeout
        self.output_limit = output_limit
        self.terminate_on_output_limit = terminate_on_output_limit

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str,
        env: Mapping[str, str] | None = None,
        stdin: bytes = b"",
        on_stdout_chunk: Callable[[bytes], object] | None = None,
        on_stderr_chunk: Callable[[bytes], object] | None = None,
        on_tick: Callable[[], object] | None = None,
        tick_interval: float = 0.05,
    ) -> ProcessResult:
        if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
            raise ValueError("command must be a non-empty argv array of non-empty strings")
        if not isinstance(stdin, bytes):
            raise ValueError("process stdin must be bytes")
        if tick_interval <= 0:
            raise ValueError("process tick interval must be positive")
        startup: dict[str, Any] = {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "cwd": Path(cwd), "env": None if env is None else dict(env), "shell": False, "bufsize": 0,
        }
        if os.name == "nt":
            startup["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        else:
            startup["start_new_session"] = True
        try:
            process = subprocess.Popen(list(argv), **startup)
        except OSError:
            return ProcessResult(127, dispatched=False)

        job: _WindowsJob | None = None
        if os.name == "nt":
            try:
                job = _WindowsJob.assign(process)
                self._resume_windows_process(process)
            except OSError:
                if job is not None:
                    try:
                        job.close()
                    except OSError as close_error:
                        job = None
                        self._terminate_windows_tree(process)
                        raise ProcessError("Windows Job close failed; cleanup is unverified") from close_error
                    job = None
                self._close_pipes(process)
                try:
                    process.kill()
                    process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return ProcessResult(127, dispatched=False)

        stdout, stderr = bytearray(), bytearray()
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()
        input_uncertain = threading.Event()
        writer_failed = threading.Event()
        stop = threading.Event()
        terminate_lock = threading.Lock()
        callback_error: list[BaseException] = []
        callback_lock = threading.Lock()

        def remember_callback_error(error: BaseException) -> None:
            with callback_lock:
                if not callback_error:
                    callback_error.append(error)

        def terminate() -> None:
            nonlocal job
            with terminate_lock:
                if stop.is_set():
                    return
                stop.set()
                if job is not None:
                    owned_job, job = job, None
                    try:
                        owned_job.close()
                    except OSError as error:
                        # A failed close cannot be treated as kill-on-close.
                        # Try the bounded system tree fallback, then expose the
                        # uncertain cleanup to the caller.
                        self._terminate_windows_tree(process)
                        raise ProcessError("Windows Job close failed; cleanup is unverified") from error
                elif os.name != "nt":
                    terminate_posix_group(process)
                else:
                    self._terminate_windows_tree(process)

        def reader(
            pipe: Any, sink: bytearray, callback: Callable[[bytes], object] | None, overflow: threading.Event,
        ) -> None:
            try:
                # Continue until EOF after normal-parent cleanup.  The parent
                # may have exited while kernel pipe buffers still contain its
                # final output, and completion must drain that bounded tail.
                while True:
                    remaining = self.output_limit - len(sink)
                    read_size = min(_READ_CHUNK_BYTES, max(1, remaining + 1)) if self.terminate_on_output_limit else _READ_CHUNK_BYTES
                    data = pipe.read(read_size)
                    if not data:
                        return
                    retained = data[:max(0, remaining)]
                    if retained:
                        sink.extend(retained)
                        if callback is not None:
                            try:
                                callback(retained)
                            except BaseException as error:
                                remember_callback_error(error)
                                terminate()
                                return
                    if len(data) > remaining:
                        overflow.set()
                        if self.terminate_on_output_limit:
                            terminate()
                            return
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
                terminate()
            finally:
                try:
                    assert process.stdin is not None
                    process.stdin.close()
                except OSError:
                    pass

        readers: list[threading.Thread] = []
        input_writer: threading.Thread | None = None
        timed_out = False
        cancelled = False
        returncode = 127
        try:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            readers = [
                threading.Thread(target=reader, args=(process.stdout, stdout, on_stdout_chunk, stdout_overflow), daemon=True),
                threading.Thread(target=reader, args=(process.stderr, stderr, on_stderr_chunk, stderr_overflow), daemon=True),
            ]
            input_writer = threading.Thread(target=writer, daemon=True)
            for thread in readers:
                thread.start()
            input_writer.start()
            deadline = time.monotonic() + self.timeout
            next_tick = time.monotonic() + tick_interval
            while process.poll() is None and not stop.is_set():
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    terminate()
                    break
                if on_tick is not None and now >= next_tick:
                    try:
                        if on_tick() is False:
                            cancelled = True
                            terminate()
                            break
                    except BaseException as error:
                        remember_callback_error(error)
                        terminate()
                        break
                    next_tick = now + tick_interval
                time.sleep(min(0.01, max(0.0, deadline - now)))
            if process.poll() is None:
                try:
                    returncode = process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self._force_cleanup(process, job)
                    job = None
                    returncode = process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            else:
                returncode = process.returncode

            # Completion means no process in the owned boundary remains.  This
            # is required even after a clean parent exit because descendants
            # may retain pipe handles and otherwise outlive the operation.
            if not stop.is_set():
                terminate()
            if input_writer is not None:
                input_writer.join(timeout=_TERMINATION_GRACE_SECONDS)
                if input_writer.is_alive():
                    input_uncertain.set()
                    terminate()
            for thread in readers:
                thread.join(timeout=_TERMINATION_GRACE_SECONDS)
            if any(thread.is_alive() for thread in readers):
                raise ProcessError("descendants retained output pipes; cleanup is unverified")
            if writer_failed.is_set() and returncode == 0:
                returncode = 127
            if callback_error:
                raise callback_error[0]
            return ProcessResult(
                returncode, bytes(stdout), bytes(stderr), timed_out,
                stdout_overflow.is_set() or stderr_overflow.is_set(), input_uncertain.is_set(), True, cancelled,
                stdout_overflow.is_set(), stderr_overflow.is_set(),
            )
        except BaseException:
            try:
                terminate()
            finally:
                self._close_pipes(process)
            raise
        finally:
            if job is not None:
                job.close()

    @staticmethod
    def _close_pipes(process: subprocess.Popen[bytes]) -> None:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    @staticmethod
    def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(wintypes.HANDLE(process._handle)))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xffffffff:08x}")

    @staticmethod
    def _terminate_windows_tree(process: subprocess.Popen[bytes]) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=_TERMINATION_GRACE_SECONDS + 3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    @classmethod
    def _force_cleanup(cls, process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
        if os.name == "nt":
            if job is not None:
                try:
                    job.close()
                except OSError as error:
                    cls._terminate_windows_tree(process)
                    raise ProcessError("Windows Job close failed; cleanup is unverified") from error
            cls._terminate_windows_tree(process)
        else:
            terminate_posix_group(process)
