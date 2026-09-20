"""Read-only planning for Tasktra ecosystem packs.

The interface deliberately returns data and never writes.  Detection, capability
preflight, specialist routing, and migration planning therefore stay previewable
and cannot silently change a project or grant execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path
import stat
from typing import Iterable, Mapping, Sequence

from .compiler import Catalog, CatalogError, Pack, resolve_packs

MAX_DETECTION_FILES = 256
MAX_DETECTION_DEPTH = 4
MAX_DETECTION_EVIDENCE = 64
MAX_EVIDENCE_PER_PACK = 16
MAX_DETECTION_DIRECTORIES = 64
MAX_DIRECTORY_ENTRIES = 256


@dataclass(frozen=True)
class DetectionEvidence:
    pack: str
    path: str
    signal: str


@dataclass(frozen=True)
class RecommendationReport:
    recommended: tuple[str, ...]
    evidence: tuple[DetectionEvidence, ...]
    files_observed: int
    truncated: bool
    uncertain: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "recommended": list(self.recommended),
            "evidence": [item.__dict__ for item in self.evidence],
            "observations": {
                "files_observed": self.files_observed,
                "evidence_count": len(self.evidence),
                "retry_count": 0,
                "truncated": self.truncated,
            },
            "uncertain": list(self.uncertain),
            "mutation": "none",
        }


def recommend_packs(
    root: Path | str,
    catalog: Catalog,
    *,
    max_files: int = MAX_DETECTION_FILES,
    max_depth: int = MAX_DETECTION_DEPTH,
) -> RecommendationReport:
    """Inspect bounded local names and return deterministic pack recommendations."""
    if not isinstance(max_files, int) or not 1 <= max_files <= MAX_DETECTION_FILES:
        raise CatalogError(f"max_files must be between 1 and {MAX_DETECTION_FILES}")
    if not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DETECTION_DEPTH:
        raise CatalogError(f"max_depth must be between 1 and {MAX_DETECTION_DEPTH}")
    project = Path(root).resolve()
    paths, truncated = _bounded_files(project, max_files=max_files, max_depth=max_depth)
    evidence: list[DetectionEvidence] = []
    matched: list[str] = []
    evidence_limited = False
    for identifier in sorted(catalog.packs):
        pack = catalog.packs[identifier]
        found: list[DetectionEvidence] = []
        for marker in pack.detection_markers:
            if _safe_existing_marker(project, marker):
                found.append(DetectionEvidence(identifier, marker, "marker"))
        for path in paths:
            relative = path.relative_to(project).as_posix()
            if relative in pack.detection_markers:
                continue
            matching = next((pattern for pattern in sorted(pack.detection_globs) if fnmatchcase(relative, pattern)), None)
            if matching is not None:
                found.append(DetectionEvidence(identifier, relative, f"glob:{matching}"))
        if found:
            matched.append(identifier)
            remaining = MAX_DETECTION_EVIDENCE - len(evidence)
            selected_evidence = found[: min(MAX_EVIDENCE_PER_PACK, max(remaining, 0))]
            evidence.extend(selected_evidence)
            evidence_limited = evidence_limited or len(selected_evidence) < len(found)
    requested = tuple(sorted(set(matched))) or (("generic",) if "generic" in catalog.packs else ("core",))
    resolve_packs(catalog, requested)
    recommendations = tuple(item for item in requested if item != "core")
    uncertain: list[str] = []
    if truncated:
        uncertain.append("Detection reached its file limit; unobserved evidence may change recommendations.")
    if evidence_limited:
        uncertain.append("Detection evidence was compacted to bounded representative matches.")
    if not evidence:
        uncertain.append("No ecosystem marker matched; the generic local pack is recommended.")
    return RecommendationReport(
        recommendations,
        tuple(sorted(set(evidence), key=lambda item: (item.pack, item.path, item.signal))),
        len(paths),
        truncated,
        tuple(uncertain),
    )


def _bounded_files(root: Path, *, max_files: int, max_depth: int) -> tuple[tuple[Path, ...], bool]:
    if not root.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {root}")
    found: list[Path] = []
    truncated = False
    pending = [root]
    inspected_directories = 0
    ignored = {".git", ".tasktra", ".agents", ".codex", ".claude", "node_modules", ".venv", "venv"}
    while pending:
        directory = pending.pop(0)
        inspected_directories += 1
        if inspected_directories > MAX_DETECTION_DIRECTORIES:
            return tuple(found), True
        depth = len(directory.relative_to(root).parts)
        children, directory_truncated = _bounded_children(directory)
        truncated = truncated or directory_truncated
        if not children:
            continue
        for child in children:
            if _is_linklike(child):
                continue
            if child.is_dir():
                if child.name not in ignored and depth < max_depth:
                    pending.append(child)
                continue
            if child.is_file():
                if len(found) == max_files:
                    truncated = True
                    return tuple(found), truncated
                found.append(child)
    return tuple(found), truncated


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or bool(is_junction and is_junction()):
        return True
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _bounded_children(directory: Path) -> tuple[tuple[Path, ...], bool]:
    children: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(children) == MAX_DIRECTORY_ENTRIES:
                    return (), True
                children.append(Path(entry.path))
    except OSError:
        return (), False
    return tuple(sorted(children, key=lambda path: path.name.casefold())), False


def _safe_existing_marker(root: Path, marker: str) -> bool:
    cursor = root
    for part in Path(marker).parts:
        cursor = cursor / part
        if _is_linklike(cursor):
            return False
    return cursor.is_file()


def preflight_packs(
    catalog: Catalog,
    enabled_packs: Iterable[str],
    *,
    available_capabilities: Iterable[str] = (),
    trusted_executable_packs: Iterable[str] = (),
) -> dict:
    """Report activation blockers and optional degradation without mutating state."""
    resolved = resolve_packs(catalog, enabled_packs)
    available = frozenset(available_capabilities)
    trusted = frozenset(trusted_executable_packs)
    blockers: list[dict[str, str]] = []
    optional_missing: list[dict[str, str]] = []
    validation_defaults: list[dict[str, object]] = []
    for identifier in resolved:
        pack = catalog.packs[identifier]
        for capability in pack.required_capabilities:
            if capability not in available:
                blockers.append({"pack": identifier, "capability": capability})
        for capability in pack.optional_capabilities:
            if capability not in available:
                optional_missing.append({"pack": identifier, "capability": capability})
        if pack.trust == "third-party-executable" and _pack_trust_token(pack) not in trusted:
            blockers.append({
                "pack": identifier,
                "capability": "checksum-bound-executable-pack-trust",
                "required_trust": _pack_trust_token(pack),
            })
        for command in pack.validation_commands:
            validation_defaults.append({"pack": identifier, "argv": list(command)})
    return {
        "ok": not blockers,
        "packs": list(resolved),
        "blockers": blockers,
        "optional_missing": optional_missing,
        "validation_defaults": validation_defaults,
        "unrelated_local_work_blocked": False,
    }


def preview_pack_migrations(
    catalog: Catalog,
    locked_versions: Mapping[str, str],
    enabled_packs: Iterable[str],
    *,
    trusted_executable_packs: Iterable[str] = (),
) -> dict:
    """Validate an exact declarative migration edge for every changed pack."""
    resolved = resolve_packs(catalog, enabled_packs)
    changes: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    trusted = frozenset(trusted_executable_packs)
    for identifier in resolved:
        target = catalog.packs[identifier]
        before = locked_versions.get(identifier)
        if before is None or before == target.version:
            continue
        matches = [item for item in target.migrations if item.from_version == before and item.to_version == target.version]
        if len(matches) != 1:
            blockers.append({"pack": identifier, "from": before, "to": target.version, "reason": "exact migration declaration required"})
            continue
        migration = matches[0]
        if migration.kind == "executable" and (
            target.trust != "third-party-executable" or _pack_trust_token(target) not in trusted
        ):
            blockers.append({"pack": identifier, "from": before, "to": target.version, "reason": "executable migration requires explicit executable-pack trust"})
            continue
        changes.append({
            "pack": identifier,
            "from": before,
            "to": target.version,
            "kind": migration.kind,
            "description": migration.description,
            "effects": {
                "argv": list(migration.argv),
                "network": migration.network,
                "read_paths": list(migration.read_paths),
                "write_paths": list(migration.write_paths),
            },
        })
    return {"ok": not blockers, "changes": changes, "blockers": blockers, "mutation": "none"}


_SPECIALIST_SIGNALS = {
    "architecture": "architecture-reviewer",
    "application": "application-implementer",
    "frontend": "frontend-specialist",
    "backend-api": "backend-api-specialist",
    "data-migration": "data-migration-specialist",
    "integration": "integration-specialist",
    "test-automation": "test-automation-specialist",
    "end-to-end": "end-to-end-evaluator",
    "reliability-observability": "reliability-observability-specialist",
    "developer-experience": "developer-experience-specialist",
    "refactoring": "refactoring-specialist",
    "product": "product-analyst",
    "delivery": "work-selector",
    "operations": "deployment-reviewer",
    "knowledge": "documentation-curator",
}


def route_specialists(
    catalog: Catalog,
    enabled_packs: Iterable[str],
    signals: Sequence[str],
    *,
    require_implementation_validation: bool = False,
    require_independent_review: bool = False,
) -> dict:
    """Select the smallest deterministic specialist set from explicit signals."""
    unknown = sorted(set(signals) - set(_SPECIALIST_SIGNALS))
    if unknown:
        raise CatalogError("unknown specialist signals: " + ", ".join(unknown))
    roles: list[str] = []
    for signal in sorted(set(signals)):
        role = _SPECIALIST_SIGNALS[signal]
        if role not in roles:
            roles.append(role)
    resolved = resolve_packs(catalog, enabled_packs)
    available_roles = {role for pack_id in resolved for role in catalog.packs[pack_id].roles}
    unavailable = sorted(set(roles) - available_roles)
    if unavailable:
        raise CatalogError("specialist roles are not enabled by the active profile: " + ", ".join(unavailable))
    if require_implementation_validation and "tester" not in roles:
        roles.append("tester")
    if require_independent_review and "reviewer" not in roles:
        roles.append("reviewer")
    return {
        "roles": roles,
        "signals": sorted(set(signals)),
        "separation": {
            "tester_independent": "tester" in roles,
            "reviewer_independent": "reviewer" in roles,
        },
        "observation": {"signal_count": len(set(signals)), "selected_role_count": len(roles), "retry_count": 0},
    }


def _pack_trust_token(pack: Pack) -> str:
    return f"{pack.identifier}@{pack.version}:{pack.source_sha256}"


def plan_monorepo_scopes(
    root: Path | str,
    changed_paths: Sequence[str],
    *,
    max_packages: int = 64,
) -> dict:
    """Plan bounded per-package validation without scanning unrelated trees."""
    if not isinstance(max_packages, int) or not 1 <= max_packages <= 64:
        raise CatalogError("max_packages must be between 1 and 64")
    project = Path(root).resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {project}")
    packages: list[dict] = []
    truncated = False
    for container in ("apps", "packages", "services"):
        parent = project / container
        if not parent.is_dir() or _is_linklike(parent):
            continue
        children, container_truncated = _bounded_children(parent)
        truncated = truncated or container_truncated
        if container_truncated:
            continue
        for child in children:
            if _is_linklike(child) or not child.is_dir():
                continue
            manifests = [name for name in ("package.json", "pyproject.toml") if (child / name).is_file() and not _is_linklike(child / name)]
            if not manifests:
                continue
            if len(packages) == max_packages:
                truncated = True
                break
            relative = child.relative_to(project).as_posix()
            commands: list[list[str]] = []
            if "package.json" in manifests:
                commands.append(["npm", "test"])
            if "pyproject.toml" in manifests:
                commands.append(["python", "-m", "unittest", "discover"])
            packages.append({"path": relative, "manifests": manifests, "validation_commands": commands})
        if truncated:
            break
    normalized_changes: list[str] = []
    for raw in changed_paths:
        if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
            raise CatalogError(f"changed path must be a safe relative POSIX path: {raw!r}")
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CatalogError(f"changed path must be a safe relative POSIX path: {raw!r}")
        normalized_changes.append(raw)
    affected = [
        package
        for package in packages
        if any(change == package["path"] or change.startswith(package["path"] + "/") for change in normalized_changes)
    ]
    root_level_changes = [
        change for change in normalized_changes
        if not any(change == package["path"] or change.startswith(package["path"] + "/") for package in packages)
    ]
    if root_level_changes:
        affected = packages
    return {
        "packages": packages,
        "affected": [package["path"] for package in affected],
        "validation_fanout": [
            {"package": package["path"], "commands": package["validation_commands"], "workspace": package["path"]}
            for package in affected
        ],
        "isolation": "per-package-workspace",
        "root_level_changes": root_level_changes,
        "truncated": truncated,
        "uncertain": truncated,
        "mutation": "none",
    }
