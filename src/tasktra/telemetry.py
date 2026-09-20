"""Private, bounded local execution telemetry.

Telemetry is deliberately opt-in.  It stores operational measurements only;
prompt text, source, code, arbitrary metadata, and credential-shaped values are
not part of the record format.  Export is an explicit local filesystem action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping
from uuid import uuid4


TELEMETRY_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 128
DEFAULT_MAX_FILE_BYTES = 1_048_576
MAX_RECORD_BYTES = 4_096
MAX_TOOLS = 16
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EVENT_ID = re.compile(r"^event-(?:[0-9]{3,12}|[a-f0-9]{32})$")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:bearer\s+|(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|"
    r"authorization|password|secret|credential|cookie)\s*[:=]\s*\S+|"
    r"sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})"
)
_PROHIBITED_FIELDS = frozenset({
    "prompt", "prompts", "source", "sources", "code", "context", "message",
    "messages", "secret", "secrets", "password", "credential", "credentials",
    "api_key", "access_token", "refresh_token", "authorization", "cookie",
})
_OUTCOMES = frozenset({"succeeded", "failed", "blocked", "cancelled", "partial", "unknown"})
_VALIDATION_OUTCOMES = frozenset({"passed", "failed", "not-run", "skipped", "unknown"})
_ROLE_IDS = frozenset({
    "coordinator", "goal-steward", "implementer", "operator", "reviewer", "scout",
    "specialist", "tester", "work-selector", "writer", "unknown",
})
_MODEL_TIERS = frozenset({"local", "economy", "balanced", "frontier", "specialist", "unknown"})
_TOOL_IDS = frozenset({
    "browser", "compiler", "database", "filesystem", "git", "http", "other", "provider",
    "pytest", "python", "rg", "shell", "tasktra", "tasktra-cli", "unittest", "validator",
})


class TelemetryError(ValueError):
    """A telemetry record or local telemetry path is unsafe or invalid."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise TelemetryError("telemetry must contain JSON-compatible values") from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TelemetryError(f"duplicate telemetry key: {key}")
        result[key] = value
    return result


