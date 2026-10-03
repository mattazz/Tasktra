"""Isolated Git workspaces for worker-owned changes.

Workers operate in a disposable clone.  Only a bounded, validated patch can
cross back to the coordinator checkout, and applying it is deliberately a
separate operation with a fresh baseline check.

The boundary deliberately transports Git-visible changes only.  Ignored files
are not evidence and disappear after ``refresh_from_patch``; work that needs
such an artifact must create it again from tracked inputs in the fresh clone.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
from typing import Any

from .processes import ArgvProcessRunner


MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_CHANGED_PATHS = 128
GIT_TIMEOUT_SECONDS = 60
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RESERVED_PREFIXES = (".git", ".tasktra/runtime")
_RESERVED_PATHS = frozenset({".tasktra/project.toml"})
_HOST_CONTROL_PREFIXES = frozenset({
    ".agents", ".claude", ".codex", ".continue", ".cursor", ".gemini", ".roo",
})
_HOST_CONTROL_FILES = frozenset({
    ".aider.conf.yml", ".cursorrules", ".mcp.json", ".windsurfrules",
    "copilot.md", "gemini.md",
})
_HOST_INSTRUCTION_FILES = frozenset({"agents.md", "agents.override.md", "claude.md"})


class RunWorkspaceError(ValueError):
    """A worker patch cannot be safely captured or published."""


def _is_link_or_reparse(path: Path) -> bool:
    """Return whether ``path`` is a symlink or Windows reparse point."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class RunWorkspace(AbstractContextManager["RunWorkspace"]):
    """A disposable clone that transports worker changes by validated patch.

    ``capture`` writes a content-addressed patch under
    ``.tasktra/runtime/runs/<attempt>/`` in the coordinator checkout.
    ``publish`` is the only method that changes
    tracked coordinator files, and it rechecks the original clean baseline
    immediately before Git applies the retained patch.
    """

    def __init__(self, root: Path | str, attempt_id: str) -> None:
        if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
            raise ValueError("attempt_id must be a short filesystem-safe identifier")
        supplied_root = Path(root).absolute()
        self._reject_link_ancestors(supplied_root, "workspace root")
        self.root = supplied_root.resolve(strict=True)
        if not self.root.is_dir():
            raise RunWorkspaceError("workspace root must be a directory")
        self.attempt_id = attempt_id
        self.path: Path | None = None
        self.patch_path: Path | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._baseline_head: str | None = None
        self._clone_count = 0
        self._captured: dict[str, Any] | None = None
        self._published = False

    @property
    def published(self) -> bool:
        """Whether this instance has completed a successful publish."""
        return self._published

    def __enter__(self) -> "RunWorkspace":
        if self.path is not None:
            raise RunWorkspaceError("run workspace is already active")
        self._require_clean_root()
        self._baseline_head = self._head(self.root)
        self._temporary = TemporaryDirectory(prefix="tasktra-run-")
        try:
            self.path = self._fresh_clone()
            return self
        except BaseException:
            self._temporary.cleanup()
            self._temporary = None
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
        self._temporary = None
        self.path = None

    def capture(self) -> dict[str, Any]:
        """Persist a bounded patch of permitted worker changes and describe it."""
        checkout = self._require_active()
        if self._head(checkout) != self._baseline_head:
            raise RunWorkspaceError("worker checkout HEAD changed since it was created")
        self._reject_worker_runtime(checkout)
        changed = self._changed_paths(checkout)
        if len(changed) > MAX_CHANGED_PATHS:
            raise RunWorkspaceError(f"worker patch changes more than {MAX_CHANGED_PATHS} paths")
        for relative in changed:
            self._validate_changed_path(checkout, relative)

        untracked = self._nul_paths(self._git(checkout, "ls-files", "--others", "--exclude-standard", "-z"))
        if untracked:
            for relative in untracked:
                self._validate_changed_path(checkout, relative)
            self._git(checkout, "add", "--intent-to-add", "--", *untracked)
            changed = self._changed_paths(checkout)
            if len(changed) > MAX_CHANGED_PATHS:
                raise RunWorkspaceError(f"worker patch changes more than {MAX_CHANGED_PATHS} paths")

        patch = self._git(
            checkout, "-c", "core.externalDiff=", "diff", "--binary", "--no-ext-diff",
            "--no-textconv", "--no-renames", "HEAD", "--", output_limit=MAX_PATCH_BYTES + 1,
        )
        if len(patch) > MAX_PATCH_BYTES:
            raise RunWorkspaceError(f"worker patch exceeds {MAX_PATCH_BYTES} bytes")
        patch_hash = sha256(patch).hexdigest()
        self.patch_path = self._write_patch(patch, patch_hash)
        self._captured = {"patch_sha256": patch_hash, "changed_paths": changed}
        return dict(self._captured)

    def refresh_from_patch(self) -> Path:
        """Use a fresh baseline clone containing only the retained patch.

        The prior worker checkout remains available until context cleanup, so
        this method never resets or deletes worker files.  Later validators
        and reviewers instead receive a new no-remote clone with the exact
        captured patch applied.
        """
        self._require_active()
        if self._captured is None or self.patch_path is None:
            raise RunWorkspaceError("capture must succeed before refresh_from_patch")
        self._require_clean_root()
        if self._head(self.root) != self._baseline_head:
            raise RunWorkspaceError("coordinator HEAD changed since worker checkout was created")
        patch = self._retained_patch()
        refreshed = self._fresh_clone()
        if patch:
            self._git(refreshed, "apply", "--check", "--binary", "--whitespace=nowarn", "--", str(self.patch_path))
            self._git(refreshed, "apply", "--binary", "--whitespace=nowarn", "--", str(self.patch_path))
        self.path = refreshed
        return refreshed

    def publish(self) -> dict[str, Any]:
        """Apply the retained patch only if the original baseline is unchanged."""
        if self._captured is None or self.patch_path is None:
            raise RunWorkspaceError("capture must succeed before publish")
        if self._published:
            return dict(self._captured)
        self._require_clean_root()
        if self._head(self.root) != self._baseline_head:
            raise RunWorkspaceError("coordinator HEAD changed since worker checkout was created")
        patch = self._retained_patch()
        if not patch:
            self._published = True
            return dict(self._captured)
        self._git(self.root, "apply", "--check", "--binary", "--whitespace=nowarn", "--", str(self.patch_path))
        self._git(self.root, "apply", "--binary", "--whitespace=nowarn", "--", str(self.patch_path))
        self._published = True
        return dict(self._captured)

    def _require_active(self) -> Path:
        if self.path is None or self._temporary is None:
            raise RunWorkspaceError("run workspace is not active")
        return self.path

    def _fresh_clone(self) -> Path:
        if self._temporary is None or self._baseline_head is None:
            raise RunWorkspaceError("run workspace is not active")
        destination = Path(self._temporary.name) / f"checkout-{self._clone_count}"
        self._clone_count += 1
        try:
            destination.resolve().relative_to(self.root)
        except ValueError:
            pass
        else:
            raise RunWorkspaceError("isolated checkout must be outside the workspace root")
        # --no-local plus --no-hardlinks forces object copies even for a
        # local source checkout.  The worker cannot mutate shared objects.
        self._git(self.root, "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(self.root), str(destination))
        self._git(destination, "remote", "remove", "origin")
        self._git(destination, "checkout", "--detach", self._baseline_head)
        if self._git(destination, "remote").strip():
            raise RunWorkspaceError("isolated checkout unexpectedly retains a remote")
        return destination

    def _require_clean_root(self) -> None:
        if self._git(self.root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
            raise RunWorkspaceError("workspace root must have a pristine working tree")

    def _head(self, directory: Path) -> str:
        head = self._git(directory, "rev-parse", "--verify", "HEAD").decode("ascii", "strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise RunWorkspaceError("workspace root must have a valid current HEAD")
        return head

    def _git(self, directory: Path, *arguments: str, output_limit: int = 64 * 1024) -> bytes:
        result = ArgvProcessRunner(timeout=GIT_TIMEOUT_SECONDS, output_limit=output_limit).run(
            ["git", *arguments], cwd=directory, env=None,
        )
        if not result.dispatched:
            raise RunWorkspaceError("Git is unavailable")
        if result.timed_out or result.cancelled or result.output_limited or result.input_uncertain:
            raise RunWorkspaceError("Git command did not complete with bounded evidence")
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise RunWorkspaceError(f"Git command failed: {' '.join(arguments[:2])}{': ' + detail if detail else ''}")
        return result.stdout

    @staticmethod
    def _nul_paths(value: bytes) -> list[str]:
        if not value:
            return []
        if not value.endswith(b"\0"):
            raise RunWorkspaceError("Git returned malformed NUL-delimited paths")
        return [os.fsdecode(item) for item in value[:-1].split(b"\0")]

    def _changed_paths(self, checkout: Path) -> list[str]:
        paths = self._nul_paths(self._git(
            checkout, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            "--no-renames", "HEAD", "--",
        ))
        return sorted(set(paths))

    def _reject_worker_runtime(self, checkout: Path) -> None:
        runtime = checkout / ".tasktra" / "runtime"
        if runtime.exists() or _is_link_or_reparse(runtime):
            raise RunWorkspaceError("worker changes may not include reserved path .tasktra/runtime")

    def _validate_changed_path(self, checkout: Path, relative: str) -> None:
        if not relative or "\0" in relative:
            raise RunWorkspaceError("worker patch contains an invalid path")
        candidate = Path(relative)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise RunWorkspaceError("worker patch contains an outside path")
        for part in candidate.parts:
            if ":" in part or "\\" in part or part.endswith((".", " ")) or any(ord(char) < 32 or ord(char) == 127 for char in part):
                raise RunWorkspaceError("worker patch contains a non-portable path")
        portable = candidate.as_posix()
        if portable in _RESERVED_PATHS or any(portable == prefix or portable.startswith(prefix + "/") for prefix in _RESERVED_PREFIXES):
            raise RunWorkspaceError(f"worker patch changes reserved path {portable}")
        self._validate_host_control_path(candidate, portable)
        current = checkout
        for part in candidate.parts:
            current = current / part
            if _is_link_or_reparse(current):
                raise RunWorkspaceError(f"worker patch crosses a symlink or reparse point: {portable}")
        self._reject_tracked_symlink(checkout, portable)

    @staticmethod
    def _validate_host_control_path(candidate: Path, portable: str) -> None:
        folded_parts = tuple(part.casefold() for part in candidate.parts)
        if candidate.name.casefold() in _HOST_INSTRUCTION_FILES:
            raise RunWorkspaceError(f"worker patch changes reserved host instruction {portable}")
        if folded_parts[0] in _HOST_CONTROL_PREFIXES:
            raise RunWorkspaceError(f"worker patch changes reserved host control path {portable}")
        if len(folded_parts) == 1 and folded_parts[0] in _HOST_CONTROL_FILES:
            raise RunWorkspaceError(f"worker patch changes reserved host control file {portable}")
        if folded_parts == (".github", "copilot-instructions.md"):
            raise RunWorkspaceError(f"worker patch changes reserved host control file {portable}")

    def _reject_tracked_symlink(self, checkout: Path, relative: str) -> None:
        index = self._git(checkout, "ls-files", "-s", "--", relative)
        for line in index.splitlines():
            if line.startswith(b"120000 "):
                raise RunWorkspaceError(f"worker patch changes symlink path {relative}")

    def _artifact_directory(self) -> Path:
        directory = self.root
        for part in (".tasktra", "runtime", "runs", self.attempt_id):
            directory = directory / part
            if directory.exists() or _is_link_or_reparse(directory):
                if _is_link_or_reparse(directory) or not directory.is_dir():
                    raise RunWorkspaceError("worker patch artifact path is unsafe")
            else:
                directory.mkdir()
        return directory

    def _write_patch(self, patch: bytes, patch_hash: str) -> Path:
        destination = self._artifact_directory() / f"changes-{patch_hash}.patch"
        if destination.exists() or _is_link_or_reparse(destination):
            self._validate_artifact_path(destination)
            try:
                if destination.read_bytes() != patch:
                    raise RunWorkspaceError("worker patch artifact hash collision or tampering detected")
            except OSError as error:
                raise RunWorkspaceError("could not read existing worker patch artifact") from error
            return destination
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(patch)
        except OSError as error:
            raise RunWorkspaceError("could not preserve worker patch artifact") from error
        self._validate_artifact_path(destination)
        return destination

    def _retained_patch(self) -> bytes:
        if self._captured is None or self.patch_path is None:
            raise RunWorkspaceError("capture must succeed before using a retained patch")
        self._validate_artifact_path(self.patch_path)
        try:
            if self.patch_path.stat().st_size > MAX_PATCH_BYTES:
                raise RunWorkspaceError("retained worker patch exceeds the capture limit")
            patch = self.patch_path.read_bytes()
        except OSError as error:
            raise RunWorkspaceError("retained worker patch is unavailable") from error
        if sha256(patch).hexdigest() != self._captured["patch_sha256"]:
            raise RunWorkspaceError("retained worker patch no longer matches captured evidence")
        return patch

    @staticmethod
    def _reject_link_ancestors(path: Path, label: str) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if _is_link_or_reparse(current):
                raise RunWorkspaceError(f"{label} crosses a symlink or reparse point")

    def _validate_artifact_path(self, destination: Path) -> None:
        try:
            relative = destination.relative_to(self.root)
        except ValueError as error:
            raise RunWorkspaceError("worker patch artifact is outside the workspace root") from error
        current = self.root
        for part in relative.parts:
            current = current / part
            if _is_link_or_reparse(current):
                raise RunWorkspaceError("worker patch artifact crosses a symlink or reparse point")
        try:
            if not destination.is_file():
                raise RunWorkspaceError("worker patch artifact is not a regular file")
        except OSError as error:
            raise RunWorkspaceError("worker patch artifact is unavailable") from error
