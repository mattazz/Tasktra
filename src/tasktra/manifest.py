"""Deterministic records for generated Tasktra output and installed inputs.

The manifest intentionally stores only paths, sizes, and SHA-256 digests.  It
can therefore detect drift without copying project instructions into Tasktra's
runtime state or lockfile.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Mapping


LOCKFILE_FILENAME = "tasktra.lock"
GENERATED_DIRECTORY = "generated"
GENERATED_MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
LOCKFILE_SCHEMA_VERSION = 3


class ManifestError(ValueError):
    """Raised when a manifest or lockfile cannot be validated safely."""


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _relative_path(value: str | PurePosixPath) -> str:
    raw = str(value)
    if not raw or "\\" in raw or ":" in raw:
        raise ManifestError(f"generated path must be a safe relative POSIX path: {value}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."} or path.as_posix() != raw:
        raise ManifestError(f"generated path must be a safe relative path: {value}")
    return path.as_posix()


@dataclass(frozen=True, order=True)
class GeneratedFile:
    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ManifestError(f"invalid SHA-256 for {self.path}")
        if self.size < 0:
            raise ManifestError(f"negative size for {self.path}")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class GeneratedManifest:
    """The exact file set emitted by a compiler run."""

    tasktra_version: str
    catalog_version: str
    packs: tuple[str, ...]
    files: tuple[GeneratedFile, ...]
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema version: {self.schema_version}")
        if not self.tasktra_version or not self.catalog_version:
            raise ManifestError("manifest requires tasktra_version and catalog_version")
        if tuple(sorted(set(self.packs))) != self.packs:
            raise ManifestError("manifest packs must be unique and sorted")
        paths = tuple(item.path for item in self.files)
        if tuple(sorted(paths)) != paths or len(set(paths)) != len(paths):
            raise ManifestError("manifest files must be unique and sorted by path")

    def as_dict(self) -> dict[str, object]:
        return {
            "catalog_version": self.catalog_version,
            "files": [item.as_dict() for item in self.files],
            "packs": list(self.packs),
            "schema_version": self.schema_version,
            "tasktra_version": self.tasktra_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"

    @property
    def digest(self) -> str:
        return sha256_text(self.canonical_json())

    @classmethod
    def from_dict(cls, value: object) -> "GeneratedManifest":
        data = _object(value, "manifest")
        _keys(data, {"schema_version", "tasktra_version", "catalog_version", "packs", "files"}, "manifest")
        files = tuple(
            GeneratedFile(
                path=_string(_object(item, "manifest file").get("path"), "manifest file.path"),
                sha256=_string(_object(item, "manifest file").get("sha256"), "manifest file.sha256"),
                size=_integer(_object(item, "manifest file").get("size"), "manifest file.size"),
            )
            for item in _list(data.get("files"), "manifest.files")
        )
        return cls(
            schema_version=_integer(data.get("schema_version"), "manifest.schema_version"),
            tasktra_version=_string(data.get("tasktra_version"), "manifest.tasktra_version"),
            catalog_version=_string(data.get("catalog_version"), "manifest.catalog_version"),
            packs=tuple(_string(item, "manifest.packs item") for item in _list(data.get("packs"), "manifest.packs")),
            files=files,
        )


@dataclass(frozen=True)
class TasktraLock:
    """Pinned inputs required to reproduce a generated projection."""

    tasktra_version: str
    catalog_version: str
    packs: tuple[str, ...]
    pack_versions: tuple[tuple[str, str], ...]
    generated_manifest_sha256: str
    catalog_source_sha256: str
    schema_versions: tuple[tuple[str, int], ...] = ()
    pack_contracts: tuple[tuple[str, str, int, str, str], ...] = ()
    schema_version: int = LOCKFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {2, LOCKFILE_SCHEMA_VERSION}:
            raise ManifestError(f"unsupported lockfile schema version: {self.schema_version}")
        if not self.tasktra_version or not self.catalog_version:
            raise ManifestError("lockfile requires tasktra_version and catalog_version")
        if tuple(sorted(set(self.packs))) != self.packs:
            raise ManifestError("lockfile packs must be unique and sorted")
        pack_names = tuple(name for name, _ in self.pack_versions)
        if pack_names != self.packs:
            raise ManifestError("lockfile pack_versions must exactly match enabled packs")
        if any(not version for _, version in self.pack_versions):
            raise ManifestError("lockfile pack versions must be non-empty")
        if self.schema_version >= 3:
            contract_names = tuple(item[0] for item in self.pack_contracts)
            if contract_names != self.packs:
                raise ManifestError("lockfile pack_contracts must exactly match enabled packs")
            for name, version, contract_version, trust, digest in self.pack_contracts:
                if not version or contract_version < 1:
                    raise ManifestError(f"lockfile pack contract is invalid: {name}")
                if trust not in {"builtin-data-only", "third-party-data-only", "third-party-executable"}:
                    raise ManifestError(f"lockfile pack trust is invalid: {name}")
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ManifestError(f"lockfile pack checksum must be a SHA-256: {name}")
        elif self.pack_contracts:
            raise ManifestError("legacy lockfile schema 2 cannot contain pack_contracts")
        if len(self.generated_manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.generated_manifest_sha256):
            raise ManifestError("lockfile generated_manifest_sha256 must be a SHA-256")
        if len(self.catalog_source_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.catalog_source_sha256):
            raise ManifestError("lockfile catalog_source_sha256 must be a SHA-256")
        names = tuple(name for name, _ in self.schema_versions)
        if tuple(sorted(names)) != names or len(set(names)) != len(names):
            raise ManifestError("lockfile schema versions must be unique and sorted")
        if any(not name or version < 1 for name, version in self.schema_versions):
            raise ManifestError("lockfile schema versions must have non-empty names and positive versions")

    def as_dict(self) -> dict[str, object]:
        result = {
            "catalog_version": self.catalog_version,
            "catalog_source_sha256": self.catalog_source_sha256,
            "generated_manifest_sha256": self.generated_manifest_sha256,
            "packs": list(self.packs),
            "pack_versions": {name: version for name, version in self.pack_versions},
            "schema_version": self.schema_version,
            "schema_versions": {name: version for name, version in self.schema_versions},
            "tasktra_version": self.tasktra_version,
        }
        if self.schema_version >= 3:
            result["pack_contracts"] = {
                name: {"version": version, "contract_version": contract, "trust": trust, "sha256": digest}
                for name, version, contract, trust, digest in self.pack_contracts
            }
        return result

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, value: object) -> "TasktraLock":
        data = _object(value, "lockfile")
        schema_version = _integer(data.get("schema_version"), "lockfile.schema_version")
        allowed = {"schema_version", "tasktra_version", "catalog_version", "catalog_source_sha256", "packs", "pack_versions", "generated_manifest_sha256", "schema_versions"}
        if schema_version >= 3:
            allowed.add("pack_contracts")
        _keys(data, allowed, "lockfile")
        versions = _object(data.get("schema_versions", {}), "lockfile.schema_versions")
        pack_versions = _object(data.get("pack_versions", {}), "lockfile.pack_versions")
        contracts = _object(data.get("pack_contracts", {}), "lockfile.pack_contracts")
        parsed_contracts: list[tuple[str, str, int, str, str]] = []
        for name, raw in contracts.items():
            contract = _object(raw, f"lockfile.pack_contracts.{name}")
            _keys(contract, {"version", "contract_version", "trust", "sha256"}, f"lockfile.pack_contracts.{name}")
            parsed_contracts.append((
                name,
                _string(contract.get("version"), f"lockfile.pack_contracts.{name}.version"),
                _integer(contract.get("contract_version"), f"lockfile.pack_contracts.{name}.contract_version"),
                _string(contract.get("trust"), f"lockfile.pack_contracts.{name}.trust"),
                _string(contract.get("sha256"), f"lockfile.pack_contracts.{name}.sha256"),
            ))
        return cls(
            schema_version=schema_version,
            tasktra_version=_string(data.get("tasktra_version"), "lockfile.tasktra_version"),
            catalog_version=_string(data.get("catalog_version"), "lockfile.catalog_version"),
            catalog_source_sha256=_string(data.get("catalog_source_sha256"), "lockfile.catalog_source_sha256"),
            packs=tuple(_string(item, "lockfile.packs item") for item in _list(data.get("packs"), "lockfile.packs")),
            pack_versions=tuple(sorted(
                (name, _string(version, f"lockfile.pack_versions.{name}"))
                for name, version in pack_versions.items()
            )),
            generated_manifest_sha256=_string(data.get("generated_manifest_sha256"), "lockfile.generated_manifest_sha256"),
            schema_versions=tuple(sorted((name, _integer(version, f"lockfile.schema_versions.{name}")) for name, version in versions.items())),
            pack_contracts=tuple(sorted(parsed_contracts)),
        )


def build_generated_manifest(
    files: Mapping[str | PurePosixPath, str | bytes], *, tasktra_version: str, catalog_version: str, packs: tuple[str, ...] | list[str] = (),
) -> GeneratedManifest:
    """Build a reproducible manifest from relative generated paths and bytes."""
    records: list[GeneratedFile] = []
    for path, content in files.items():
        safe_path = _relative_path(path)
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(raw, bytes):
            raise ManifestError(f"generated content for {safe_path} must be text or bytes")
        records.append(GeneratedFile(safe_path, sha256_bytes(raw), len(raw)))
    return GeneratedManifest(tasktra_version, catalog_version, tuple(sorted(set(packs))), tuple(sorted(records)))


def build_lockfile(
    manifest: GeneratedManifest,
    *,
    catalog_source_sha256: str,
    pack_versions: Mapping[str, str],
    pack_contracts: Mapping[str, Mapping[str, object]],
    schema_versions: Mapping[str, int] | None = None,
) -> TasktraLock:
    versions = tuple(sorted((schema_versions or {}).items()))
    contracts = tuple(sorted(
        (
            name,
            str(value["version"]),
            int(value["contract_version"]),
            str(value["trust"]),
            str(value["sha256"]),
        )
        for name, value in pack_contracts.items()
    ))
    return TasktraLock(
        tasktra_version=manifest.tasktra_version,
        catalog_version=manifest.catalog_version,
        packs=manifest.packs,
        pack_versions=tuple(sorted(pack_versions.items())),
        generated_manifest_sha256=manifest.digest,
        catalog_source_sha256=catalog_source_sha256,
        schema_versions=versions,
        pack_contracts=contracts,
    )


def manifest_path(root: Path) -> Path:
    return root / ".tasktra" / GENERATED_DIRECTORY / GENERATED_MANIFEST_FILENAME


def lockfile_path(root: Path) -> Path:
    return root / ".tasktra" / LOCKFILE_FILENAME


def write_manifest(root: Path, manifest: GeneratedManifest) -> Path:
    return _write_newline(manifest_path(root), manifest.canonical_json())


def read_manifest(root: Path) -> GeneratedManifest:
    return GeneratedManifest.from_dict(_read_json(manifest_path(root)))


def write_lockfile(root: Path, lockfile: TasktraLock) -> Path:
    return _write_newline(lockfile_path(root), lockfile.canonical_json())


def read_lockfile(root: Path) -> TasktraLock:
    return TasktraLock.from_dict(_read_json(lockfile_path(root)))


def _write_newline(path: Path, text: str) -> Path:
    _reject_symlink_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tasktra-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _reject_symlink_ancestors(path: Path) -> None:
    """Keep manifest and lockfile writes within the requested project root."""
    metadata = next((parent for parent in (path.parent, *path.parents) if parent.name == ".tasktra"), None)
    if metadata is None:
        raise ManifestError(f"Tasktra metadata path must be below .tasktra: {path}")
    root = metadata.parent
    if root.is_symlink():
        raise ManifestError(f"Tasktra root cannot be a symlink: {root}")
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ManifestError(f"Tasktra metadata path crosses a symlink: {relative.as_posix()}")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"invalid JSON in {path}: {error}") from error


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ManifestError(f"{name} must be a list")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"{name} must be an integer")
    return value


def _keys(data: dict[str, object], expected: set[str], name: str) -> None:
    if set(data) != expected:
        unknown = sorted(set(data) - expected)
        missing = sorted(expected - set(data))
        details = ([f"unknown: {', '.join(unknown)}"] if unknown else []) + ([f"missing: {', '.join(missing)}"] if missing else [])
        raise ManifestError(f"{name} has invalid keys ({'; '.join(details)})")
