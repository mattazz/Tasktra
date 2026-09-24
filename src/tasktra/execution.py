"""Project-local ledger for supported agent execution receipts.

This module records the small amount of attribution needed to report a work
unit's execution.  It deliberately does not intercept Codex, and it never
stores prompts, messages, logs, or source file paths.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator


_SCHEMA = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASON = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_AGENT_PATH = re.compile(r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_CREDENTIAL = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_-]{12,}|"
    r"AKIA[A-Z0-9]{16}|(?:token|secret|password|credential)[._:-])"
)
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_PROVENANCE = frozenset({"manual-assertion", "host-callback", "rollout-verified"})
_COUNTERS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


class ExecutionError(ValueError):
    """Raised when a project-local execution receipt is incomplete or conflicts."""


def _identifier(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or _CREDENTIAL.search(value):
        raise ExecutionError(f"{label} must be a bounded identifier")
    return value


def _agent_identifier(value: Any, label: str, *, optional: bool = False) -> str | None:
    """Accept an opaque host ID or Tasktra's restricted agent hierarchy."""
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 512 or _CREDENTIAL.search(value):
        raise ExecutionError(f"{label} must be a bounded non-credential agent identifier")
    if _AGENT_PATH.fullmatch(value):
        return value
    return _identifier(value, label)


