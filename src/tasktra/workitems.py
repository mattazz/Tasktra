"""Local Markdown work-item storage.

Work items deliberately live outside the runtime database so they remain easy
to inspect, review, and move with a repository.  Each document has a JSON
frontmatter block (JSON is valid YAML) followed by Markdown body text.  The
JSON encoding is used here because it is dependency-free and canonical when
written with sorted keys.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Iterator
from uuid import uuid4

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, MAX_IDENTIFIER_CHARS as _MAX_IDENTIFIER_CHARS, is_identifier, require_identifier, require_optional_identifier
from .workflow import WorkflowError, workflow_completion_token


_STATUSES = frozenset({"planned", "ready", "in-progress", "blocked", "in-review", "done", "cancelled"})
_UNSET = object()
WORK_ITEM_SCHEMA_VERSION = 1

# These limits keep a local planning ledger inexpensive to inspect and prevent
# a corrupt (or deliberately hostile) Markdown file from consuming unbounded
# memory when an agent lists work.  Bodies belong in explicit ``read`` calls.
MAX_WORK_ITEM_FILE_BYTES = 1_048_576
MAX_FRONTMATTER_BYTES = 65_536
MAX_BODY_CHARS = 524_288
MAX_IDENTIFIER_CHARS = _MAX_IDENTIFIER_CHARS
MAX_TITLE_CHARS = 240
MAX_GOAL_ID_CHARS = _MAX_IDENTIFIER_CHARS
MAX_LABELS = 32
MAX_LABEL_CHARS = 64
MAX_METADATA_BYTES = 65_536
MAX_METADATA_DEPTH = 8
MAX_METADATA_ENTRIES = 128
MAX_METADATA_LIST_ITEMS = 64
MAX_METADATA_KEY_CHARS = 128
MAX_METADATA_STRING_CHARS = 8_192
MAX_VERSION = 2_147_483_647

# Python 3.11 does not expose Path.is_junction().  Windows exposes both
# junctions and symbolic links through FILE_ATTRIBUTE_REPARSE_POINT, which is
# available via lstat().st_file_attributes.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WorkItemError(ValueError):
    """Raised when a local work-item operation is unsafe or invalid."""


class WorkItemNotFoundError(WorkItemError):
    pass


class WorkItemExistsError(WorkItemError):
    pass


class WorkItemConflictError(WorkItemError):
    pass


@dataclass(frozen=True)
class WorkItem:
    id: str
    title: str
    status: str
    body: str
    created_at: str
    updated_at: str
    version: int
    goal_id: str | None = None
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None
    schema_version: int = WORK_ITEM_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "body": self.body,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "goal_id": self.goal_id,
            "labels": list(self.labels),
            "metadata": self.metadata or {},
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class WorkItemSummary:
    """The deliberately compact representation returned by ``list``."""

    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    version: int
    goal_id: str | None = None
    labels: tuple[str, ...] = ()
    schema_version: int = WORK_ITEM_SCHEMA_VERSION

    @classmethod
    def from_item(cls, item: WorkItem) -> "WorkItemSummary":
        return cls(
            id=item.id,
            title=item.title,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
            goal_id=item.goal_id,
            labels=item.labels,
            schema_version=item.schema_version,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "goal_id": self.goal_id,
            "labels": list(self.labels),
            "schema_version": self.schema_version,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise WorkItemError("metadata must contain JSON-compatible values") from error


class WorkItemStore:
    """Safely manage project-scoped ``.tasktra/work-items`` Markdown files."""

    def __init__(self, project_root: Path | str):
        supplied_root = Path(project_root)
        if not supplied_root.is_dir():
            raise WorkItemError(f"Project root is not a directory: {supplied_root}")
        # A project root may itself be reached through a normal workspace
        # symlink.  Resolve it once; all managed children must remain below it.
        self.project_root = supplied_root.resolve(strict=True)
        self._tasktra_dir = self.project_root / ".tasktra"
        self.directory = self._tasktra_dir / "work-items"

    @staticmethod
    def _validate_identifier(item_id: str) -> str:
        try:
            return require_identifier(item_id, label="work item id")
        except IdentifierError as error:
            raise WorkItemError(str(error)) from error

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return False
        # POSIX uses the lstat mode for symbolic links.  Windows additionally
        # exposes junctions and other redirects as reparse points.  We reject
        # either kind even when it currently resolves inside the project:
        # managed paths must not be redirected at all.
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )

    def _assert_safe_ancestor_chain(self, path: Path) -> None:
        """Reject symbolic links and Windows reparse points below the root."""
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as error:
            raise WorkItemError("work item path escapes the project root") from error
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if self._is_reparse_point(current):
                raise WorkItemError(f"work item path contains a symbolic link or reparse point: {current}")

    def _assert_safe_path(self, path: Path, *, must_exist: bool = False) -> None:
        """Check lexical and resolved containment immediately before I/O.

        The lexical chain catches reparse points without following them.  The
        resolved check then catches a race or platform-specific redirect before
        a file operation is attempted.  Callers invoke this adjacent to every
        open, link, replace, or mkdir operation.
        """
        self._assert_safe_ancestor_chain(path)
        try:
            if path.exists() or path.is_symlink():
                resolved = path.resolve(strict=True)
            else:
                if must_exist:
                    raise WorkItemNotFoundError(f"Work item does not exist: {path.stem}")
                resolved = path.parent.resolve(strict=True) / path.name
            resolved.relative_to(self.project_root)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise WorkItemError(f"work item path escapes the project root: {path}") from error
        if self._is_reparse_point(path):
            raise WorkItemError(f"work item path contains a symbolic link or reparse point: {path}")

    def _ensure_directory(self) -> None:
        for directory in (self._tasktra_dir, self.directory):
            self._assert_safe_path(directory)
            if directory.exists():
                if not directory.is_dir():
                    raise WorkItemError(f"work item directory is not a directory: {directory}")
                continue
            self._assert_safe_path(directory)
            directory.mkdir()
            self._assert_safe_path(directory, must_exist=True)

    def _existing_directory(self) -> bool:
        self._assert_safe_path(self.directory)
        if not self.directory.exists():
            return False
        if not self.directory.is_dir():
            raise WorkItemError(f"work item directory is not a directory: {self.directory}")
        return True

    def _path_for(self, item_id: str) -> Path:
        item_id = self._validate_identifier(item_id)
        path = self.directory / f"{item_id}.md"
        self._assert_safe_path(path)
        if not self._within(path, self.directory):  # defensive; slug validation makes this true.
            raise WorkItemError("work item path escapes its directory")
        return path

    @staticmethod
    def _normalise_labels(labels: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        if labels is None:
            return ()
        if not isinstance(labels, (list, tuple)) or len(labels) > MAX_LABELS:
            raise WorkItemError("labels must be a list of non-empty strings")
        result = []
        seen: set[str] = set()
        for label in labels:
            if (
                not isinstance(label, str)
                or len(label) > MAX_LABEL_CHARS
                or not (cleaned := label.strip())
            ):
                raise WorkItemError("labels must be a list of non-empty strings")
            key = cleaned.casefold()
            if key in seen:
                raise WorkItemError("labels must be unique ignoring case")
            seen.add(key)
            result.append(cleaned)
        return tuple(sorted(result, key=str.casefold))

    @staticmethod
    def _normalise_goal_id(goal_id: str | None) -> str | None:
        try:
            return require_optional_identifier(goal_id, label="goal_id")
        except IdentifierError as error:
            raise WorkItemError(str(error)) from error

    @staticmethod
    def _normalise_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise WorkItemError("metadata must be an object")
        _validate_metadata_value(metadata)
        encoded = _canonical_json(metadata)
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise WorkItemError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
        return json.loads(encoded, parse_constant=_reject_non_finite, parse_float=_parse_finite_float)

    @staticmethod
    def _normalise_title(title: str) -> str:
        if (
            not isinstance(title, str)
            or len(title) > MAX_TITLE_CHARS
            or not (cleaned := title.strip())
        ):
            raise WorkItemError("title must be a non-empty string")
        return cleaned

    @staticmethod
    def _normalise_status(status: str) -> str:
        if status not in _STATUSES:
            raise WorkItemError(f"status must be one of {', '.join(sorted(_STATUSES))}")
        return status

    @staticmethod
    def _normalise_body(body: str) -> str:
        if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
            raise WorkItemError("body must be a string")
        return body

    def _validate_record(self, record: dict[str, Any]) -> WorkItem:
        try:
            validate_named(record, "work-item")
        except ContractError as error:
            raise WorkItemError(str(error)) from error
        item_id = self._validate_identifier(record["id"])
        title = self._normalise_title(record["title"])
        status = self._normalise_status(record["status"])
        body = self._normalise_body(record["body"])
        goal_id = self._normalise_goal_id(record["goal_id"])
        labels = self._normalise_labels(record["labels"])
        metadata = self._normalise_metadata(record["metadata"])
        if not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] > MAX_VERSION:
            raise WorkItemError(f"version must be between 1 and {MAX_VERSION}")
        return WorkItem(
            id=item_id, title=title, status=status, body=body,
            created_at=record["created_at"], updated_at=record["updated_at"], version=record["version"],
            goal_id=goal_id, labels=labels, metadata=metadata,
            schema_version=record["schema_version"],
        )

    @staticmethod
    def _render(record: WorkItem) -> str:
        frontmatter = record.as_dict()
        body = frontmatter.pop("body")
        rendered = f"---\n{_canonical_json(frontmatter)}\n---\n{body}"
        if len(rendered.encode("utf-8")) > MAX_WORK_ITEM_FILE_BYTES:
            raise WorkItemError(f"work item exceeds {MAX_WORK_ITEM_FILE_BYTES} bytes")
        return rendered

    def _read_path(self, path: Path, *, expected_id: str | None = None) -> WorkItem:
        self._assert_safe_path(path, must_exist=True)
        if not path.is_file():
            raise WorkItemNotFoundError(f"Work item does not exist: {path.stem}")
        try:
            self._assert_safe_path(path, must_exist=True)
            if path.stat().st_size > MAX_WORK_ITEM_FILE_BYTES:
                raise WorkItemError(f"work item exceeds {MAX_WORK_ITEM_FILE_BYTES} bytes: {path}")
            # Perform containment validation adjacent to the read.  A bounded
            # byte read prevents a concurrent writer from defeating the size
            # check by growing the file after stat().
            self._assert_safe_path(path, must_exist=True)
            with path.open("rb") as handle:
                raw_bytes = handle.read(MAX_WORK_ITEM_FILE_BYTES + 1)
            if len(raw_bytes) > MAX_WORK_ITEM_FILE_BYTES:
                raise WorkItemError(f"work item exceeds {MAX_WORK_ITEM_FILE_BYTES} bytes: {path}")
            # Hand-authored files may use Windows line endings.  Normalize
            # only CRLF after enforcing the byte limit, so canonical writes
            # remain stable while reads are portable.
            raw = raw_bytes.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError as error:
            raise WorkItemError(f"Work item is not UTF-8: {path}") from error
        except OSError as error:
            raise WorkItemError(f"Unable to read work item: {path}") from error
        if not raw.startswith("---\n"):
            raise WorkItemError(f"Invalid work-item frontmatter: {path}")
        boundary = raw.find("\n---\n", 4)
        if boundary < 0:
            raise WorkItemError(f"Unclosed work-item frontmatter: {path}")
        frontmatter_bytes = raw[4:boundary].encode("utf-8")
        if len(frontmatter_bytes) > MAX_FRONTMATTER_BYTES:
            raise WorkItemError(f"work-item frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes: {path}")
        try:
            frontmatter = json.loads(
                raw[4:boundary],
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
                parse_float=_parse_finite_float,
            )
        except (json.JSONDecodeError, WorkItemError) as error:
            raise WorkItemError(f"Invalid work-item frontmatter JSON: {path}") from error
        if not isinstance(frontmatter, dict):
            raise WorkItemError(f"Work-item frontmatter must be an object: {path}")
        record = {**frontmatter, "body": raw[boundary + 5 :]}
        try:
            item = self._validate_record(record)
        except WorkItemError as error:
            record_id = record.get("id")
            record_goal_id = record.get("goal_id")
            if not is_identifier(record_id) or (
                record_goal_id is not None and not is_identifier(record_goal_id)
            ):
                raise WorkItemError(
                    f"Persisted work item has an invalid identifier; manual migration is required: {path}"
                ) from error
            raise
        if item.id != path.stem or (expected_id is not None and item.id != expected_id):
            raise WorkItemError(f"Work-item id does not match its filename: {path}")
        return item

    def _write_new(self, path: Path, content: str) -> None:
        """Atomically publish a new file without replacing an existing entry."""
        self._assert_safe_path(path)
        if path.exists() or path.is_symlink():
            raise WorkItemExistsError(f"Work item already exists: {path.stem}")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # A hard link is an atomic create-if-absent operation on the same
            # filesystem, so a concurrent create cannot be overwritten.
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(path)
            os.link(temporary, path)
        except FileExistsError as error:
            raise WorkItemExistsError(f"Work item already exists: {path.stem}") from error
        except OSError as error:
            raise WorkItemError(f"Unable to create work item: {path}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _exclusive_update_lock(self, path: Path) -> Iterator[None]:
        lock_path = path.with_name(f".{path.name}.lock")
        self._assert_safe_path(lock_path)
        try:
            self._assert_safe_path(lock_path)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise WorkItemConflictError(f"Work item is already being updated: {path.stem}") from error
        try:
            os.close(descriptor)
            yield
        finally:
            self._assert_safe_path(lock_path)
            lock_path.unlink(missing_ok=True)

    def _replace(self, path: Path, content: str) -> None:
        self._assert_safe_path(path, must_exist=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(path, must_exist=True)
            os.replace(temporary, path)
        except OSError as error:
            raise WorkItemError(f"Unable to update work item: {path}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)

    def create(
        self, *, item_id: str, title: str, body: str = "", status: str = "planned",
        goal_id: str | None = None, labels: list[str] | tuple[str, ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkItem:
        """Create one item. Existing files are never overwritten."""
        if status == "done":
            raise WorkItemError("a work item cannot be created as done; complete its validated workflow first")
        self._ensure_directory()
        item_id = self._validate_identifier(item_id)
        timestamp = _now()
        record = {
            "id": item_id, "title": self._normalise_title(title), "status": self._normalise_status(status),
            "body": self._normalise_body(body), "goal_id": self._normalise_goal_id(goal_id),
            "labels": list(self._normalise_labels(labels)), "metadata": self._normalise_metadata(metadata),
            "created_at": timestamp, "updated_at": timestamp, "version": 1,
            "schema_version": WORK_ITEM_SCHEMA_VERSION,
        }
        item = self._validate_record(record)
        self._write_new(self._path_for(item_id), self._render(item))
        return item

    def read(self, item_id: str) -> WorkItem:
        """Read one item by its slug, never by an arbitrary filesystem path."""
        self._existing_directory()
        item_id = self._validate_identifier(item_id)
        return self._read_path(self._path_for(item_id), expected_id=item_id)

    def list(self) -> list[WorkItemSummary]:
        """Return summaries only; callers must use :meth:`read` for a body."""
        if not self._existing_directory():
            return []
        result: list[WorkItemSummary] = []
        self._assert_safe_path(self.directory, must_exist=True)
        for path in sorted(self.directory.glob("*.md"), key=lambda value: (value.name.casefold(), value.name)):
            self._assert_safe_path(path, must_exist=True)
            if path.is_file():
                result.append(WorkItemSummary.from_item(self._read_path(path)))
        return result

    list_summaries = list

    def update(
        self, item_id: str, *, expected_version: int, title: str | object = _UNSET,
        body: str | object = _UNSET, status: str | object = _UNSET,
        goal_id: str | None | object = _UNSET,
        labels: list[str] | tuple[str, ...] | object = _UNSET,
        metadata: dict[str, Any] | object = _UNSET,
        completion_workflow: Mapping[str, Any] | None = None,
    ) -> WorkItem:
        """Explicitly and safely update an existing item using optimistic versioning."""
        if not isinstance(expected_version, int) or expected_version < 1:
            raise WorkItemError("expected_version must be a positive integer")
        self._existing_directory()
        item_id = self._validate_identifier(item_id)
        path = self._path_for(item_id)
        with self._exclusive_update_lock(path):
            current = self._read_path(path, expected_id=item_id)
            if current.version != expected_version:
                raise WorkItemConflictError(
                    f"Work item version conflict for {item_id}: expected {expected_version}, found {current.version}"
                )
            record = current.as_dict()
            if title is not _UNSET:
                record["title"] = self._normalise_title(title)  # type: ignore[arg-type]
            if body is not _UNSET:
                record["body"] = self._normalise_body(body)  # type: ignore[arg-type]
            if status is not _UNSET:
                record["status"] = self._normalise_status(status)  # type: ignore[arg-type]
            if goal_id is not _UNSET:
                record["goal_id"] = self._normalise_goal_id(goal_id)  # type: ignore[arg-type]
            if labels is not _UNSET:
                record["labels"] = list(self._normalise_labels(labels))  # type: ignore[arg-type]
            if metadata is not _UNSET:
                record["metadata"] = self._normalise_metadata(metadata)  # type: ignore[arg-type]
            if status == "done":
                if completion_workflow is None:
                    raise WorkItemError("status done requires a validated completed workflow")
                try:
                    completion = workflow_completion_token(completion_workflow)
                except WorkflowError as error:
                    raise WorkItemError(str(error)) from error
                source = completion["source"]
                if source["goal_id"] != current.goal_id or source["work_unit_id"] != current.id:
                    raise WorkItemError("completed workflow does not belong to this work item and goal")
                combined_metadata = dict(record["metadata"])
                combined_metadata["workflow_completion"] = completion
                record["metadata"] = self._normalise_metadata(combined_metadata)
            elif completion_workflow is not None:
                raise WorkItemError("completion workflow may only be supplied when status becomes done")
            record["updated_at"] = _now()
            record["version"] = current.version + 1
            updated = self._validate_record(record)
            self._replace(path, self._render(updated))
            return updated


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkItemError(f"duplicate work-item frontmatter key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise WorkItemError(f"non-finite work-item value is not permitted: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_non_finite(value)
    return parsed


def _validate_metadata_value(value: Any, *, depth: int = 0) -> None:
    """Validate metadata before JSON encoding so the same bounds apply on I/O."""
    if depth > MAX_METADATA_DEPTH:
        raise WorkItemError(f"metadata nesting exceeds {MAX_METADATA_DEPTH} levels")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING_CHARS:
            raise WorkItemError(f"metadata strings must not exceed {MAX_METADATA_STRING_CHARS} characters")
        return
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise WorkItemError("metadata integers must be safely representable in JSON consumers")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkItemError("metadata must not contain non-finite numbers")
        return
    if isinstance(value, list):
        if len(value) > MAX_METADATA_LIST_ITEMS:
            raise WorkItemError(f"metadata lists must not exceed {MAX_METADATA_LIST_ITEMS} items")
        for item in value:
            _validate_metadata_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_METADATA_ENTRIES:
            raise WorkItemError(f"metadata objects must not exceed {MAX_METADATA_ENTRIES} entries")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > MAX_METADATA_KEY_CHARS:
                raise WorkItemError("metadata keys must be non-empty strings within the configured bound")
            _validate_metadata_value(item, depth=depth + 1)
        return
    raise WorkItemError("metadata must contain JSON-compatible values")
