"""Previewable, bounded execution of canonical project validation argv values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from time import monotonic
from collections.abc import Callable
from typing import Iterable, Sequence
from uuid import uuid4

from .processes import ArgvProcessRunner, ProcessError, posix_group_has_no_live_members, terminate_posix_group


# Validation keeps a failure prefix and drains the remaining pipes so a noisy
# command may still report its actual exit status.
MAX_CAPTURE_CHARS = 12_000
VALIDATION_REPORT_VERSION = 1


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
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv), "elapsed_ms": self.elapsed_ms,
            "exit_code": self.exit_code, "status": self.status,
            "stderr": self.stderr, "stdout": self.stdout,
        }


def parse_command(command: str) -> ValidationArgv:
    """Reject the former string command format without trying to parse it."""
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
    return tuple(ValidationResult(normalize_argv(command), "planned", None, 0) for command in commands)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_linklike(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _safe_report_path(project: Path, report_path: Path | str) -> Path:
    candidate = Path(report_path)
    if not candidate.is_absolute():
        candidate = project / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(project)
    except ValueError as error:
        raise ValidationError("validation report path must stay within the project root") from error
    current = project
    for part in relative.parts:
        current = current / part
        if _is_linklike(current):
            raise ValidationError("validation report path crosses a symbolic link or reparse point")
    if candidate.exists() and not candidate.is_file():
        raise ValidationError("validation report path is not a regular file")
    return candidate


def _atomic_json(path: Path, payload: dict[str, object], project: Path) -> None:
    path = _safe_report_path(project, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _safe_report_path(project, path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tasktra-validation-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_report_path(project, path)
        os.replace(temporary, path)
    except OSError as error:
        raise ValidationError(f"could not retain validation report: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _report_payload(report_id: str, started_at: str, results: Sequence[ValidationResult], *, complete: bool) -> dict[str, object]:
    has_truncated_output = any(item.stdout_truncated or item.stderr_truncated for item in results)
    if not complete:
        status = "running"
    elif not results:
        status = "partial"
    elif all(item.status == "passed" for item in results):
        status = "partial" if has_truncated_output else "passed"
    else:
        status = results[-1].status
    return {
        "kind": "tasktra.validation-report", "version": VALIDATION_REPORT_VERSION,
        "report_id": report_id, "status": status, "started_at": started_at,
        "finished_at": _utc_timestamp() if complete else None,
        "partial": has_truncated_output or status in {"partial", "running"},
        "checks": [
            {"index": index, "argv": list(item.argv), "status": item.status, "exit_code": item.exit_code,
             "elapsed_ms": item.elapsed_ms, "stdout": item.stdout, "stderr": item.stderr,
             "stdout_truncated": item.stdout_truncated, "stderr_truncated": item.stderr_truncated}
            for index, item in enumerate(results)
        ],
    }


def _write_report(project: Path, report_path: Path, report_id: str, started_at: str,
                  results: Sequence[ValidationResult], *, complete: bool) -> None:
    report = _report_payload(report_id, started_at, results, complete=complete)
    history = report_path.parent / "history" / f"{report_id}.json"
    _atomic_json(history, report, project)
    _atomic_json(report_path, report, project)


def run_validations(
    root: Path | str,
    commands: Iterable[Sequence[str]],
    *,
    timeout_seconds: int = 300,
    on_tick: Callable[[], object] | None = None,
    report_path: Path | str | None = None,
) -> tuple[ValidationResult, ...]:
    """Run direct argv commands in order, stopping at the first non-pass result.

    Supplying ``report_path`` opts into atomically retained local reports.  It
    writes the fixed latest path and one private per-run history file after
    each completed command, before a failed result is returned.
    """
    if timeout_seconds < 1:
        raise ValidationError("validation timeout must be positive")
    project = Path(root).resolve()
    if not project.is_dir():
        raise ValidationError(f"validation root is not a directory: {project}")
    planned = validation_plan(commands)
    destination = _safe_report_path(project, report_path) if report_path is not None else None
    report_id = uuid4().hex
    started_at = _utc_timestamp()
    results: list[ValidationResult] = []
    if destination is not None and not planned:
        _write_report(project, destination, report_id, started_at, results, complete=True)
    for index, item in enumerate(planned):
        results.append(_run_one(project, item.argv, timeout_seconds, on_tick=on_tick))
        if destination is not None:
            _write_report(project, destination, report_id, started_at, results,
                          complete=results[-1].status != "passed" or index == len(planned) - 1)
        if results[-1].status != "passed":
            break
    return tuple(results)


def _render_output(data: bytes, limited: bool) -> str:
    text = data.decode(errors="replace")
    # The shared runner deliberately does not retain an unbounded byte count;
    # expose the cap without pretending an exact omitted amount is known.
    return text if not limited else text[:MAX_CAPTURE_CHARS - 22] + "\n... bytes omitted"


def _run_one(
    project: Path, argv: ValidationArgv, timeout_seconds: int, *, on_tick: Callable[[], object] | None,
) -> ValidationResult:
    started = monotonic()
    try:
        result = ArgvProcessRunner(
            timeout=timeout_seconds, output_limit=MAX_CAPTURE_CHARS, terminate_on_output_limit=False,
        ).run(argv, cwd=project, env=None, on_tick=on_tick)
    except ProcessError as error:
        return ValidationResult(
            argv, "failed", None, int((monotonic() - started) * 1000), stderr=f"validation process cleanup failed: {error}",
        )
    except OSError as error:
        return ValidationResult(argv, "unavailable", None, int((monotonic() - started) * 1000), stderr=str(error))
    elapsed = int((monotonic() - started) * 1000)
    if not result.dispatched:
        return ValidationResult(argv, "unavailable", None, elapsed, stderr="validation command could not be launched")
    status = "timed_out" if result.timed_out else ("passed" if result.returncode == 0 else "failed")
    return ValidationResult(
        argv, status, result.returncode, elapsed,
        _render_output(result.stdout, result.stdout_limited),
        _render_output(result.stderr, result.stderr_limited),
        result.stdout_limited, result.stderr_limited,
    )


# These narrow wrappers retain the old test/import seam while the substantive
# ownership and Darwin zombie handling live in tasktra.processes.
def _posix_group_has_no_live_members(pgid: int) -> bool:
    return posix_group_has_no_live_members(pgid, run=subprocess.run)


def _terminate_posix_group(process: subprocess.Popen[bytes]) -> None:
    try:
        terminate_posix_group(process, probe=_posix_group_has_no_live_members)
    except ProcessError as error:
        raise ValidationError(str(error)) from error
