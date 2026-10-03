"""Previewable, bounded execution of canonical project validation argv values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from time import monotonic
from collections.abc import Callable
from typing import Iterable, Sequence

from .processes import ArgvProcessRunner, ProcessError, posix_group_has_no_live_members, terminate_posix_group


# Validation keeps a failure prefix and drains the remaining pipes so a noisy
# command may still report its actual exit status.
MAX_CAPTURE_CHARS = 12_000


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


def run_validations(
    root: Path | str,
    commands: Iterable[Sequence[str]],
    *,
    timeout_seconds: int = 300,
    on_tick: Callable[[], object] | None = None,
) -> tuple[ValidationResult, ...]:
    """Run direct argv commands in order, stopping at the first non-pass result."""
    if timeout_seconds < 1:
        raise ValidationError("validation timeout must be positive")
    project = Path(root).resolve()
    if not project.is_dir():
        raise ValidationError(f"validation root is not a directory: {project}")
    results: list[ValidationResult] = []
    for item in validation_plan(commands):
        results.append(_run_one(project, item.argv, timeout_seconds, on_tick=on_tick))
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