def _reason(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _REASON.fullmatch(value) or _CREDENTIAL.search(value):
        raise ExecutionError(f"{label} must be a bounded reason code")
    return value


def _profile(model: Any, effort: Any, *, label: str, required: bool = False) -> tuple[str | None, str | None]:
    if (model is None) != (effort is None):
        raise ExecutionError(f"{label} model and effort must be supplied together")
    if required and model is None:
        raise ExecutionError(f"{label} model and effort are required")
    return _identifier(model, f"{label} model", optional=True), _identifier(effort, f"{label} effort", optional=True)


def _profile_mismatch(observed: tuple[str | None, str | None], expected: tuple[str | None, str | None]) -> bool:
    """Compare only dimensions a project configured or explicitly requested."""
    return any(target is not None and actual != target for actual, target in zip(observed, expected))


def _canonical_source_hash(path: Path | str) -> str:
    try:
        source = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ExecutionError("rollout path cannot be resolved") from error
    if not source.is_file():
        raise ExecutionError("rollout path must name a file")
    return sha256(str(source).encode("utf-8")).hexdigest()


def _usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(_COUNTERS):
        raise ExecutionError("rollout usage must contain exactly the six supported counters")
    result: dict[str, int] = {}
    for key in _COUNTERS:
        item = value[key]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ExecutionError("rollout usage counters must be non-negative integers")
        result[key] = item
    return result


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _provenance(value: Any, label: str, *, allowed: frozenset[str] = _PROVENANCE) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ExecutionError(f"{label} is not supported")
    return value


class ExecutionStore:
    """SQLite-backed project-local ledger for planned, started, and terminal work."""

    def __init__(self, project_root: Path | str) -> None:
        root = Path(project_root)
        if not root.is_dir():
            raise ExecutionError("project root is not a directory")
        self.project_root = root.resolve(strict=True)
        self.directory = self.project_root / ".tasktra" / "runtime"
        self.path = self.directory / "agent-execution.sqlite"

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & 0x400
        )

    def _assert_private_path(self, path: Path, *, must_exist: bool = False) -> None:
        """Reject links and junctions before SQLite can traverse them."""
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as error:
            raise ExecutionError("execution ledger path escapes the project root") from error
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if self._is_reparse_point(current):
                raise ExecutionError("execution ledger path contains a symbolic link or reparse point")
        if must_exist and not path.exists():
            raise ExecutionError("execution ledger path does not exist")
        if path.exists() and self._is_reparse_point(path):
            raise ExecutionError("execution ledger path contains a symbolic link or reparse point")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._assert_private_path(self.path, must_exist=self.path.exists())
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        tasktra = self.project_root / ".tasktra"
        for directory in (tasktra, self.directory):
            self._assert_private_path(directory)
            if directory.exists():
                if not directory.is_dir():
                    raise ExecutionError("execution ledger directory is not a directory")
            else:
                directory.mkdir()
            self._assert_private_path(directory, must_exist=True)
        self._assert_private_path(self.path)
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS execution (
                    work_id TEXT PRIMARY KEY, role TEXT NOT NULL,
                    configured_model TEXT, configured_effort TEXT,
                    requested_model TEXT, requested_effort TEXT, override_reason TEXT,
                    parent_work_id TEXT, attribution_reason TEXT,
                    state TEXT NOT NULL,
                    provider TEXT, host TEXT, thread_id TEXT, agent_id TEXT, turn_id TEXT,
                    observed_model TEXT, observed_effort TEXT, fallback_reason TEXT,
                    start_provenance TEXT, finish_provenance TEXT, usage_provenance TEXT,
                    outcome TEXT, unknown_reason TEXT,
                    source_path_sha256 TEXT, source_sha256 TEXT, source_bytes INTEGER,
                    rollout_agent_id TEXT, rollout_model TEXT, rollout_effort TEXT,
                    usage_json TEXT, rollout_schema TEXT, response_count INTEGER,
                    response_fingerprints_json TEXT
                )
            """)
            columns = {item[1] for item in connection.execute("PRAGMA table_info(execution)")}
            additions = {"start_provenance": "TEXT", "finish_provenance": "TEXT", "usage_provenance": "TEXT",
                         "response_fingerprints_json": "TEXT", "source_bytes": "INTEGER"}
            for name, field_type in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE execution ADD COLUMN {name} {field_type}")
            connection.execute("CREATE INDEX IF NOT EXISTS execution_scope ON execution(provider, host, thread_id, turn_id)")

    def _require_database(self) -> None:
        if not self.path.exists():
            raise ExecutionError("work_id is not planned")
        self._assert_private_path(self.path, must_exist=True)
        if not self.path.is_file():
            raise ExecutionError("execution ledger storage is not a file")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        usage_json = value.pop("usage_json", None)
        fingerprints_json = value.pop("response_fingerprints_json", None)
        value["usage"] = json.loads(usage_json) if usage_json is not None else None
        value["response_fingerprints"] = json.loads(fingerprints_json) if fingerprints_json is not None else []
        host_model, host_effort = value["observed_model"], value["observed_effort"]
        host_agent = value["agent_id"]
        # The parser is the most specific observation for an imported scope.
        # If a fresh import has no context, its NULLs clear the prior rollout
        # observation while retaining a separately reported host receipt.
        value["host_observed_model"] = host_model if value["start_provenance"] == "host-callback" else None
        value["host_observed_effort"] = host_effort if value["start_provenance"] == "host-callback" else None
        value["host_agent_id"] = host_agent if value["start_provenance"] == "host-callback" else None
        value["asserted_agent_id"] = host_agent if value["start_provenance"] == "manual-assertion" else None
        value["asserted_observed_model"] = host_model if value["start_provenance"] == "manual-assertion" else None
        value["asserted_observed_effort"] = host_effort if value["start_provenance"] == "manual-assertion" else None
        if value["rollout_agent_id"] is not None:
            value["agent_id"] = value["rollout_agent_id"]
        elif value["start_provenance"] != "host-callback":
            value["agent_id"] = None
        if value["rollout_model"] is not None:
            value["observed_model"] = value["rollout_model"]
            value["observed_effort"] = value["rollout_effort"]
        elif value["start_provenance"] != "host-callback":
            value["observed_model"] = None
            value["observed_effort"] = None
        value["asserted"] = {
            "agent_id": value["asserted_agent_id"], "model": value["asserted_observed_model"],
            "effort": value["asserted_observed_effort"], "start_provenance": value["start_provenance"],
            "finish_provenance": value["finish_provenance"],
        }
        value["verified"] = {
            "agent_id": value["agent_id"], "model": value["observed_model"], "effort": value["observed_effort"],
            "usage": value["usage"], "usage_provenance": value["usage_provenance"],
            "provenance": ("rollout-verified" if value["source_sha256"] is not None
                           else "host-callback" if value["start_provenance"] == "host-callback" else None),
        }
        # Hashes are attribution values; a raw source path is never a column.
        return copy.deepcopy(value)

    @staticmethod
    def _same(row: sqlite3.Row, values: dict[str, Any], fields: tuple[str, ...]) -> bool:
        return all(row[name] == values[name] for name in fields)

    def _row(self, connection: sqlite3.Connection, work_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM execution WHERE work_id = ?", (work_id,)).fetchone()
        if row is None:
            raise ExecutionError("work_id is not planned")
        return row

    def plan(self, work_id: str, role: str, configured_model: str | None, configured_effort: str | None,
             requested_model: str | None = None, requested_effort: str | None = None,
             override_reason: str | None = None, parent_work_id: str | None = None,
             attribution_reason: str | None = None) -> dict[str, Any]:
        work_id = _identifier(work_id, "work_id")
        role = _identifier(role, "role")
        configured_model = _identifier(configured_model, "configured model", optional=True)
        configured_effort = _identifier(configured_effort, "configured effort", optional=True)
        # A requester may override either dimension.  A host receipt is
        # different: actual model and effort are inseparable evidence.
        requested_model = _identifier(requested_model, "requested model", optional=True)
        requested_effort = _identifier(requested_effort, "requested effort", optional=True)
        if requested_model is not None or requested_effort is not None:
            override_reason = _reason(override_reason, "override_reason", required=True)
        elif override_reason is not None:
            raise ExecutionError("override_reason requires a requested profile")
        parent_work_id = _identifier(parent_work_id, "parent_work_id", optional=True)
        attribution_reason = _reason(attribution_reason, "attribution_reason")
        if role == "coordinator" and attribution_reason is None:
            raise ExecutionError("coordinator role requires attribution_reason")
        values = {"work_id": work_id, "role": role, "configured_model": configured_model,
                  "configured_effort": configured_effort, "requested_model": requested_model,
                  "requested_effort": requested_effort, "override_reason": override_reason,
                  "parent_work_id": parent_work_id, "attribution_reason": attribution_reason}
        fields = tuple(values)
        self._initialize()
        with self._connection() as connection:
            if parent_work_id is not None and connection.execute("SELECT 1 FROM execution WHERE work_id = ?", (parent_work_id,)).fetchone() is None:
                raise ExecutionError("parent_work_id is not planned")
            old = connection.execute("SELECT * FROM execution WHERE work_id = ?", (work_id,)).fetchone()
            if old is not None:
                if self._same(old, values, fields):
                    return self._public(old)
                raise ExecutionError("work_id is immutable once planned")
            connection.execute("""INSERT INTO execution
                (work_id, role, configured_model, configured_effort, requested_model,
                 requested_effort, override_reason, parent_work_id, attribution_reason, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')""",
                tuple(values[name] for name in fields))
            return self._public(self._row(connection, work_id))

    @staticmethod
    def _overlaps(existing_turn: str | None, requested_turn: str | None) -> bool:
        return existing_turn is None or requested_turn is None or existing_turn == requested_turn

    def start(self, work_id: str, provider: str, host: str, thread_id: str,
              agent_id: str | None = None, turn_id: str | None = None,
              observed_model: str | None = None, observed_effort: str | None = None,
              fallback_reason: str | None = None, provenance: str = "manual-assertion") -> dict[str, Any]:
        work_id = _identifier(work_id, "work_id")
        provider, host, thread_id = (_identifier(provider, "provider"), _identifier(host, "host"), _identifier(thread_id, "thread_id"))
        agent_id, turn_id = _agent_identifier(agent_id, "agent_id", optional=True), _identifier(turn_id, "turn_id", optional=True)
        observed_model, observed_effort = _profile(observed_model, observed_effort, label="observed")
        fallback_reason = _reason(fallback_reason, "fallback_reason")
        provenance = _provenance(provenance, "start_provenance", allowed=frozenset({"manual-assertion", "host-callback"}))
        receipt = {"provider": provider, "host": host, "thread_id": thread_id, "agent_id": agent_id,
                   "turn_id": turn_id, "observed_model": observed_model, "observed_effort": observed_effort,
                   "fallback_reason": fallback_reason, "start_provenance": provenance}
        self._require_database()
        with self._connection() as connection:
            row = self._row(connection, work_id)
            if row["state"] != "planned":
                if self._same(row, receipt, tuple(receipt)):
                    return self._public(row)
                raise ExecutionError("start receipt conflicts with immutable work scope")
            expected = (row["requested_model"] or row["configured_model"], row["requested_effort"] or row["configured_effort"])
            mismatch = ((observed_model, observed_effort) != (None, None)
                        and _profile_mismatch((observed_model, observed_effort), expected))
            if mismatch and fallback_reason is None:
                raise ExecutionError("observed profile differs from requested profile and needs fallback_reason")
            if fallback_reason is not None and not mismatch:
                raise ExecutionError("fallback_reason requires an observed profile mismatch")
            collisions = connection.execute("""SELECT work_id, turn_id FROM execution
                WHERE work_id != ? AND provider = ? AND host = ? AND thread_id = ? AND state != 'planned'""",
                (work_id, provider, host, thread_id)).fetchall()
            if any(self._overlaps(item["turn_id"], turn_id) for item in collisions):
                raise ExecutionError("provider host thread scope overlaps another work_id")
            connection.execute("""UPDATE execution SET state='started', provider=?, host=?, thread_id=?,
                agent_id=?, turn_id=?, observed_model=?, observed_effort=?, fallback_reason=?, start_provenance=? WHERE work_id=?""",
                (provider, host, thread_id, agent_id, turn_id, observed_model, observed_effort, fallback_reason, provenance, work_id))
            return self._public(self._row(connection, work_id))

    @staticmethod
    def _prefix_digest(path: Path, length: int) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            remaining = length
            while remaining:
                chunk = handle.read(min(65_536, remaining))
                if not chunk:
                    raise ExecutionError("rollout refresh removed prior evidence")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()

    def _import_in_connection(self, connection: sqlite3.Connection, work_id: str, path: Path | str,
                              fallback_reason: str | None = None) -> dict[str, Any]:
        """Scan and apply one rollout while the caller owns the transaction."""
        fallback_reason = _reason(fallback_reason, "fallback_reason")
        source = Path(path)
        source_path_sha256 = _canonical_source_hash(source)
        row = self._row(connection, work_id)
        if row["state"] == "planned":
            raise ExecutionError("planned work cannot import rollout usage")
        if row["provider"] != "codex":
            raise ExecutionError("Codex rollout usage requires a Codex provider receipt")
        if row["source_path_sha256"] is not None and row["source_path_sha256"] != source_path_sha256:
            raise ExecutionError("rollout source scope is immutable for this work_id")
        if row["source_sha256"] is not None and row["source_bytes"] is None:
            raise ExecutionError("older rollout import lacks an append-only baseline")
        if row["source_bytes"] is not None:
            try:
                previous_bytes = int(row["source_bytes"])
            except (TypeError, ValueError) as error:
                raise ExecutionError("stored rollout byte length is invalid") from error
            if previous_bytes < 0 or self._prefix_digest(source, previous_bytes) != row["source_sha256"]:
                raise ExecutionError("rollout refresh is not append-only")
        try:
            from .codex_usage import RolloutUsageError, scan_rollout
            observation = scan_rollout(source, row["thread_id"], row["turn_id"])
        except ImportError as error:
            raise ExecutionError("Codex rollout parser is unavailable") from error
        except RolloutUsageError as error:
            raise ExecutionError("rollout usage could not be read") from error
        if observation.thread_id != row["thread_id"] or observation.turn_id != row["turn_id"]:
            raise ExecutionError("rollout scope differs from started work")
        rollout_agent = _agent_identifier(observation.agent_id, "rollout agent_id", optional=True)
        rollout_model, rollout_effort = _profile(observation.model, observation.effort, label="rollout observed")
        if (row["start_provenance"] == "host-callback" and row["agent_id"] is not None
                and rollout_agent is not None and row["agent_id"] != rollout_agent):
            raise ExecutionError("rollout agent_id conflicts with started receipt")
        if (row["start_provenance"] == "host-callback" and row["observed_model"] is not None
                and (rollout_model, rollout_effort) != (None, None)
                and (row["observed_model"], row["observed_effort"]) != (rollout_model, rollout_effort)):
            raise ExecutionError("rollout observed profile conflicts with started receipt")
        expected = (row["requested_model"] or row["configured_model"], row["requested_effort"] or row["configured_effort"])
        mismatch = ((rollout_model, rollout_effort) != (None, None) and _profile_mismatch((rollout_model, rollout_effort), expected))
        effective_fallback = fallback_reason or row["fallback_reason"]
        if mismatch and effective_fallback is None:
            raise ExecutionError("rollout observed profile differs from requested profile and needs fallback_reason")
        if fallback_reason is not None and not mismatch and row["fallback_reason"] is None:
            raise ExecutionError("fallback_reason requires an observed profile mismatch")
        usage = _usage(observation.usage)
        source_sha256 = _identifier(observation.source_sha256, "source_sha256")
        schema = str(observation.schema)
        if schema not in {"token_usage_record", "event_msg/token_count", "none"}:
            raise ExecutionError("rollout schema is not supported")
        if row["rollout_schema"] not in (None, "none", schema):
            raise ExecutionError("rollout schema cannot switch during refresh")
        fingerprints = tuple(observation.response_fingerprints)
        if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in fingerprints):
            raise ExecutionError("rollout response fingerprints are invalid")
        old_fingerprints = set(json.loads(row["response_fingerprints_json"]) if row["response_fingerprints_json"] else [])
        if not old_fingerprints.issubset(fingerprints):
            raise ExecutionError("rollout refresh removed or changed prior responses")
        old_usage = json.loads(row["usage_json"]) if row["usage_json"] else None
        if schema == "event_msg/token_count" and old_usage is not None and usage is not None and any(usage[key] < old_usage[key] for key in _COUNTERS):
            raise ExecutionError("legacy rollout usage cannot decrease during refresh")
        unknown_reason = None if row["state"] in _TERMINAL and usage is not None else row["unknown_reason"]
        if row["state"] in _TERMINAL and usage is None and unknown_reason is None:
            raise ExecutionError("terminal work with unknown usage requires unknown_reason")
        # A missing context in a verified append clears stale rollout profile;
        # agent identity is retained when the new suffix has no metadata claim.
        stored_agent = rollout_agent if rollout_agent is not None else row["rollout_agent_id"]
        connection.execute("""UPDATE execution SET source_path_sha256=?, source_sha256=?, source_bytes=?, rollout_agent_id=?,
            rollout_model=?, rollout_effort=?, usage_json=?, usage_provenance=?, rollout_schema=?,
            response_count=?, response_fingerprints_json=? WHERE work_id=?""",
            (source_path_sha256, source_sha256, observation.source_bytes, stored_agent, rollout_model, rollout_effort,
             _json(usage) if usage is not None else None, "rollout-verified" if usage is not None else None,
             schema, observation.response_count, _json(list(fingerprints)), work_id))
        if fallback_reason is not None and row["fallback_reason"] is None:
            connection.execute("UPDATE execution SET fallback_reason=? WHERE work_id=?", (fallback_reason, work_id))
        if unknown_reason != row["unknown_reason"]:
            connection.execute("UPDATE execution SET unknown_reason=? WHERE work_id=?", (unknown_reason, work_id))
        return self._public(self._row(connection, work_id))

    def import_codex_rollout(self, work_id: str, path: Path | str,
                             fallback_reason: str | None = None) -> dict[str, Any]:
        """Import one append-only rollout refresh."""
        work_id = _identifier(work_id, "work_id")
        self._require_database()
        with self._connection() as connection:
            return self._import_in_connection(connection, work_id, path, fallback_reason)

    def finish(self, work_id: str, outcome: str, unknown_reason: str | None = None,
               rollout_path: Path | str | None = None, fallback_reason: str | None = None,
               provenance: str = "manual-assertion") -> dict[str, Any]:
        work_id = _identifier(work_id, "work_id")
        if outcome not in _TERMINAL:
            raise ExecutionError("outcome must be succeeded, failed, or cancelled")
        unknown_reason = _reason(unknown_reason, "unknown_reason")
        provenance = _provenance(provenance, "finish_provenance", allowed=frozenset({"manual-assertion", "host-callback"}))
        if rollout_path is None and fallback_reason is not None:
            raise ExecutionError("fallback_reason requires rollout_path")
        self._require_database()
        with self._connection() as connection:
            if rollout_path is not None:
                self._import_in_connection(connection, work_id, rollout_path, fallback_reason)
            row = self._row(connection, work_id)
            if row["state"] == "planned":
                raise ExecutionError("planned work cannot finish")
            if row["state"] in _TERMINAL:
                if (row["outcome"] == outcome and row["unknown_reason"] == unknown_reason
                        and row["finish_provenance"] == provenance):
                    return self._public(row)
                raise ExecutionError("terminal work is immutable")
            if row["usage_json"] is None and unknown_reason is None:
                raise ExecutionError("unknown usage requires unknown_reason")
            if row["usage_json"] is not None and unknown_reason is not None:
                raise ExecutionError("unknown_reason requires unknown usage")
            connection.execute("UPDATE execution SET state=?, outcome=?, unknown_reason=?, finish_provenance=? WHERE work_id=?",
                               (outcome, outcome, unknown_reason, provenance, work_id))
            return self._public(self._row(connection, work_id))

    def get(self, work_id: str) -> dict[str, Any]:
        work_id = _identifier(work_id, "work_id")
        self._require_database()
        with self._connection() as connection:
            return self._public(self._row(connection, work_id))

    def report(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"kind": "tasktra.agent-execution-report", "schema_version": _SCHEMA,
                    "work_count": 0, "executions": [], "asserted_executions": [], "verified_executions": []}
        self._require_database()
        with self._connection() as connection:
            records = [self._public(row) for row in connection.execute("SELECT * FROM execution ORDER BY work_id")]
        asserted = [{"work_id": item["work_id"], **item["asserted"]} for item in records
                    if (item["start_provenance"] == "manual-assertion"
                        or item["finish_provenance"] == "manual-assertion")]
        verified = [{"work_id": item["work_id"], **item["verified"]} for item in records
                    if item["verified"]["provenance"] is not None]
        return {"kind": "tasktra.agent-execution-report", "schema_version": _SCHEMA,
                "work_count": len(records), "executions": records,
                "asserted_executions": asserted, "verified_executions": verified}


class HostExecutionAdapter:
    """Small callback facade for hosts that explicitly report dispatch receipts.

    It is intentionally passive: native Codex calls remain outside Tasktra.
    """

    def __init__(self, store: ExecutionStore) -> None:
        if not isinstance(store, ExecutionStore):
            raise ExecutionError("store must be an ExecutionStore")
        self.store = store

    def started(self, work_id: str, **receipt: Any) -> dict[str, Any]:
        return self.store.start(work_id, provenance="host-callback", **receipt)

    def finished(self, work_id: str, outcome: str, *, unknown_reason: str | None = None,
                 rollout_path: Path | str | None = None, fallback_reason: str | None = None) -> dict[str, Any]:
        return self.store.finish(work_id, outcome, unknown_reason, rollout_path, fallback_reason,
                                 provenance="host-callback")
