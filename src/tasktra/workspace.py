"""Read-only Git workspace inspection and conservative placement advice."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Literal


WorkspaceStrategy = Literal["existing-checkout", "isolated-worktree", "escalation"]
ChangeScope = Literal["small", "substantial"]


@dataclass(frozen=True)
class GitCommandResult:
    """The bounded result of one direct Git argv invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GitWorkspaceAssessment:
    """Facts observed without changing the workspace or Git state."""

    requested_root: Path
    git_available: bool
    is_repository: bool
    repository_root: Path | None = None
    branch: str | None = None
    detached: bool = False
    head_revision: str | None = None
    dirty: bool | None = None
    has_untracked: bool | None = None
    git_dir: Path | None = None
    common_git_dir: Path | None = None
    linked_worktree: bool | None = None
    worktree_identity: str | None = None
    diagnostics: tuple[str, ...] = ()

    @property
    def local_work_can_continue(self) -> bool:
        """Git capability is optional; its absence does not stop local work."""
        return True


@dataclass(frozen=True)
class WorkspaceRequest:
    """Bounded context used to recommend a place for one work unit."""

    read_only: bool = False
    change_scope: ChangeScope = "small"
    concurrent_workers: int = 1
    paths_known_disjoint: bool = False
    require_isolation: bool = False

    def __post_init__(self) -> None:
        if self.change_scope not in {"small", "substantial"}:
            raise ValueError("change_scope must be 'small' or 'substantial'")
        if self.concurrent_workers < 1:
            raise ValueError("concurrent_workers must be at least one")


@dataclass(frozen=True)
class WorkspaceRecommendation:
    strategy: WorkspaceStrategy
    reasons: tuple[str, ...]


def _run_git(root: Path, args: tuple[str, ...]) -> GitCommandResult | None:
    """Run Git directly. ``None`` means the Git executable is unavailable."""
    try:
        process = subprocess.run(
            ("git", "-C", str(root), *args),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError:
        return None
    return GitCommandResult(process.returncode, process.stdout, process.stderr)


def _output(result: GitCommandResult) -> str:
    return result.stdout.strip()


def _git_path(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve(strict=False)


def assess_workspace(root: Path | str) -> GitWorkspaceAssessment:
    """Inspect a directory's Git state using only read-only direct commands.

    Missing Git and non-repositories produce a useful assessment instead of an
    exception, because Tasktra's local workflows do not require Git.
    """
    requested = Path(root).expanduser()
    if not requested.is_dir():
        return GitWorkspaceAssessment(
            requested_root=requested,
            git_available=False,
            is_repository=False,
            diagnostics=(f"workspace is not a directory: {requested}",),
        )
    workspace = requested.resolve(strict=True)
    version = _run_git(workspace, ("--version",))
    if version is None:
        return GitWorkspaceAssessment(
            requested_root=workspace,
            git_available=False,
            is_repository=False,
            diagnostics=("Git executable is unavailable; local workflows remain available.",),
        )
    if version.returncode != 0:
        return GitWorkspaceAssessment(
            requested_root=workspace,
            git_available=False,
            is_repository=False,
            diagnostics=("Git could not be invoked; local workflows remain available.",),
        )

    top_level = _run_git(workspace, ("rev-parse", "--show-toplevel"))
    if top_level is None or top_level.returncode != 0 or not _output(top_level):
        return GitWorkspaceAssessment(
            requested_root=workspace,
            git_available=True,
            is_repository=False,
            diagnostics=("Directory is not inside a Git work tree; local workflows remain available.",),
        )
    repository_root = _git_path(workspace, _output(top_level))
    diagnostics: list[str] = []

    branch_result = _run_git(workspace, ("symbolic-ref", "--quiet", "--short", "HEAD"))
    branch = _output(branch_result) if branch_result is not None and branch_result.returncode == 0 else None
    detached = branch is None
    if detached:
        diagnostics.append("HEAD is detached; choose an integration branch before writing.")

    head_result = _run_git(workspace, ("rev-parse", "HEAD"))
    head_revision = _output(head_result) if head_result is not None and head_result.returncode == 0 else None
    if head_revision is None:
        diagnostics.append("HEAD revision is unavailable (the repository may be unborn).")

    status_result = _run_git(workspace, ("status", "--porcelain=v1", "--untracked-files=all", "-z"))
    dirty: bool | None = None
    has_untracked: bool | None = None
    if status_result is None or status_result.returncode != 0:
        diagnostics.append("Git status could not be read.")
    else:
        records = [record for record in status_result.stdout.split("\0") if record]
        dirty = bool(records)
        has_untracked = any(record.startswith("?? ") for record in records)

    git_dir_result = _run_git(workspace, ("rev-parse", "--path-format=absolute", "--git-dir"))
    common_dir_result = _run_git(workspace, ("rev-parse", "--path-format=absolute", "--git-common-dir"))
    git_dir = None
    common_git_dir = None
    linked_worktree: bool | None = None
    worktree_identity = None
    if git_dir_result is not None and common_dir_result is not None and git_dir_result.returncode == common_dir_result.returncode == 0:
        git_dir = _git_path(workspace, _output(git_dir_result))
        common_git_dir = _git_path(workspace, _output(common_dir_result))
        linked_worktree = git_dir != common_git_dir
        worktree_identity = git_dir.name if linked_worktree else "main"
    else:
        diagnostics.append("Git worktree identity is unavailable.")

    return GitWorkspaceAssessment(
        requested_root=workspace,
        git_available=True,
        is_repository=True,
        repository_root=repository_root,
        branch=branch,
        detached=detached,
        head_revision=head_revision,
        dirty=dirty,
        has_untracked=has_untracked,
        git_dir=git_dir,
        common_git_dir=common_git_dir,
        linked_worktree=linked_worktree,
        worktree_identity=worktree_identity,
        diagnostics=tuple(diagnostics),
    )


def recommend_workspace(
    assessment: GitWorkspaceAssessment, request: WorkspaceRequest,
) -> WorkspaceRecommendation:
    """Select conservatively without creating branches, worktrees, or files."""
    if request.read_only:
        return WorkspaceRecommendation(
            "existing-checkout",
            ("Read-only work can use the existing checkout without modifying Git state.",),
        )
    needs_isolation = (
        request.require_isolation
        or request.change_scope == "substantial"
        or request.concurrent_workers > 1
    )
    if not assessment.git_available or not assessment.is_repository:
        if needs_isolation:
            return WorkspaceRecommendation(
                "escalation",
                ("An isolated worktree requires an available Git repository.", "Local sequential work remains possible."),
            )
        return WorkspaceRecommendation(
            "existing-checkout",
            ("Git isolation is unavailable; use bounded local work and preserve unrelated files.",),
        )
    if assessment.detached:
        return WorkspaceRecommendation(
            "escalation",
            ("Writing from detached HEAD needs a human-selected integration branch.",),
        )
    if needs_isolation:
        reasons = ["The requested scope benefits from an isolated Git worktree."]
        if assessment.dirty:
            reasons.append("The existing checkout is dirty; isolation preserves unrelated changes.")
        return WorkspaceRecommendation("isolated-worktree", tuple(reasons))
    if assessment.dirty and not request.paths_known_disjoint:
        return WorkspaceRecommendation(
            "escalation",
            ("The checkout has unrelated or unknown changes; confirm path ownership before writing there.",),
        )
    return WorkspaceRecommendation(
        "existing-checkout",
        ("The change is small, sequential, and safe to perform in the current checkout.",),
    )