def _is_counter(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000_000_000:
        raise TelemetryError(f"{label} must be a non-negative bounded integer")
    return value


def _registered(value: Any, *, label: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed or _CREDENTIAL_VALUE.search(value):
        raise TelemetryError(f"{label} must be a registered non-credential identifier")
    return value


def _event_identifier(value: Any) -> str:
    if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
        raise TelemetryError("event_id must be a generated or numeric event identifier")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise TelemetryError("recorded_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TelemetryError("recorded_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise TelemetryError("recorded_at must include an offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class TelemetryRecord:
    """The closed, versioned measurement format persisted by :class:`TelemetryStore`."""

    event_id: str
    recorded_at: str
    role: str
    model_tier: str
    tools: tuple[str, ...]
    elapsed_ms: int
    retries: int
    evidence_reused: bool
    validation_outcome: str
    human_interventions: int
    final_outcome: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    schema_version: int = TELEMETRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TELEMETRY_SCHEMA_VERSION:
            raise TelemetryError(f"unsupported telemetry schema version: {self.schema_version}")
        object.__setattr__(self, "event_id", _event_identifier(self.event_id))
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at))
        object.__setattr__(self, "role", _registered(self.role, label="role", allowed=_ROLE_IDS))
        object.__setattr__(self, "model_tier", _registered(self.model_tier, label="model_tier", allowed=_MODEL_TIERS))
        if not isinstance(self.tools, tuple) or len(self.tools) > MAX_TOOLS:
            raise TelemetryError(f"tools must contain at most {MAX_TOOLS} identifiers")
        cleaned_tools = tuple(sorted(
            (_registered(tool, label="tool", allowed=_TOOL_IDS) for tool in self.tools),
            key=str.casefold,
        ))
        if len(set(cleaned_tools)) != len(cleaned_tools):
            raise TelemetryError("tools must be unique")
        object.__setattr__(self, "tools", cleaned_tools)
        for field in ("elapsed_ms", "retries", "human_interventions"):
            object.__setattr__(self, field, _is_counter(getattr(self, field), label=field))
        object.__setattr__(self, "input_tokens", _is_counter(self.input_tokens, label="input_tokens"))
        object.__setattr__(self, "output_tokens", _is_counter(self.output_tokens, label="output_tokens"))
        if not isinstance(self.evidence_reused, bool):
            raise TelemetryError("evidence_reused must be boolean")
        if self.validation_outcome not in _VALIDATION_OUTCOMES:
            raise TelemetryError("validation_outcome is not supported")
        if self.final_outcome not in _OUTCOMES:
            raise TelemetryError("final_outcome is not supported")
        if len(_canonical_json(self.as_dict()).encode("utf-8")) > MAX_RECORD_BYTES:
            raise TelemetryError(f"telemetry record exceeds {MAX_RECORD_BYTES} bytes")

    @classmethod
    def create(
        cls, *, role: str, model_tier: str, tools: Iterable[str] = (), elapsed_ms: int = 0,
        retries: int = 0, evidence_reused: bool = False, validation_outcome: str = "not-run",
        human_interventions: int = 0, final_outcome: str = "unknown", input_tokens: int | None = None,
        output_tokens: int | None = None, event_id: str | None = None, recorded_at: str | None = None,
    ) -> "TelemetryRecord":
        return cls(
            event_id=event_id or f"event-{uuid4().hex}", recorded_at=recorded_at or _now(),
            role=role, model_tier=model_tier, tools=tuple(tools), elapsed_ms=elapsed_ms,
            retries=retries, evidence_reused=evidence_reused, validation_outcome=validation_outcome,
            human_interventions=human_interventions, final_outcome=final_outcome,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TelemetryRecord":
        if not isinstance(value, Mapping):
            raise TelemetryError("telemetry record must be an object")
        fields = {field.name for field in __import__("dataclasses").fields(cls)}
        unknown = set(value) - fields
        if unknown:
            unsafe = sorted(key for key in unknown if str(key).casefold() in _PROHIBITED_FIELDS)
            detail = ", ".join(unsafe or sorted(map(str, unknown)))
            raise TelemetryError(f"telemetry does not permit field(s): {detail}")
        required = {"event_id", "recorded_at", "role", "model_tier", "tools", "elapsed_ms", "retries",
                    "evidence_reused", "validation_outcome", "human_interventions", "final_outcome"}
        missing = required - set(value)
        if missing:
            raise TelemetryError(f"telemetry record is missing field(s): {', '.join(sorted(missing))}")
        if not isinstance(value["tools"], (list, tuple)):
            raise TelemetryError("tools must be a list of identifiers")
        return cls(
            event_id=value["event_id"], recorded_at=value["recorded_at"], role=value["role"],
            model_tier=value["model_tier"], tools=tuple(value["tools"]), elapsed_ms=value["elapsed_ms"],
            retries=value["retries"], evidence_reused=value["evidence_reused"],
            validation_outcome=value["validation_outcome"], human_interventions=value["human_interventions"],
            final_outcome=value["final_outcome"], input_tokens=value.get("input_tokens"),
            output_tokens=value.get("output_tokens"), schema_version=value.get("schema_version", TELEMETRY_SCHEMA_VERSION),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "event_id": self.event_id, "recorded_at": self.recorded_at,
            "role": self.role, "model_tier": self.model_tier, "tools": list(self.tools),
            "elapsed_ms": self.elapsed_ms, "retries": self.retries, "evidence_reused": self.evidence_reused,
            "validation_outcome": self.validation_outcome, "human_interventions": self.human_interventions,
            "final_outcome": self.final_outcome, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class TelemetryStore:
    """A bounded local-only telemetry store; collection is disabled by default."""

    def __init__(
        self, project_root: Path | str, *, enabled: bool = False,
        max_records: int = DEFAULT_MAX_RECORDS, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        root = Path(project_root)
        if not root.is_dir():
            raise TelemetryError(f"project root is not a directory: {root}")
        if not isinstance(enabled, bool):
            raise TelemetryError("enabled must be boolean")
        if not isinstance(max_records, int) or isinstance(max_records, bool) or not 1 <= max_records <= 10_000:
            raise TelemetryError("max_records must be between 1 and 10000")
        if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or not MAX_RECORD_BYTES < max_file_bytes <= 16_777_216:
            raise TelemetryError("max_file_bytes is outside the supported bounded range")
        self.project_root = root.resolve(strict=True)
        self.enabled = enabled
        self.max_records = max_records
        self.max_file_bytes = max_file_bytes
        self.directory = self.project_root / ".tasktra" / "telemetry"
        self.path = self.directory / "records-v1.jsonl"

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )

    def _assert_safe_path(self, path: Path, *, must_exist: bool = False) -> None:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as error:
            raise TelemetryError("telemetry path escapes the project root") from error
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if self._is_reparse_point(current):
                raise TelemetryError(f"telemetry path contains a symbolic link or reparse point: {current}")
        try:
            if path.exists() or path.is_symlink():
                resolved = path.resolve(strict=True)
            else:
                if must_exist:
                    raise FileNotFoundError(path)
                parent = path.parent
                while not parent.exists():
                    parent = parent.parent
                resolved = parent.resolve(strict=True) / path.name
            resolved.relative_to(self.project_root)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise TelemetryError(f"telemetry path escapes the project root: {path}") from error
        if self._is_reparse_point(path):
            raise TelemetryError(f"telemetry path contains a symbolic link or reparse point: {path}")

    def _ensure_directory(self) -> None:
        for directory in (self.project_root / ".tasktra", self.directory):
            self._assert_safe_path(directory)
            if directory.exists():
                if not directory.is_dir():
                    raise TelemetryError(f"telemetry directory is not a directory: {directory}")
                continue
            directory.mkdir()
            self._assert_safe_path(directory, must_exist=True)

    def _read_records(self) -> tuple[TelemetryRecord, ...]:
        self._assert_safe_path(self.path)
        if not self.path.exists():
            return ()
        self._assert_safe_path(self.path, must_exist=True)
        if not self.path.is_file():
            raise TelemetryError(f"telemetry storage is not a file: {self.path}")
        try:
            if self.path.stat().st_size > self.max_file_bytes:
                raise TelemetryError(f"telemetry storage exceeds {self.max_file_bytes} bytes")
            self._assert_safe_path(self.path, must_exist=True)
            with self.path.open("rb") as handle:
                raw = handle.read(self.max_file_bytes + 1)
        except OSError as error:
            raise TelemetryError(f"unable to read telemetry storage: {self.path}") from error
        if len(raw) > self.max_file_bytes:
            raise TelemetryError(f"telemetry storage exceeds {self.max_file_bytes} bytes")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise TelemetryError("telemetry storage is not UTF-8") from error
        if len(lines) > self.max_records:
            raise TelemetryError("telemetry storage exceeds the configured record bound")
        records: list[TelemetryRecord] = []
        for line in lines:
            if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                raise TelemetryError(f"telemetry record exceeds {MAX_RECORD_BYTES} bytes")
            try:
                decoded = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, TelemetryError) as error:
                raise TelemetryError("telemetry storage contains invalid JSON") from error
            records.append(TelemetryRecord.from_mapping(decoded))
        return tuple(records)

    def records(self) -> tuple[TelemetryRecord, ...]:
        """Read persisted local records.  This never enables collection or exports data."""
        return self._read_records()

    def _write_records(self, records: Iterable[TelemetryRecord]) -> None:
        retained = list(records)[-self.max_records:]
        while retained:
            payload = "".join(_canonical_json(record.as_dict()) + "\n" for record in retained)
            if len(payload.encode("utf-8")) <= self.max_file_bytes:
                break
            retained.pop(0)
        payload = "".join(_canonical_json(record.as_dict()) + "\n" for record in retained)
        self._ensure_directory()
        self._assert_safe_path(self.path)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(self.path)
            os.replace(temporary, self.path)
        except OSError as error:
            raise TelemetryError(f"unable to write telemetry storage: {self.path}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)

    def append(self, record: TelemetryRecord | Mapping[str, Any]) -> bool:
        """Validate and persist one record when enabled; return whether it was stored."""
        normalized = record if isinstance(record, TelemetryRecord) else TelemetryRecord.from_mapping(record)
        if not self.enabled:
            return False
        self._write_records((*self._read_records(), normalized))
        return True

    def capture(self, **fields: Any) -> TelemetryRecord:
        """Create a closed record and append it if this store was explicitly enabled."""
        record = TelemetryRecord.create(**fields)
        self.append(record)
        return record

    def export_sanitized(self, destination: Path | str) -> Path:
        """Write a canonical sanitized export under this project, explicitly on request."""
        target = Path(destination)
        if not target.is_absolute():
            target = self.project_root / target
        self._assert_safe_path(target)
        if target == self.path:
            raise TelemetryError("export destination must differ from telemetry storage")
        records = self._read_records()
        payload = _canonical_json({
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "records": [record.as_dict() for record in records],
        }) + "\n"
        if len(payload.encode("utf-8")) > self.max_file_bytes:
            raise TelemetryError("sanitized export exceeds the configured telemetry byte bound")
        parent = target.parent
        self._assert_safe_path(parent)
        if not parent.exists():
            parent.mkdir(parents=True)
        self._assert_safe_path(parent, must_exist=True)
        self._assert_safe_path(target)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(target)
            os.replace(temporary, target)
        except OSError as error:
            raise TelemetryError(f"unable to write telemetry export: {target}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)
        return target
