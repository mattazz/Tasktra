"""Authority-neutral application of exact, read-only lifecycle plans.

Callers must authorize the local effect before entering this module.  The
module binds the complete lifecycle preview to a digest, snapshots every
managed/runtime write, applies bounded pack migrations, regenerates canonical
projections, validates them, and rolls the snapshot back on any failure.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Mapping

from . import __version__
from .compiler import Catalog, check_drift, compile_catalog, write_projection
from .config import ConfigError, ProjectConfig
from .manifest import (
    GeneratedManifest,
    build_generated_manifest,
    build_lockfile,
    read_lockfile,
    read_manifest,
    sha256_bytes,
    write_lockfile,
    write_manifest,
)
from .migrations import (
    MigrationExecution,
    apply_migration_plan,
    migration_plan_digest,
    rollback_migration,
)
from .state import StateStore
from .validation import run_validations


class UpgradeError(ValueError):
    """Raised when an upgrade cannot remain exact and recoverable."""


def upgrade_plan_digest(plan: Mapping[str, object]) -> str:
    """Bind the entire lifecycle preview, not only executable steps."""
    normalized = _digestable_plan(plan)
    return sha256(_canonical(normalized)).hexdigest()


def apply_upgrade(
    root: Path | str,
    catalog_root: Path | str,
    catalog: Catalog,
    config: ProjectConfig,
    plan: Mapping[str, object],
    *,
    expected_plan_sha256: str,
    confirmed: bool,
    allow_network: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, object]:
    """Apply an exact lifecycle plan with migration-backed rollback."""
    project = Path(root).resolve()
    normalized = _validated_plan(plan)
    runtime_from, runtime_to = _runtime_transition(normalized)
    _verify_installed_binding(project, config, runtime_from)
    actual_plan_sha256 = sha256(_canonical(normalized)).hexdigest()
    if expected_plan_sha256 != actual_plan_sha256:
        raise UpgradeError("upgrade plan digest changed; preview again before applying")
    if not confirmed:
        raise UpgradeError("upgrade apply requires explicit confirmation")

    selected = tuple(str(item["id"]) for item in normalized["target"]["packs"])
    projection = compile_catalog(catalog, selected)
    prior_manifest = _optional_manifest(project)
    allowed_deletes = {
        str(item["path"])
        for item in normalized["managed_writes"]
        if isinstance(item, Mapping) and item.get("action") == "delete"
    }
    snapshot_paths = _snapshot_paths(project, normalized, projection.files, prior_manifest)
    migration_preview = _migration_preview(normalized, snapshot_paths)
    migration_sha256 = migration_plan_digest(migration_preview)

    def verify() -> Mapping[str, object]:
        state = StateStore(config.database_path(project))
        runtime_before = state.inspect_schema_version()
        if runtime_before != runtime_from:
            raise UpgradeError(
                f"runtime schema changed after preview: expected {runtime_from}, found {runtime_before}"
            )
        _write_canonical_projection(
            project, catalog_root, catalog, config, projection, prior_manifest, allowed_deletes,
        )
        declared_commands = tuple(
            tuple(str(arg) for arg in command)
            for command in normalized["validation_commands"]
        )
        external_commands = tuple(
            command for command in declared_commands if not _is_compile_check(command)
        )
        validation_results = run_validations(
            project,
            external_commands,
            timeout_seconds=timeout_seconds,
        )
        failed = [item.as_dict() for item in validation_results if item.status != "passed"]
        if failed:
            raise UpgradeError(f"configured validation failed: {failed}")
        runtime_evidence = state.migrate_with_evidence()
        runtime_after = int(runtime_evidence["after_schema"])
        if int(runtime_evidence["before_schema"]) != runtime_from or runtime_after != runtime_to:
            raise UpgradeError(
                "runtime migration did not match the exact previewed transition: "
                f"expected {runtime_from}->{runtime_to}, got "
                f"{runtime_evidence['before_schema']}->{runtime_after}"
            )
        runtime_changed = runtime_after != runtime_before
        if runtime_changed:
            try:
                _write_canonical_projection(
                    project, catalog_root, catalog, config, projection, read_manifest(project), set(),
                )
            except Exception as error:
                raise UpgradeError(
                    "runtime schema migrated but lock finalization failed; use the exact retained "
                    f"database backup {runtime_evidence['backup_path']}: {error}"
                ) from error
        drift = check_drift(project, projection, managed_paths=set(projection.files))
        if not drift.clean:
            raise UpgradeError("post-upgrade projection drift remains")
        return {
            "ok": True,
            "compile_check": "clean",
            "validation": [
                *[item.as_dict() for item in validation_results],
                *(
                    [{"argv": list(command), "status": "passed", "source": "internal-compile-check"}
                     for command in declared_commands if _is_compile_check(command)]
                ),
            ],
            "project_owned_preserved": True,
            "runtime_schema_before": runtime_before,
            "runtime_schema_after": runtime_after,
            "runtime_schema_changed": runtime_changed,
            "runtime_backup_path": runtime_evidence["backup_path"],
            "runtime_backup_sha256": runtime_evidence["backup_sha256"],
        }

    execution = apply_migration_plan(
        project,
        catalog_root,
        migration_preview,
        expected_plan_sha256=migration_sha256,
        confirmed=True,
        allow_network=allow_network,
        timeout_seconds=timeout_seconds,
        verifier=verify,
    )
    return {
        "ok": True,
        "action": "upgrade-apply",
        "upgrade_plan_sha256": actual_plan_sha256,
        "migration": execution.as_dict(),
    }


def rollback_upgrade(
    root: Path | str,
    snapshot_plan_sha256: str,
    *,
    expected_before_sha256: str,
) -> dict[str, object]:
    receipt_path = Path(root).resolve() / ".tasktra" / "upgrades" / snapshot_plan_sha256 / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpgradeError("rollback requires the verified migration receipt") from error
    verification = receipt.get("verification") if isinstance(receipt, dict) else None
    if isinstance(verification, dict) and verification.get("runtime_schema_changed") is True:
        backup = verification.get("runtime_backup_path")
        digest = verification.get("runtime_backup_sha256")
        raise UpgradeError(
            "automatic rollback is unavailable after a runtime schema migration; "
            f"restore the exact retained database backup {backup} (sha256 {digest}) "
            "with explicit human recovery"
        )
    return rollback_migration(
        root,
        snapshot_plan_sha256,
        expected_before_sha256=expected_before_sha256,
    )


def _validated_plan(plan: Mapping[str, object]) -> dict[str, object]:
    value = _digestable_plan(plan)
    if value.get("ok") is not True or value.get("conflicts") != []:
        raise UpgradeError("blocked upgrade preview cannot be applied")
    return value


def _digestable_plan(plan: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(plan, Mapping):
        raise UpgradeError("upgrade plan must be an object")
    value = dict(plan)
    if value.get("action") != "upgrade-preview" or value.get("mutation") != "none":
        raise UpgradeError("only a read-only upgrade preview may be applied")
    for field in ("target", "managed_writes", "migration_steps", "validation_commands"):
        if field not in value:
            raise UpgradeError(f"upgrade plan is missing {field}")
    target = value["target"]
    if not isinstance(target, Mapping) or not isinstance(target.get("packs"), list):
        raise UpgradeError("upgrade target packs are invalid")
    if not all(isinstance(item, Mapping) and isinstance(item.get("id"), str) for item in target["packs"]):
        raise UpgradeError("upgrade target pack entry is invalid")
    if not isinstance(value["managed_writes"], list) or not isinstance(value["migration_steps"], list):
        raise UpgradeError("upgrade plan write or migration list is invalid")
    if not isinstance(value["validation_commands"], list):
        raise UpgradeError("upgrade validation commands are invalid")
    return value


def _migration_preview(plan: Mapping[str, object], snapshot_paths: tuple[str, ...]) -> dict[str, object]:
    changes: list[dict[str, object]] = []
    for step in plan["migration_steps"]:
        if not isinstance(step, Mapping) or step.get("kind") != "pack":
            continue
        changes.append({
            "pack": step["pack"],
            "from": step["from"],
            "to": step["to"],
            "kind": step["migration_kind"],
            "description": step["description"],
            "effects": dict(step["effects"]),
        })
    return {
        "ok": True,
        "changes": changes,
        "blockers": [],
        "mutation": "none",
        "snapshot_paths": list(snapshot_paths),
    }


def _snapshot_paths(
    project: Path,
    plan: Mapping[str, object],
    files: Mapping[PurePosixPath, str],
    prior_manifest: GeneratedManifest | None,
) -> tuple[str, ...]:
    paths = {
        str(item["path"])
        for item in plan["managed_writes"]
        if isinstance(item, Mapping) and item.get("action") in {"create", "update", "delete"}
    }
    paths.update(relative.as_posix() for relative in files)
    if prior_manifest is not None:
        paths.update(item.path for item in prior_manifest.files)
    return tuple(sorted(paths))


def _verify_installed_binding(project: Path, config: ProjectConfig, expected_runtime_schema: int) -> None:
    try:
        lock = read_lockfile(project)
        manifest = read_manifest(project)
    except (FileNotFoundError, ValueError) as error:
        raise UpgradeError(f"installed lock/manifest binding is unavailable: {error}") from error
    if manifest.digest != lock.generated_manifest_sha256:
        raise UpgradeError("installed manifest digest does not match the lock binding")
    if (
        manifest.tasktra_version != lock.tasktra_version
        or manifest.catalog_version != lock.catalog_version
        or manifest.packs != lock.packs
    ):
        raise UpgradeError("installed manifest versions or packs do not match the lock binding")
    locked_runtime = dict(lock.schema_versions).get("runtime")
    if locked_runtime != expected_runtime_schema:
        raise UpgradeError("upgrade plan current runtime schema does not match the installed lock")
    try:
        database = config.database_path(project)
    except ConfigError as error:
        raise UpgradeError(f"runtime database is outside the project authority scope: {error}") from error
    actual_runtime = StateStore(database).inspect_schema_version()
    if actual_runtime != expected_runtime_schema:
        raise UpgradeError(
            f"on-disk runtime schema {actual_runtime} does not match the installed lock {expected_runtime_schema}"
        )


def _optional_manifest(project: Path) -> GeneratedManifest | None:
    try:
        return read_manifest(project)
    except FileNotFoundError:
        return None


def _write_canonical_projection(
    project: Path,
    catalog_root: Path | str,
    catalog: Catalog,
    config: ProjectConfig,
    projection: object,
    prior_manifest: GeneratedManifest | None,
    allowed_deletes: set[str],
) -> None:
    prior = {item.path: item for item in prior_manifest.files} if prior_manifest is not None else {}
    desired_paths = {relative.as_posix() for relative in projection.files}
    for relative, content in projection.files.items():
        path = project.joinpath(*relative.parts)
        record = prior.get(relative.as_posix())
        if path.exists():
            if record is None:
                raise UpgradeError(f"managed output would replace project-owned content: {relative.as_posix()}")
            actual = sha256_bytes(path.read_bytes())
            desired = sha256(content.encode("utf-8")).hexdigest()
            if actual != record.sha256 and actual != desired:
                raise UpgradeError(f"managed output has local edits: {relative.as_posix()}")
    for relative, record in sorted(prior.items()):
        if relative in desired_paths:
            continue
        path = _safe_project_file(project, relative)
        if path.exists():
            if relative not in allowed_deletes:
                raise UpgradeError(f"stale managed output deletion was not declared by the preview: {relative}")
            if not path.is_file() or sha256_bytes(path.read_bytes()) != record.sha256:
                raise UpgradeError(f"stale managed output has local edits: {relative}")
            path.unlink()

    write_projection(project, projection)
    manifest = build_generated_manifest(
        projection.files,
        tasktra_version=__version__,
        catalog_version=catalog.version,
        packs=projection.packs,
    )
    lock = build_lockfile(
        manifest,
        catalog_source_sha256=_catalog_digest(Path(catalog_root)),
        pack_versions={identifier: catalog.packs[identifier].version for identifier in projection.packs},
        pack_contracts={
            identifier: {
                "version": catalog.packs[identifier].version,
                "contract_version": catalog.packs[identifier].contract_version,
                "trust": catalog.packs[identifier].trust,
                "sha256": catalog.packs[identifier].source_sha256,
            }
            for identifier in projection.packs
        },
        schema_versions={
            "configuration": config.version,
            "handoff": 1,
            "lesson-proposal": 1,
            "routing": 1,
            "runtime": StateStore(config.database_path(project)).inspect_schema_version(),
            "telemetry": 1,
            "work-item": 1,
            "workflow": 1,
        },
    )
    write_manifest(project, manifest)
    write_lockfile(project, lock)
    if read_manifest(project) != manifest or read_lockfile(project) != lock:
        raise UpgradeError("post-upgrade manifest or lock verification failed")


def _safe_project_file(project: Path, relative: str) -> Path:
    path = project.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve(strict=False).relative_to(project)
    except (OSError, ValueError) as error:
        raise UpgradeError(f"managed path escapes project: {relative}") from error
    if path.is_symlink():
        raise UpgradeError(f"managed path crosses a link: {relative}")
    return path


def _runtime_transition(plan: Mapping[str, object]) -> tuple[int, int]:
    current = plan.get("current")
    target = plan.get("target")
    if not isinstance(current, Mapping) or not isinstance(target, Mapping):
        raise UpgradeError("upgrade plan current or target state is invalid")
    before = current.get("runtime_schema_version")
    observed = current.get("runtime_state_schema_version")
    after = target.get("runtime_schema_version")
    if not isinstance(before, int) or not isinstance(observed, int) or not isinstance(after, int):
        raise UpgradeError("upgrade plan runtime schema binding is invalid")
    if before != observed:
        raise UpgradeError("upgrade plan runtime schema does not match its on-disk observation")
    steps = [
        item for item in plan["migration_steps"]
        if isinstance(item, Mapping) and item.get("kind") == "runtime-schema"
    ]
    expected_steps = 0 if before == after else 1
    if len(steps) != expected_steps:
        raise UpgradeError("upgrade plan runtime transition is not exact")
    if steps and (steps[0].get("from"), steps[0].get("to")) != (before, after):
        raise UpgradeError("upgrade plan runtime step does not match current and target schemas")
    return before, after


def _catalog_digest(root: Path) -> str:
    from .compiler import catalog_digest

    return catalog_digest(root)


def _is_compile_check(command: tuple[str, ...]) -> bool:
    return (
        len(command) >= 5
        and command[1:4] == ("-m", "tasktra", "compile")
        and "--check" in command
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
