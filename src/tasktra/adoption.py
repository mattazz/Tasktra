"""Read-only inspection and preview planning for existing projects.

Existing agent instructions are project-owned.  This module reports them with
content digests only; it neither reads their meaning into a generated file nor
offers an API that writes over them.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath

from .config import config_path, default_config_text
from .manifest import sha256_bytes


INSTRUCTION_PATHS = ("AGENTS.md", "CLAUDE.md", ".agents", ".codex", ".claude")
MAX_INSTRUCTION_FILES = 128
MAX_INSTRUCTION_DIRECTORIES = 64
MAX_INSTRUCTION_BYTES = 1024 * 1024


@dataclass(frozen=True, order=True)
class InstructionInventoryItem:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class InstructionInspection:
    """A bounded, metadata-only view of existing instruction surfaces.

    Digests identify bytes for later human review; this type deliberately does
    not derive instruction meaning or claim that two instruction files agree.
    """

    instructions: tuple[InstructionInventoryItem, ...]
    uncertain: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "instructions": [item.as_dict() for item in self.instructions],
            "uncertain": list(self.uncertain),
            "semantic_equivalence": "not-assessed",
            "mutation": "none",
        }


@dataclass(frozen=True)
class AdoptionPreview:
    root: Path
    project_name: str
    config_exists: bool
    config_content: str | None
    instructions: tuple[InstructionInventoryItem, ...]
    uncertain: tuple[str, ...] = ()

    @property
    def can_initialize(self) -> bool:
        return not self.config_exists

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "init-preview",
            "config": {
                "action": "create" if self.can_initialize else "preserve",
                "content": self.config_content,
                "path": str(config_path(self.root)),
            },
            "instructions": [item.as_dict() for item in self.instructions],
            "uncertain": list(self.uncertain),
            "semantic_equivalence": "not-assessed",
            "ok": True,
            "root": str(self.root),
            "writes": [],
        }


def inventory_existing_instructions(root: Path) -> tuple[InstructionInventoryItem, ...]:
    """List instruction files below the approved instruction roots.

    Symlinks are intentionally excluded: following one could make a preview
    depend on files outside the adopted project.  Unreadable files similarly do
    not become a reason to modify the project; callers receive the readable
    inventory and can surface filesystem errors separately if needed.
    """
    return inspect_existing_instructions(root).instructions


def inspect_existing_instructions(
    root: Path,
    *,
    max_files: int = MAX_INSTRUCTION_FILES,
    max_directories: int = MAX_INSTRUCTION_DIRECTORIES,
    max_bytes: int = MAX_INSTRUCTION_BYTES,
) -> InstructionInspection:
    """Inspect known instruction roots without following links or reading meaning.

    Limits are intentionally small and validated so a preview remains a local,
    deterministic observation even in a large adopted repository.  A skipped
    link, unreadable file, or exhausted limit is reported as uncertainty rather
    than being treated as evidence that no instruction exists.
    """
    if not isinstance(max_files, int) or not 1 <= max_files <= MAX_INSTRUCTION_FILES:
        raise ValueError(f"max_files must be between 1 and {MAX_INSTRUCTION_FILES}")
    if not isinstance(max_directories, int) or not 1 <= max_directories <= MAX_INSTRUCTION_DIRECTORIES:
        raise ValueError(f"max_directories must be between 1 and {MAX_INSTRUCTION_DIRECTORIES}")
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_INSTRUCTION_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_INSTRUCTION_BYTES}")
    project = root.resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {project}")

    discovered: list[InstructionInventoryItem] = []
    uncertain: list[str] = []
    pending: list[Path] = []
    for name in INSTRUCTION_PATHS:
        candidate = project / name
        if _linklike(candidate):
            uncertain.append(f"Skipped linked instruction path: {name}")
        elif candidate.is_file():
            if len(discovered) >= max_files:
                uncertain.append("Instruction inspection reached its file limit; unobserved files may exist.")
                continue
            _observe_file(project, candidate, discovered, uncertain, max_files, max_bytes)
        elif candidate.is_dir():
            pending.append(candidate)

    directories = 0
    while pending:
        directory = pending.pop(0)
        directories += 1
        if directories > max_directories:
            uncertain.append("Instruction inspection reached its directory limit; unobserved files may exist.")
            break
        try:
            children = sorted(
                (Path(entry.path) for entry in os.scandir(directory)),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as error:
            uncertain.append(f"Could not inspect instruction directory {directory.relative_to(project).as_posix()}: {error.__class__.__name__}")
            continue
        for child in children:
            if _linklike(child):
                uncertain.append(f"Skipped linked instruction path: {child.relative_to(project).as_posix()}")
            elif child.is_dir():
                pending.append(child)
            elif child.is_file():
                if len(discovered) >= max_files:
                    uncertain.append("Instruction inspection reached its file limit; unobserved files may exist.")
                    pending.clear()
                    break
                _observe_file(project, child, discovered, uncertain, max_files, max_bytes)
    return InstructionInspection(tuple(sorted(discovered)), tuple(sorted(set(uncertain))))


def preview_initialization(root: Path, *, name: str | None = None) -> AdoptionPreview:
    """Return a no-write adoption plan.

    The plan explicitly marks every existing instruction as preserved and never
    proposes generated instruction output during initial adoption.
    """
    resolved = root.resolve()
    destination = config_path(resolved)
    exists = destination.exists()
    # `init --apply` is allowed to target a new directory.  There is nothing
    # to inspect yet, so retain the no-write preview contract without creating
    # the directory merely to inventory it.
    inspection = (
        inspect_existing_instructions(resolved)
        if resolved.is_dir()
        else InstructionInspection((), ("Project directory does not exist yet; no existing instructions were observed.",))
    )
    return AdoptionPreview(
        root=resolved,
        project_name=name or resolved.name or "tasktra-project",
        config_exists=exists,
        config_content=None if exists else default_config_text(name or resolved.name),
        instructions=inspection.instructions,
        uncertain=inspection.uncertain,
    )


def _item(root: Path, path: Path) -> InstructionInventoryItem:
    raw = path.read_bytes()
    relative = PurePosixPath(path.relative_to(root).as_posix()).as_posix()
    return InstructionInventoryItem(relative, sha256_bytes(raw), len(raw))


def _observe_file(
    root: Path,
    path: Path,
    discovered: list[InstructionInventoryItem],
    uncertain: list[str],
    max_files: int,
    max_bytes: int,
) -> None:
    if len(discovered) >= max_files:
        return
    relative = path.relative_to(root).as_posix()
    try:
        size = path.stat().st_size
        if size > max_bytes:
            uncertain.append(f"Instruction file exceeds the {max_bytes}-byte observation limit: {relative}")
            return
        discovered.append(_item(root, path))
    except OSError as error:
        uncertain.append(f"Could not read instruction file {relative}: {error.__class__.__name__}")


def _linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())
