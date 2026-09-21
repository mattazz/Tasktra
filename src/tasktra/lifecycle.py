"""Pure preview planning for Tasktra adoption and upgrades.

This module is intentionally the seam between inspection and later, separately
authorized application.  It reads only bounded project metadata, emits stable
data, and never calls a compiler writer, migration, validator, or runtime
store constructor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping, Sequence

from . import __version__
from .adoption import inspect_existing_instructions
from .compiler import Catalog, CatalogError, compile_catalog
from .model_policy import CodexModelPolicy
from .config import ConfigError, ProjectConfig, config_path, load_project_config
from .ecosystem import preflight_packs, preview_pack_migrations
from .manifest import GeneratedManifest, ManifestError, TasktraLock, read_lockfile, read_manifest, sha256_bytes, sha256_text
from .state import SCHEMA_VERSION, StateError, StateStore


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_LOCK_PATH = PurePosixPath(".tasktra/tasktra.lock")
_MANIFEST_PATH = PurePosixPath(".tasktra/generated/manifest.json")
_DECLARED_MAJOR_COMPATIBILITY_EDGES = frozenset({("0.6.0", "1.0.0")})
_DECLARED_RUNTIME_COMPATIBILITY_EDGES = frozenset({(8, 10)})


class LifecycleError(ValueError):
    """Raised for invalid planner inputs before any filesystem observation."""


@dataclass(frozen=True)
class LifecyclePreview:
    """A serializable, no-write lifecycle preview."""

    action: str
    ok: bool
    plan: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {"action": self.action, "ok": self.ok, "mutation": "none", **self.plan}


def preview_adoption(
    root: Path | str,
    catalog: Catalog,
    *,
    enabled_packs: Iterable[str] = ("core",),
    available_capabilities: Iterable[str] = (),
    trusted_executable_packs: Iterable[str] = (),
    validation_commands: Iterable[Sequence[str]] = (),
    tasktra_version: str = __version__,
    runtime_schema_version: int = SCHEMA_VERSION,
    codex_model_policy: CodexModelPolicy | None = None,
    codex_role_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> LifecyclePreview:
    """Return a bounded adoption plan without treating instructions as equivalent.

    Existing instruction paths always remain project-owned in this preview.  A
    generated destination that overlaps one is a visible conflict, never a
    proposed replacement.
    """
    project = _project_root(root)
    inspection = inspect_existing_instructions(project)
    target = _target_projection(catalog, enabled_packs, codex_model_policy, codex_role_overrides)
    activation = preflight_packs(
        catalog, target.packs,
        available_capabilities=available_capabilities,
        trusted_executable_packs=trusted_executable_packs,
    )
    writes, conflicts = _managed_writes(project, target.files, None)
    profile = config_path(project)
    config_exists = profile.exists()
    if config_exists:
        writes.insert(0, _write_item(project, PurePosixPath(".tasktra/project.toml"), None, "preserve"))
    else:
        writes.insert(0, _write_item(project, PurePosixPath(".tasktra/project.toml"), None, "create"))
    project_owned = [item.as_dict() for item in inspection.instructions]
    commands = _validation_commands(validation_commands, activation)
    blockers = list(conflicts) + list(activation["blockers"])
    uncertainty = list(inspection.uncertain)
    if config_exists:
        uncertainty.append("Existing project configuration is preserved; its pack selection was not inferred.")
    return LifecyclePreview("adoption-preview", not blockers, {
        "root": str(project),
        "instruction_inspection": inspection.as_dict(),
        "managed_writes": writes,
        "project_owned": project_owned,
        "preserved_paths": [item["path"] for item in project_owned] + ([".tasktra/project.toml"] if config_exists else []),
        "conflicts": sorted(blockers, key=_stable_item_key),
        "capability_gaps": list(activation["blockers"]),
        "validation_commands": commands,
        "target": _target_state(catalog, target.packs, tasktra_version, runtime_schema_version),
        "uncertain": sorted(set(uncertainty)),
    })


def preview_upgrade(
    root: Path | str,
    catalog: Catalog,
    *,
    current_lock: TasktraLock | None = None,
    enabled_packs: Iterable[str] | None = None,
    available_capabilities: Iterable[str] = (),
    trusted_executable_packs: Iterable[str] = (),
    tasktra_version: str = __version__,
    runtime_schema_version: int = SCHEMA_VERSION,
    validation_commands: Iterable[Sequence[str]] | None = None,
    codex_model_policy: CodexModelPolicy | None = None,
    codex_role_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> LifecyclePreview:
    """Compose a deterministic, authority-scoped upgrade preview.

    The planner accepts only same-major Tasktra and catalog versions, an
    immediately preceding runtime schema, and exact declared pack edges.  It
    reports every unsupported edge as a blocker and never invokes it.
    """
    project = _project_root(root)
    blockers: list[dict[str, object]] = []
    lock = current_lock
    if lock is None:
        try:
            lock = read_lockfile(project)
        except FileNotFoundError:
            blockers.append(_blocker("lockfile", "missing lockfile; an upgrade needs installed versions"))
        except ManifestError as error:
            blockers.append(_blocker("lockfile", f"invalid lockfile: {error}"))
    selected = tuple(enabled_packs) if enabled_packs is not None else (lock.packs if lock is not None else ("core",))
    try:
        target = _target_projection(catalog, selected, codex_model_policy, codex_role_overrides)
        activation = preflight_packs(
            catalog, target.packs,
            available_capabilities=available_capabilities,
            trusted_executable_packs=trusted_executable_packs,
        )
    except CatalogError as error:
        return LifecyclePreview("upgrade-preview", False, {
            "root": str(project), "managed_writes": [], "migration_steps": [],
            "compatibility": [], "capability_gaps": [],
            "conflicts": [_blocker("target", str(error))], "validation_commands": [],
            "uncertain": [], "authority_scope": _authority_scope(),
        })

    manifest = _read_manifest(project, blockers)
    if lock is not None and manifest is not None:
        if manifest.digest != lock.generated_manifest_sha256:
            blockers.append(_blocker(
                "generated-manifest",
                "manifest digest does not match the installed lock binding",
            ))
        if (
            manifest.tasktra_version != lock.tasktra_version
            or manifest.catalog_version != lock.catalog_version
            or manifest.packs != lock.packs
        ):
            blockers.append(_blocker(
                "generated-manifest",
                "manifest versions or packs do not match the installed lock",
            ))
    writes, conflicts = _managed_writes(project, target.files, manifest)
    blockers.extend(conflicts)
    blockers.extend(activation["blockers"])
    compatibility: list[dict[str, object]] = []
    migrations: list[dict[str, object]] = []
    runtime_state_schema: int | None = None
    if lock is not None:
        compatibility.extend((
            _semver_edge("tasktra", lock.tasktra_version, tasktra_version),
            _semver_edge("catalog", lock.catalog_version, catalog.version),
        ))
        blockers.extend(item for item in compatibility if not item["supported"])
        locked_versions = dict(lock.pack_versions)
        migration_preview = preview_pack_migrations(
            catalog, locked_versions, target.packs,
            trusted_executable_packs=trusted_executable_packs,
        )
        blockers.extend(migration_preview["blockers"])
        migrations.extend(_pack_steps(migration_preview["changes"], catalog))
        blockers.extend(_removed_pack_blockers(lock, target.packs))
        blockers.extend(_contract_blockers(lock, catalog, target.packs))
        runtime_state_schema = _runtime_state_schema(project, blockers)
        locked_runtime = dict(lock.schema_versions).get("runtime")
        if runtime_state_schema is not None and runtime_state_schema != locked_runtime:
            blockers.append(_blocker(
                "runtime-state",
                "on-disk runtime schema does not match the installed lock",
                lock_schema=locked_runtime,
                on_disk_schema=runtime_state_schema,
            ))
        runtime_edge, runtime_step = _runtime_edge(lock, runtime_schema_version)
        compatibility.append(runtime_edge)
        if not runtime_edge["supported"]:
            blockers.append(runtime_edge)
        elif runtime_step is not None:
            migrations.insert(0, runtime_step)
    commands = _configured_validation_commands(project, validation_commands, activation, blockers)
    for index, step in enumerate(migrations, start=1):
        step["order"] = index
    current_state = _lock_state(lock)
    if current_state is not None:
        current_state["runtime_state_schema_version"] = runtime_state_schema
    return LifecyclePreview("upgrade-preview", not blockers, {
        "root": str(project),
        "current": current_state,
        "target": _target_state(catalog, target.packs, tasktra_version, runtime_schema_version),
        "compatibility": compatibility,
        "managed_writes": writes,
        "conflicts": sorted(blockers, key=_stable_item_key),
        "capability_gaps": list(activation["blockers"]),
        "migration_steps": migrations,
        "validation_commands": commands,
        "authority_scope": _authority_scope(),
        "uncertain": [],
    })


def _project_root(root: Path | str) -> Path:
    project = Path(root).resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {project}")
    return project


def _target_projection(
    catalog: Catalog, enabled_packs: Iterable[str], codex_model_policy: CodexModelPolicy | None = None,
    codex_role_overrides: Mapping[str, Mapping[str, str]] | None = None,
):
    selected = tuple(sorted(set(enabled_packs)))
    if not selected:
        raise LifecycleError("enabled_packs must not be empty")
    return compile_catalog(
        catalog, selected, codex_model_policy=codex_model_policy,
        codex_role_overrides=codex_role_overrides,
    )


def _target_state(catalog: Catalog, packs: Iterable[str], tasktra_version: str, runtime_schema_version: int) -> dict[str, object]:
    _parse_semver(tasktra_version)
    _parse_semver(catalog.version)
    if not isinstance(runtime_schema_version, int) or runtime_schema_version < 1:
        raise LifecycleError("runtime_schema_version must be a positive integer")
    return {
        "tasktra_version": tasktra_version,
        "catalog_version": catalog.version,
        "packs": [{"id": item, "version": catalog.packs[item].version, "sha256": catalog.packs[item].source_sha256} for item in packs],
        "runtime_schema_version": runtime_schema_version,
    }


def _managed_writes(project: Path, files: Mapping[PurePosixPath, str], manifest: GeneratedManifest | None) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    known_managed = {item.path for item in manifest.files} if manifest is not None else set()
    records = {item.path: item for item in manifest.files} if manifest is not None else {}
    writes: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    for relative, content in sorted(files.items(), key=lambda item: item[0].as_posix()):
        path = relative.as_posix()
        existing = project.joinpath(*relative.parts)
        if existing.exists() and path not in known_managed:
            writes.append(_write_item(project, relative, content, "conflict"))
            conflicts.append(_blocker("project-owned-path", "managed output would replace project-owned content", path=path))
        else:
            record = records.get(path)
            if existing.exists() and record is not None:
                if not existing.is_file():
                    conflicts.append(_blocker("managed-local-edit", "managed output is not a regular file", path=path))
                else:
                    actual = sha256_bytes(existing.read_bytes())
                    desired = sha256_text(content)
                    if actual not in {record.sha256, desired}:
                        conflicts.append(_blocker("managed-local-edit", "managed output has local edits", path=path))
            action = "update" if existing.exists() else "create"
            writes.append(_write_item(project, relative, content, action))
    desired_paths = {relative.as_posix() for relative in files}
    for path, record in sorted(records.items()):
        if path in desired_paths:
            continue
        relative = PurePosixPath(path)
        existing = project.joinpath(*relative.parts)
        if not existing.exists() and not existing.is_symlink():
            continue
        if existing.is_symlink() or not existing.is_file():
            writes.append({"path": path, "action": "conflict", "managed": True})
            conflicts.append(_blocker("managed-local-edit", "stale managed output is not a regular file", path=path))
            continue
        data = existing.read_bytes()
        actual = sha256_bytes(data)
        action = "delete" if actual == record.sha256 else "conflict"
        writes.append({"path": path, "action": action, "managed": True, "sha256": actual, "size": len(data)})
        if action == "conflict":
            conflicts.append(_blocker("managed-local-edit", "stale managed output has local edits", path=path))
    for relative in (_MANIFEST_PATH, _LOCK_PATH):
        existing = project.joinpath(*relative.parts)
        writes.append(_write_item(project, relative, None, "update" if existing.exists() else "create"))
    return writes, conflicts


def _write_item(project: Path, relative: PurePosixPath, content: str | None, action: str) -> dict[str, object]:
    value: dict[str, object] = {"path": relative.as_posix(), "action": action, "managed": action != "preserve"}
    if content is not None:
        value.update({"sha256": sha256_text(content), "size": len(content.encode("utf-8"))})
    return value


def _read_manifest(project: Path, blockers: list[dict[str, object]]) -> GeneratedManifest | None:
    try:
        return read_manifest(project)
    except FileNotFoundError:
        return None
    except ManifestError as error:
        blockers.append(_blocker("generated-manifest", f"invalid generated manifest: {error}"))
        return None


def _semver_edge(component: str, current: str, target: str) -> dict[str, object]:
    try:
        before, after = _parse_semver(current), _parse_semver(target)
    except LifecycleError as error:
        return {"component": component, "from": current, "to": target, "supported": False, "reason": str(error)}
    if before == after:
        reason, supported = "unchanged", True
    elif after < before:
        reason, supported = "downgrade is not supported", False
    elif before[0] != after[0] and (current, target) not in _DECLARED_MAJOR_COMPATIBILITY_EDGES:
        reason, supported = "major-version migration requires an explicit compatibility edge", False
    elif before[0] != after[0]:
        reason, supported = "declared Stage 7 compatibility edge", True
    else:
        reason, supported = "same-major upgrade", True
    return {"component": component, "from": current, "to": target, "supported": supported, "reason": reason}


def _runtime_edge(lock: TasktraLock, target: int) -> tuple[dict[str, object], dict[str, object] | None]:
    current = dict(lock.schema_versions).get("runtime")
    if current is None:
        return _blocker("runtime-schema", "current lockfile has no runtime schema version", supported=False), None
    if target == current:
        return {"component": "runtime-schema", "from": current, "to": target, "supported": True, "reason": "unchanged"}, None
    if target == current + 1 or (current, target) in _DECLARED_RUNTIME_COMPATIBILITY_EDGES:
        reason = (
            "immediately preceding schema is supported"
            if target == current + 1
            else "declared compound schema migration is supported"
        )
        return ({"component": "runtime-schema", "from": current, "to": target, "supported": True, "reason": reason}, {
            "kind": "runtime-schema", "from": current, "to": target,
            "effects": {"read_paths": [".tasktra/runtime"], "write_paths": [".tasktra/runtime"], "network": False},
            "checksum_binding": {"catalog_source_sha256": lock.catalog_source_sha256},
            "execution": "preview-only",
        })
    reason = "runtime schema downgrade is not supported" if target < current else "runtime migration supports only the immediately preceding schema"
    return {"component": "runtime-schema", "from": current, "to": target, "supported": False, "reason": reason}, None


def _pack_steps(changes: Sequence[Mapping[str, object]], catalog: Catalog) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for change in changes:
        pack = catalog.packs[str(change["pack"])]
        result.append({
            "kind": "pack", "pack": change["pack"], "from": change["from"], "to": change["to"],
            "migration_kind": change["kind"], "description": change["description"], "effects": change["effects"],
            "checksum_binding": {"pack": pack.identifier, "version": pack.version, "sha256": pack.source_sha256},
            "execution": "preview-only",
        })
    return result


def _removed_pack_blockers(lock: TasktraLock, target_packs: Iterable[str]) -> list[dict[str, object]]:
    target = set(target_packs)
    return [_blocker("pack", "pack removal requires an explicit compatibility edge", pack=item) for item in lock.packs if item not in target]


def _contract_blockers(lock: TasktraLock, catalog: Catalog, target_packs: Iterable[str]) -> list[dict[str, object]]:
    locked = {name: (version, contract, trust, digest) for name, version, contract, trust, digest in lock.pack_contracts}
    blockers: list[dict[str, object]] = []
    for identifier in target_packs:
        previous = locked.get(identifier)
        target = catalog.packs[identifier]
        if previous is None:
            continue
        version, contract, trust, digest = previous
        if version == target.version and (contract, trust, digest) != (target.contract_version, target.trust, target.source_sha256):
            blockers.append(_blocker("pack-contract", "pack checksum or contract changed without a version edge", pack=identifier))
    return blockers


def _configured_validation_commands(project: Path, explicit: Iterable[Sequence[str]] | None, activation: Mapping[str, object], blockers: list[dict[str, object]]) -> list[list[str]]:
    commands = explicit
    include_pack_defaults = True
    if commands is None:
        try:
            config = load_project_config(project)
            commands = config.validation_commands
            include_pack_defaults = config.include_pack_validation_defaults
        except FileNotFoundError:
            commands = ()
        except ConfigError as error:
            blockers.append(_blocker("project-config", f"invalid project configuration: {error}"))
            commands = ()
    return _validation_commands(commands, activation, include_pack_defaults=include_pack_defaults)


def _runtime_state_schema(project: Path, blockers: list[dict[str, object]]) -> int | None:
    try:
        config = load_project_config(project)
    except FileNotFoundError:
        config = ProjectConfig(name=project.name)
    except ConfigError as error:
        blockers.append(_blocker("project-config", f"invalid project configuration: {error}"))
        return None
    try:
        return StateStore(config.database_path(project)).inspect_schema_version()
    except (ConfigError, StateError) as error:
        blockers.append(_blocker("runtime-state", str(error)))
        return None


def _validation_commands(
    commands: Iterable[Sequence[str]], activation: Mapping[str, object], *,
    include_pack_defaults: bool = True,
) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    values = list(commands)
    if include_pack_defaults:
        values += [item["argv"] for item in activation["validation_defaults"]]
    for command in values:
        argv = tuple(command)
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise LifecycleError("validation commands must be non-empty argv arrays")
        if argv not in seen:
            seen.add(argv)
            result.append(list(argv))
    return result


def _lock_state(lock: TasktraLock | None) -> dict[str, object] | None:
    if lock is None:
        return None
    return {
        "tasktra_version": lock.tasktra_version,
        "catalog_version": lock.catalog_version,
        "packs": [{"id": name, "version": version} for name, version in lock.pack_versions],
        "runtime_schema_version": dict(lock.schema_versions).get("runtime"),
    }


def _authority_scope() -> dict[str, object]:
    return {
        "required_effect": "local-reversible-write",
        "required_action": "local-effect",
        "request_actions": ["upgrade-apply", "upgrade-rollback"],
        "preview_executes": False,
        "rollback_evidence_required": True,
    }


def _parse_semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise LifecycleError(f"semantic version must be MAJOR.MINOR.PATCH: {value!r}")
    return tuple(int(item) for item in match.groups())


def _blocker(component: str, reason: str, **details: object) -> dict[str, object]:
    return {"component": component, "reason": reason, **details}


def _stable_item_key(item: Mapping[str, object]) -> tuple[str, str, str]:
    return (str(item.get("component", "")), str(item.get("pack", item.get("path", ""))), str(item.get("reason", "")))
