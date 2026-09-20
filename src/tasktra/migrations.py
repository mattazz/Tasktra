"""Bounded, recoverable execution for explicitly authorized upgrade migrations.

Planning remains elsewhere and is always read-only.  This module accepts only a
validated preview with an exact digest, snapshots every declared project write
path before dispatch, invokes argv directly, and automatically restores the
snapshot when execution or verification fails.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence


MAX_MIGRATION_FILES = 512
MAX_MIGRATION_BYTES = 16 * 1024 * 1024
MAX_MIGRATION_OUTPUT = 64 * 1024
SNAPSHOT_SCHEMA_VERSION = 1


class MigrationError(ValueError):
    """Raised when migration execution cannot remain bounded and recoverable."""


@dataclass(frozen=True)
class MigrationExecution:
    plan_sha256: str
    snapshot_path: Path
    before_sha256: str
    after_sha256: str
    commands: tuple[dict[str, object], ...]
    verification: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        rollback_available = self.verification.get("runtime_schema_changed") is not True
        return {
            "plan_sha256": self.plan_sha256,
            "snapshot": str(self.snapshot_path),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "commands": [dict(item) for item in self.commands],
            "verification": dict(self.verification),
            "rollback_available": rollback_available,
        }


def migration_plan_digest(preview: Mapping[str, object]) -> str:
    normalized = _normalize_preview(preview)
    return sha256(_canonical(normalized)).hexdigest()


def preview_migration_snapshot(root: Path | str, preview: Mapping[str, object]) -> dict[str, object]:
    """Return the exact bounded snapshot impact without writing anything."""
    project = Path(root).resolve()
    normalized = _normalize_preview(preview)
    write_paths = _write_paths(normalized)
    entries = _capture_entries(project, write_paths)
    digest = sha256(_canonical(entries)).hexdigest()
    plan_sha256 = sha256(_canonical(normalized)).hexdigest()
    return {
        "ok": True,
        "action": "migration-snapshot-preview",
        "mutation": "none",
        "plan_sha256": plan_sha256,
        "write_paths": list(write_paths),
        "entry_count": len(entries),
        "total_bytes": sum(int(item.get("size", 0)) for item in entries),
        "before_sha256": digest,
        "snapshot": str(_snapshot_path(project, plan_sha256)),
    }


def apply_migration_plan(
    root: Path | str,
    catalog_root: Path | str,
    preview: Mapping[str, object],
    *,
    expected_plan_sha256: str,
    confirmed: bool,
    allow_network: bool = False,
    timeout_seconds: int = 300,
    verifier: Callable[[], Mapping[str, object]] | None = None,
) -> MigrationExecution:
    """Apply an exact preview with automatic rollback on every failure.

    Authorization is intentionally checked by the caller against the durable
    goal ledger immediately before entering this module.  ``confirmed`` keeps
    accidental programmatic calls from turning a preview into a write.
    """
    if not confirmed:
        raise MigrationError("migration apply requires explicit confirmation")
    if timeout_seconds < 1 or timeout_seconds > 3600:
        raise MigrationError("migration timeout must be between 1 and 3600 seconds")
    project = Path(root).resolve()
    catalog = Path(catalog_root).resolve()
    normalized = _normalize_preview(preview)
    plan_sha256 = sha256(_canonical(normalized)).hexdigest()
    if expected_plan_sha256 != plan_sha256:
        raise MigrationError("migration preview digest changed; preview again before applying")
    write_paths = _write_paths(normalized)
    entries = _capture_entries(project, write_paths)
    before_sha256 = sha256(_canonical(entries)).hexdigest()
    snapshot = _write_snapshot(project, plan_sha256, write_paths, entries, before_sha256)
    command_results: list[dict[str, object]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="tasktra-upgrade-") as staging_directory:
            staging = Path(staging_directory)
            staged_project = staging / "project"
            staged_project.mkdir()
            _restore_entries(staged_project, write_paths, entries)
            for change in normalized["changes"]:
                effects = change["effects"]
                if change["kind"] != "executable":
                    continue
                if effects["network"] and not allow_network:
                    raise MigrationError(f"migration for {change['pack']} declares network access")
                pack_root = catalog / "packs" / str(change["pack"])
                _safe_directory(catalog, pack_root, label="pack migration directory")
                staged_pack = staging / "catalog" / "packs" / str(change["pack"])
                staged_pack.mkdir(parents=True)
                for relative in effects["read_paths"]:
                    source = _safe_pack_file(pack_root, relative)
                    destination = staged_pack.joinpath(*PurePosixPath(relative).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_bytes(destination, source.read_bytes())
                allowed_environment = (
                    "COMSPEC", "LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
                )
                environment = {
                    name: os.environ[name]
                    for name in allowed_environment
                    if name in os.environ
                }
                environment["TASKTRA_PROJECT_ROOT"] = str(staged_project)
                environment["TASKTRA_NETWORK_ALLOWED"] = "1" if effects["network"] else "0"
                environment["PYTHONNOUSERSITE"] = "1"
                try:
                    completed = subprocess.run(
                        list(effects["argv"]),
                        cwd=staged_pack,
                        env=environment,
                        shell=False,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise MigrationError(f"migration command failed to launch or timed out for {change['pack']}: {error}") from error
                result = {
                    "pack": change["pack"],
                    "argv": list(effects["argv"]),
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout[:MAX_MIGRATION_OUTPUT],
                    "stderr": completed.stderr[:MAX_MIGRATION_OUTPUT],
                    "stdout_truncated": len(completed.stdout) > MAX_MIGRATION_OUTPUT,
                    "stderr_truncated": len(completed.stderr) > MAX_MIGRATION_OUTPUT,
                }
                command_results.append(result)
                if completed.returncode != 0:
                    raise MigrationError(f"migration command failed for {change['pack']} with exit code {completed.returncode}")
            staged_entries = _capture_entries(staged_project, write_paths)
            _restore_entries(project, write_paths, staged_entries)
        verification = dict(verifier() if verifier is not None else {"ok": True})
        if verification.get("ok") is not True:
            raise MigrationError("post-migration verification failed")
        after_entries = _capture_entries(project, write_paths)
        after_sha256 = sha256(_canonical(after_entries)).hexdigest()
        receipt = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "plan_sha256": plan_sha256,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "commands": command_results,
            "verification": verification,
            "rollback_available": verification.get("runtime_schema_changed") is not True,
        }
        _write_json(snapshot.parent / "receipt.json", receipt)
        return MigrationExecution(
            plan_sha256,
            snapshot,
            before_sha256,
            after_sha256,
            tuple(command_results),
            verification,
        )
    except BaseException as error:
        restored = rollback_migration(project, plan_sha256, expected_before_sha256=before_sha256)
        raise MigrationError(
            f"migration failed and snapshot rollback completed ({restored['restored_sha256']}): {error}"
        ) from error


def rollback_migration(
    root: Path | str,
    plan_sha256: str,
    *,
    expected_before_sha256: str,
) -> dict[str, object]:
    """Restore one immutable pre-mutation snapshot and verify its digest."""
    _require_sha256(plan_sha256, "plan_sha256")
    project = Path(root).resolve()
    snapshot = _snapshot_path(project, plan_sha256)
    data = _read_snapshot(snapshot)
    if data["plan_sha256"] != plan_sha256:
        raise MigrationError("snapshot plan digest does not match its directory")
    _require_sha256(expected_before_sha256, "expected_before_sha256")
    if data["before_sha256"] != expected_before_sha256:
        raise MigrationError("snapshot does not match the expected pre-mutation digest")
    write_paths = tuple(str(item) for item in data["write_paths"])
    entries = tuple(dict(item) for item in data["entries"])
    _restore_entries(project, write_paths, entries)
    restored = _capture_entries(project, write_paths)
    restored_sha256 = sha256(_canonical(restored)).hexdigest()
    if restored_sha256 != data["before_sha256"]:
        raise MigrationError("rollback verification did not reproduce the pre-mutation snapshot")
    result = {
        "ok": True,
        "action": "migration-rollback",
        "plan_sha256": plan_sha256,
        "restored_sha256": restored_sha256,
        "entry_count": len(restored),
    }
    _write_json(snapshot.parent / "rollback.json", result)
    return result


def _normalize_preview(preview: Mapping[str, object]) -> dict[str, object]:
    base_fields = {"ok", "changes", "blockers", "mutation"}
    if not isinstance(preview, Mapping) or set(preview) not in (base_fields, base_fields | {"snapshot_paths"}):
        raise MigrationError("migration preview has missing or unknown fields")
    if preview.get("ok") is not True or preview.get("mutation") != "none" or preview.get("blockers") != []:
        raise MigrationError("only an unblocked read-only migration preview may be applied")
    changes = preview.get("changes")
    if not isinstance(changes, list) or len(changes) > 64:
        raise MigrationError("migration preview changes must be a bounded list")
    normalized: list[dict[str, object]] = []
    for raw in changes:
        if not isinstance(raw, Mapping) or set(raw) != {"pack", "from", "to", "kind", "description", "effects"}:
            raise MigrationError("migration change has missing or unknown fields")
        if raw["kind"] not in {"declarative", "executable"}:
            raise MigrationError("migration change kind is invalid")
        for key in ("pack", "from", "to", "description"):
            if not isinstance(raw[key], str) or not raw[key]:
                raise MigrationError(f"migration change {key} must be non-empty")
        effects = raw["effects"]
        if not isinstance(effects, Mapping) or set(effects) != {"argv", "network", "read_paths", "write_paths"}:
            raise MigrationError("migration effects have missing or unknown fields")
        argv = _string_list(effects["argv"], "argv", maximum=32)
        reads = _safe_paths(effects["read_paths"], "read_paths")
        writes = _safe_paths(effects["write_paths"], "write_paths")
        if not isinstance(effects["network"], bool):
            raise MigrationError("migration network effect must be boolean")
        if raw["kind"] == "executable" and (not argv or not writes):
            raise MigrationError("executable migration requires argv and declared write paths")
        if raw["kind"] == "declarative" and (argv or effects["network"] or reads or writes):
            raise MigrationError("declarative migration cannot contain executable effects")
        normalized.append({
            "pack": raw["pack"], "from": raw["from"], "to": raw["to"],
            "kind": raw["kind"], "description": raw["description"],
            "effects": {"argv": list(argv), "network": effects["network"], "read_paths": list(reads), "write_paths": list(writes)},
        })
    snapshot_paths = _safe_paths(
        preview.get("snapshot_paths", []),
        "snapshot_paths",
        maximum=MAX_MIGRATION_FILES,
    )
    return {"schema_version": 1, "changes": normalized, "snapshot_paths": list(snapshot_paths)}


def _write_paths(normalized: Mapping[str, object]) -> tuple[str, ...]:
    values = {
        path
        for change in normalized["changes"]
        for path in change["effects"]["write_paths"]
    }
    values.update(str(path) for path in normalized.get("snapshot_paths", ()))
    return tuple(sorted(values))


def _capture_entries(project: Path, write_paths: Sequence[str]) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    total_bytes = 0
    for relative in write_paths:
        target = _safe_target(project, relative)
        if not target.exists():
            entries.append({"path": relative, "kind": "absent", "size": 0})
            continue
        if target.is_file():
            raw = target.read_bytes()
            total_bytes += len(raw)
            entries.append(_file_entry(relative, raw))
            continue
        if not target.is_dir():
            raise MigrationError(f"migration write path is not a regular file or directory: {relative}")
        entries.append({"path": relative, "kind": "directory", "size": 0})
        for child in sorted(target.rglob("*")):
            child_relative = child.relative_to(project).as_posix()
            if child_relative.startswith(".tasktra/upgrades/"):
                continue
            if _is_linklike(child):
                raise MigrationError(f"migration snapshot crosses a link or reparse point: {child_relative}")
            if child.is_dir():
                entries.append({"path": child_relative, "kind": "directory", "size": 0})
            elif child.is_file():
                raw = child.read_bytes()
                total_bytes += len(raw)
                entries.append(_file_entry(child_relative, raw))
            else:
                raise MigrationError(f"migration snapshot contains an unsupported entry: {child_relative}")
            if len(entries) > MAX_MIGRATION_FILES:
                raise MigrationError("migration snapshot exceeds the file-entry bound")
            if total_bytes > MAX_MIGRATION_BYTES:
                raise MigrationError("migration snapshot exceeds the byte bound")
    if len(entries) > MAX_MIGRATION_FILES:
        raise MigrationError("migration snapshot exceeds the file-entry bound")
    if total_bytes > MAX_MIGRATION_BYTES:
        raise MigrationError("migration snapshot exceeds the byte bound")
    return tuple(sorted(entries, key=lambda item: (str(item["path"]), str(item["kind"]))))


def _restore_entries(project: Path, write_paths: Sequence[str], entries: Sequence[Mapping[str, object]]) -> None:
    expected_files = {str(item["path"]) for item in entries if item["kind"] == "file"}
    expected_directories = {str(item["path"]) for item in entries if item["kind"] == "directory"}
    for relative in sorted(write_paths, key=lambda item: (item.count("/"), item), reverse=True):
        target = _safe_target(project, relative)
        if target.is_file():
            if relative not in expected_files:
                target.unlink()
        elif target.is_dir():
            current = sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True)
            if len(current) > MAX_MIGRATION_FILES:
                raise MigrationError("rollback target exceeds the file-entry bound")
            for child in current:
                child_relative = child.relative_to(project).as_posix()
                if child_relative.startswith(".tasktra/upgrades/"):
                    continue
                if _is_linklike(child):
                    raise MigrationError(f"rollback target contains a link or reparse point: {child_relative}")
                if child.is_file() and child_relative not in expected_files:
                    child.unlink()
                elif child.is_dir() and child_relative not in expected_directories:
                    try:
                        child.rmdir()
                    except OSError:
                        pass
    for item in sorted(entries, key=lambda value: (str(value["kind"]) != "directory", str(value["path"]))):
        path = _safe_target(project, str(item["path"]))
        kind = item["kind"]
        if kind == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = b64decode(str(item["content_b64"]), validate=True)
            # Declarative migrations commonly stage an unchanged managed
            # tree. Avoid a no-op atomic rewrite for every file: on large
            # projections (and especially Windows scanners) that turns a
            # bounded migration into work proportional to all managed files.
            if path.is_file() and path.read_bytes() == raw:
                continue
            _atomic_bytes(path, raw)
        elif kind == "absent":
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError as error:
                    raise MigrationError(f"rollback could not remove newly created directory: {item['path']}") from error
        else:
            raise MigrationError("snapshot entry kind is invalid")


def _write_snapshot(project: Path, plan_sha256: str, write_paths: Sequence[str], entries: Sequence[Mapping[str, object]], before_sha256: str) -> Path:
    path = _snapshot_path(project, plan_sha256)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "plan_sha256": plan_sha256,
        "before_sha256": before_sha256,
        "write_paths": list(write_paths),
        "entries": [dict(item) for item in entries],
    }
    if path.exists():
        existing = _read_snapshot(path)
        if existing != payload:
            raise MigrationError("existing migration snapshot conflicts with the current pre-mutation state")
        return path
    _write_json(path, payload)
    return path


def _read_snapshot(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MigrationError(f"migration snapshot is unavailable: {path}") from error
    if len(raw) > (MAX_MIGRATION_BYTES * 2) + 1024 * 1024:
        raise MigrationError("migration snapshot JSON exceeds its bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError("migration snapshot is invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "plan_sha256", "before_sha256", "write_paths", "entries"}:
        raise MigrationError("migration snapshot has missing or unknown fields")
    if value["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise MigrationError("migration snapshot schema version is unsupported")
    _require_sha256(value["plan_sha256"], "snapshot plan_sha256")
    _require_sha256(value["before_sha256"], "snapshot before_sha256")
    _safe_paths(
        value["write_paths"],
        "snapshot write_paths",
        maximum=MAX_MIGRATION_FILES,
    )
    if not isinstance(value["entries"], list) or len(value["entries"]) > MAX_MIGRATION_FILES:
        raise MigrationError("migration snapshot entries are invalid or unbounded")
    total_bytes = 0
    for item in value["entries"]:
        if not isinstance(item, dict) or item.get("kind") not in {"absent", "directory", "file"}:
            raise MigrationError("migration snapshot entry is invalid")
        _safe_paths([item.get("path")], "snapshot entry path")
        if item["kind"] in {"absent", "directory"}:
            if set(item) != {"path", "kind", "size"} or item["size"] != 0:
                raise MigrationError("migration snapshot non-file entry is invalid")
            continue
        if set(item) != {"path", "kind", "size", "sha256", "content_b64"}:
            raise MigrationError("migration snapshot file entry has missing or unknown fields")
        if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
            raise MigrationError("migration snapshot file size is invalid")
        _require_sha256(item["sha256"], "snapshot file sha256")
        try:
            content = b64decode(str(item["content_b64"]), validate=True)
        except ValueError as error:
            raise MigrationError("migration snapshot file content is invalid base64") from error
        if len(content) != item["size"] or sha256(content).hexdigest() != item["sha256"]:
            raise MigrationError("migration snapshot file content does not match its metadata")
        total_bytes += len(content)
        if total_bytes > MAX_MIGRATION_BYTES:
            raise MigrationError("migration snapshot exceeds the byte bound")
    if sha256(_canonical(tuple(value["entries"]))).hexdigest() != value["before_sha256"]:
        raise MigrationError("migration snapshot entry digest is invalid")
    return value


def _snapshot_path(project: Path, plan_sha256: str) -> Path:
    _require_sha256(plan_sha256, "plan_sha256")
    return project / ".tasktra" / "upgrades" / plan_sha256 / "snapshot.json"


def _safe_target(project: Path, relative: str) -> Path:
    _safe_paths([relative], "path")
    target = project.joinpath(*PurePosixPath(relative).parts)
    cursor = project
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if _is_linklike(cursor):
            raise MigrationError(f"migration path crosses a link or reparse point: {relative}")
    try:
        target.resolve(strict=False).relative_to(project)
    except ValueError as error:
        raise MigrationError(f"migration path escapes the project: {relative}") from error
    return target


def _safe_directory(root: Path, path: Path, *, label: str) -> None:
    try:
        path.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise MigrationError(f"{label} is missing or escapes its root: {path}") from error
    cursor = root
    for part in path.relative_to(root).parts:
        cursor = cursor / part
        if _is_linklike(cursor):
            raise MigrationError(f"{label} crosses a link or reparse point: {path}")
    if not path.is_dir():
        raise MigrationError(f"{label} is not a directory: {path}")


def _safe_pack_file(pack_root: Path, relative: str) -> Path:
    _safe_paths([relative], "migration read path")
    candidate = pack_root.joinpath(*PurePosixPath(relative).parts)
    cursor = pack_root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if _is_linklike(cursor):
            raise MigrationError(f"migration read path crosses a link or reparse point: {relative}")
    try:
        candidate.resolve(strict=True).relative_to(pack_root.resolve())
    except (FileNotFoundError, ValueError) as error:
        raise MigrationError(f"migration read path is missing or escapes its pack: {relative}") from error
    if not candidate.is_file():
        raise MigrationError(f"migration read path is not a regular file: {relative}")
    return candidate


def _safe_paths(value: object, label: str, *, maximum: int = 64) -> tuple[str, ...]:
    values = _string_list(value, label, maximum=maximum)
    for item in values:
        path = PurePosixPath(item)
        if item in {"", "."} or path.is_absolute() or path.as_posix() != item or ".." in path.parts or "\\" in item or ":" in item:
            raise MigrationError(f"{label} contains an unsafe relative POSIX path: {item}")
    if len(set(values)) != len(values):
        raise MigrationError(f"{label} must not contain duplicates")
    return values


def _string_list(value: object, label: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum or not all(isinstance(item, str) and item for item in value):
        raise MigrationError(f"{label} must be a bounded list of non-empty strings")
    return tuple(value)


def _file_entry(path: str, raw: bytes) -> dict[str, object]:
    return {
        "path": path,
        "kind": "file",
        "size": len(raw),
        "sha256": sha256(raw).hexdigest(),
        "content_b64": b64encode(raw).decode("ascii"),
    }


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_linklike(path.parent) or _is_linklike(path):
        raise MigrationError(f"migration journal path crosses a link or reparse point: {path}")
    _atomic_bytes(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")


def _atomic_bytes(path: Path, raw: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tasktra-migration-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise MigrationError(f"{label} must be a lowercase SHA-256")


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or bool(is_junction and is_junction()):
        return True
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
