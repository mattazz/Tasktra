"""Durable local runtime state backed by SQLite.

Stage 1 deliberately records *planned* work only. It does not activate goals
or lease work: those transitions require the Stage 3 authority contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import zip_longest
import json
import os
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

from .identifiers import IdentifierError, require_identifier, require_optional_identifier

SCHEMA_VERSION = 15
# The broader lifecycle belongs to the Stage 3 goal engine. Retaining only
# planned state prevents an incomplete authority envelope from authorizing work.
GOAL_STATUSES = {"planned", "active", "draining", "paused", "blocked", "complete", "stopped"}
WORK_UNIT_STATUSES = {"planned", "eligible", "leased", "retry-wait", "blocked", "approval-required", "failed", "exhausted", "complete", "paused", "stopped"}
VERIFICATION_POLICIES = {
    "implementation-review", "implementation-deterministic-review", "research-review", "documentation-review", "deterministic-direct",
}
APPROVAL_DECISIONS = {"approved", "rejected", "needs_human_review"}
_LEGACY_IDENTITY = "legacy-unattributed"
_GENESIS_HASH = "0" * 64


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

# These tables are the durable facts that can change what Tasktra is allowed
# to do or what it believes has happened.  The audit log explains transitions;
# current-state seals make an out-of-band SQLite update, insert, or delete
# visible before another transition can rely on it.
_AUTHORITATIVE_TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "goals": ("id",),
    "budgets": ("goal_id",),
    "work_units": ("id",),
    "work_unit_dependencies": ("work_unit_id", "prerequisite_id"),
    "work_attempts": ("id",),
    "schedule_resume_idempotency": ("idempotency_key",),
    "workflow_evidence": ("work_unit_id",),
    "acceptance_evidence": ("goal_id", "criterion_id"),
    "effect_intents": ("idempotency_key",),
    "effect_receipts": ("id",),
    "effect_attempts": ("id",),
    "effect_receipt_events": ("id",),
    "runtime_control": ("id",),
    "goal_contracts": ("goal_id",),
    "transition_approvals": ("id",),
    "goal_checkpoints": ("goal_id", "checkpoint_id"),
    "intervention_requests": ("id",),
    "intervention_responses": ("id",),
    "intervention_response_heads": ("request_id",),
    "intervention_closures": ("id",),
    "codex_run_preparations": ("id",),
    "codex_run_starts": ("run_id",),
    "codex_run_finishes": ("run_id",),
}
_STATE_MANIFEST_TABLE = "tasktra.current-state"
_STATE_MANIFEST_ID = "all-authoritative-rows"
_PROVIDER_SECRET_MARKERS = (
    "token", "secret", "password", "credential", "authorization", "cookie", "api_key",
)
_PROVIDER_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|(?:access[-_ ]?token|api[-_ ]?key|authorization|password|secret|cookie|credential|token)\s*[:=]\s*)\S+"
)


class StateError(ValueError):
    pass


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise StateError(str(error)) from error


def _optional_identifier(value: Any, *, label: str) -> str | None:
    try:
        return require_optional_identifier(value, label=label)
    except IdentifierError as error:
        raise StateError(str(error)) from error


def _persisted_identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise StateError(
            f"Persisted {label} is invalid; manual migration is required ({error})"
        ) from error


def _persisted_optional_identifier(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _persisted_identifier(value, label=label)


def validate_observed_usage_evidence(value: Any, *, tokens_consumed: int) -> dict[str, Any]:
    """Validate a coordinator's bounded usage attestation for debt settlement.

    This state-level API validates the attestation's shape and its binding to
    the settled total.  It cannot prove that the execution IDs exist or that
    their host callbacks measured the stated usage; the execution store must
    independently perform that verification before a coordinator calls this
    API.  Model text is never usage evidence.
    """
    if not isinstance(value, Mapping):
        raise StateError("observed usage evidence must be an object")
    required = {"source", "execution_ids", "total_tokens"}
    if set(value) != required or value.get("source") != "coordinator-attested":
        raise StateError("observed usage evidence must be coordinator-attested with exact fields")
    total = value.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise StateError("observed usage total_tokens must be a non-negative integer")
    if total != tokens_consumed:
        raise StateError("observed usage total_tokens must equal tokens_consumed")
    execution_ids = value.get("execution_ids")
    if not isinstance(execution_ids, list) or not 1 <= len(execution_ids) <= 32:
        raise StateError("observed usage evidence requires 1 to 32 execution_ids")
    normalized: list[str] = []
    for execution_id in execution_ids:
        normalized.append(_identifier(execution_id, label="observed execution_id"))
    if len(set(normalized)) != len(normalized):
        raise StateError("observed usage execution_ids must be distinct")
    return {"source": "coordinator-attested", "execution_ids": normalized, "total_tokens": total}


def unmeasured_usage_evidence(*, charged_tokens: int, reason: str) -> dict[str, Any]:
    """Describe a conservative reservation charge without claiming a measurement."""
    if not isinstance(charged_tokens, int) or isinstance(charged_tokens, bool) or charged_tokens < 0:
        raise StateError("unmeasured usage charge must be a non-negative integer")
    if not isinstance(reason, str) or not reason:
        raise StateError("unmeasured usage reason must be non-empty")
    return {
        "source": "reservation-charge", "state": "unmeasured",
        "charged_tokens": charged_tokens, "reason": reason,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: str | datetime | None) -> str:
    """Normalize injected UTC clocks; a value equal to expiry is expired."""
    if value is None:
        return _now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StateError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise StateError("timestamp must be ISO-8601 with an offset") from error
    if parsed.tzinfo is None:
        raise StateError("timestamps must include an offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _audit_hash(previous_hash: str, sequence: int, event_type: str, goal_id: str | None,
                work_unit_id: str | None, payload: str, created_at: str) -> str:
    value = _encode({"previous_hash": previous_hash, "sequence": sequence,
                     "event_type": event_type, "goal_id": goal_id, "work_unit_id": work_unit_id,
                     "payload": json.loads(payload),
                     "created_at": created_at})
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority_row_hash(table: str, row: sqlite3.Row | dict[str, Any]) -> str:
    """Hash all persisted authority-bearing columns, excluding no mutable fields."""
    values = dict(row)
    return hashlib.sha256(_encode({"table": table, "row": values}).encode("utf-8")).hexdigest()


def _attempt_binding_sha256(row: sqlite3.Row | dict[str, Any] | None) -> str:
    if row is None:
        return "missing"
    values = dict(row)
    keys = ("id", "work_unit_id", "attempt_no", "owner_id", "lease_generation", "lease_token_hash",
            "repository", "revision", "branch", "workspace", "acquired_at")
    return hashlib.sha256(_encode({key: values[key] for key in keys}).encode("utf-8")).hexdigest()


def _decode(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _acceptance_checks(value: Any, *, required: bool) -> list[list[str]]:
    """Validate a bounded, canonical list of configured command argv arrays."""
    if value is None:
        if required:
            raise StateError("implementation-deterministic-review requires nonempty acceptance_checks")
        return []
    if not isinstance(value, list) or (required and not value) or len(value) > 32:
        raise StateError("acceptance_checks must be a bounded nonempty list for implementation-deterministic-review")
    normalized: list[list[str]] = []
    for command in value:
        if not isinstance(command, list) or not command or len(command) > 32:
            raise StateError("each acceptance check must be a nonempty bounded argv array")
        if not all(isinstance(argument, str) and argument and len(argument) <= 4096 for argument in command):
            raise StateError("acceptance check argv values must be nonempty bounded strings")
        normalized.append(list(command))
    if len({_encode(command) for command in normalized}) != len(normalized):
        raise StateError("acceptance_checks must be distinct")
    return normalized


def validate_deterministic_review_completion_evidence(
    workflow: Mapping[str, Any], acceptance_checks: Any, evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the short-policy proof bound to the reviewed patch and commands.

    The caller may retain additional outcome data, but no boolean or prose is
    accepted as a substitute for exact passed command receipts and the patch
    digest the independent reviewer received.
    """
    checks = _acceptance_checks(acceptance_checks, required=True)
    if not isinstance(evidence, Mapping):
        raise StateError("implementation-deterministic-review requires completion evidence")
    validation, artifacts = evidence.get("validation"), evidence.get("artifacts")
    if not isinstance(validation, list) or not validation or len(validation) > 64:
        raise StateError("completion evidence requires bounded validation receipts")
    if len(validation) % len(checks):
        raise StateError("validation receipts must contain complete configured acceptance-check groups")
    if not isinstance(artifacts, Mapping):
        raise StateError("completion evidence requires artifacts")
    patch_sha256 = artifacts.get("patch_sha256")
    if not isinstance(patch_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", patch_sha256):
        raise StateError("completion evidence artifacts.patch_sha256 must be a lowercase SHA-256")
    normalized_validation: list[dict[str, Any]] = []
    for index, receipt in enumerate(validation):
        expected = checks[index % len(checks)]
        if not isinstance(receipt, Mapping):
            raise StateError("validation receipt must be an object")
        if receipt.get("argv") != expected:
            raise StateError("validation receipt argv does not match immutable acceptance_checks")
        if receipt.get("status") != "passed" or receipt.get("exit_code") != 0:
            raise StateError("validation receipt must be passed with exit_code 0")
        if receipt.get("patch_sha256") != patch_sha256:
            raise StateError("validation receipt patch_sha256 does not match final artifacts")
        normalized_validation.append({
            "argv": list(expected), "status": "passed", "exit_code": 0,
            "patch_sha256": patch_sha256,
        })
    try:
        final_handoff = workflow["accepted_handoffs"][-1]
        references = final_handoff["evidence_refs"]
    except (KeyError, IndexError, TypeError) as error:
        raise StateError("completion workflow lacks final reviewer evidence") from error
    digests = {
        item.get("id"): item.get("locator")
        for item in references if isinstance(item, Mapping)
    }
    for identifier in ("result-digest", "patch-digest"):
        digest = digests.get(identifier)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise StateError(f"completion workflow lacks a valid {identifier}")
    if digests["patch-digest"] != patch_sha256:
        raise StateError("completion evidence patch_sha256 does not match reviewed patch-digest")
    return {"validation": normalized_validation, "artifacts": {"patch_sha256": patch_sha256}}


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("scope", "resource_scope", "provenance", "authority", "acceptance", "metadata", "receipt", "contract", "evidence", "payload", "observation", "acceptance_checks", "completion_evidence_json"):
        if key in result:
            result[key] = _decode(result[key], {} if key in {"scope", "resource_scope", "provenance", "authority", "metadata", "receipt", "observation"} else [])
    return result


def _provider_observation_is_sanitized(value: Any, key: str = "") -> bool:
    """Reject credential-shaped durable provider observations during attestation."""
    def secret_text(text: str) -> bool:
        if _PROVIDER_SECRET_VALUE.search(text):
            return True
        parsed = urlparse(text)
        return parsed.scheme in {"http", "https"} and (
            parsed.username is not None
            or parsed.password is not None
            or any(
                any(marker in query_key.casefold() for marker in _PROVIDER_SECRET_MARKERS)
                for query_key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        )

    # Sanitized evidence replaces a dangerous key with [redacted-key]; an
    # unredacted sensitive key is never valid durable evidence.
    if any(marker in key.casefold() for marker in _PROVIDER_SECRET_MARKERS) or secret_text(key):
        return False
    if isinstance(value, dict):
        return all(
            isinstance(child_key, str)
            and _provider_observation_is_sanitized(child, child_key)
            for child_key, child in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return all(_provider_observation_is_sanitized(child) for child in value)
    if isinstance(value, str):
        if secret_text(value):
            return False
    return True


class StateStore:
    """A transactional local ledger with durable, auditable state transitions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def _connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Open one explicit transaction, serializing writers including migration."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, isolation_level=None)
        else:
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield connection
            # _prepare_write marks a transaction only after it has verified
            # the previous sealed state.  This makes sealing part of the same
            # atomic commit as the legitimate mutation, without allowing a
            # migration or an arbitrary internal connection to bless rows.
            if write and StateStore._seal_on_commit(connection):
                StateStore._seal_current_state_in_transaction(connection, _now())
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate(connection: sqlite3.Connection, *, create_backup: bool = True) -> int:
        """Apply all schema changes on the caller's transaction.

        ``executescript`` is intentionally avoided: it can issue implicit
        commits and split a migration from the state mutation that required it.
        """
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise StateError(f"Database schema {current} is newer than supported {SCHEMA_VERSION}")
        if 0 < current < SCHEMA_VERSION:
            StateStore._assert_known_pre15_lineage_in_transaction(connection, current)
        if create_backup and 0 < current < SCHEMA_VERSION:
            StateStore._backup_before_migration(connection, current)
        if current < 1:
            statements = (
                """CREATE TABLE goals (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL, priority INTEGER NOT NULL, authority TEXT NOT NULL,
                    acceptance TEXT NOT NULL, budget_tokens INTEGER, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                """CREATE TABLE work_units (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
                    title TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL,
                    lease_holder TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                """CREATE TABLE approvals (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
                    work_unit_id TEXT REFERENCES work_units(id), decision TEXT NOT NULL,
                    authority_clause TEXT NOT NULL, rationale TEXT NOT NULL,
                    receipt TEXT NOT NULL, created_at TEXT NOT NULL
                )""",
                """CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT REFERENCES goals(id),
                    work_unit_id TEXT REFERENCES work_units(id), event_type TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                )""",
                """CREATE TABLE budgets (
                    goal_id TEXT PRIMARY KEY REFERENCES goals(id), total_tokens INTEGER,
                    allocated_tokens INTEGER NOT NULL DEFAULT 0, consumed_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )""",
                "CREATE INDEX work_units_goal_status ON work_units(goal_id, status)",
                "CREATE INDEX events_goal_created ON events(goal_id, created_at)",
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")
            current = 1
        if current < 2:
            # Pre-v2 approvals are retained for audit but cannot satisfy a
            # separation-of-duty check because the actors are unknown.
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN approver_id TEXT NOT NULL "
                f"DEFAULT '{_LEGACY_IDENTITY}'"
            )
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN performer_id TEXT NOT NULL "
                f"DEFAULT '{_LEGACY_IDENTITY}'"
            )
            connection.execute("PRAGMA user_version = 2")
            current = 2
        if current < 3:
            # v2 records stay historical: neither planned goals nor old
            # approvals are silently promoted into authority.
            # Some early test/dev ledgers contained only the table under test.
            # Complete their missing v1 support tables before extending them.
            for statement in (
                "CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned', priority INTEGER NOT NULL DEFAULT 0, authority TEXT NOT NULL DEFAULT '{}', acceptance TEXT NOT NULL DEFAULT '[]', budget_tokens INTEGER, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS work_units (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), title TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL, lease_holder TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT REFERENCES goals(id), work_unit_id TEXT REFERENCES work_units(id), event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS budgets (goal_id TEXT PRIMARY KEY REFERENCES goals(id), total_tokens INTEGER, allocated_tokens INTEGER NOT NULL DEFAULT 0, consumed_tokens INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '')",
            ):
                connection.execute(statement)
            for column, definition in (
                ("current_attempt_id", "TEXT"), ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("retry_at", "TEXT"), ("last_outcome_class", "TEXT"),
            ):
                if column not in {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}:
                    connection.execute(f"ALTER TABLE work_units ADD COLUMN {column} {definition}")
            for column, definition in (
                ("total_attempts", "INTEGER"), ("consumed_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("total_elapsed_ms", "INTEGER"), ("consumed_elapsed_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("max_concurrency", "INTEGER"), ("reserved_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in {row[1] for row in connection.execute("PRAGMA table_info(budgets)")}:
                    connection.execute(f"ALTER TABLE budgets ADD COLUMN {column} {definition}")
            statements = (
                "CREATE TABLE goal_contracts (goal_id TEXT PRIMARY KEY REFERENCES goals(id), version TEXT NOT NULL, contract TEXT NOT NULL, envelope_sha256 TEXT NOT NULL, defined_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE transition_approvals (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), work_unit_id TEXT REFERENCES work_units(id), action TEXT NOT NULL, effect TEXT, envelope_sha256 TEXT NOT NULL, scope TEXT NOT NULL, decision TEXT NOT NULL, approver_kind TEXT NOT NULL, approver_id TEXT NOT NULL, performer_id TEXT NOT NULL, authority_clause TEXT NOT NULL, evidence TEXT NOT NULL, valid_until TEXT, revoked_at TEXT, revoked_by TEXT, created_at TEXT NOT NULL)",
                "CREATE TABLE work_attempts (id TEXT PRIMARY KEY, work_unit_id TEXT NOT NULL REFERENCES work_units(id), attempt_no INTEGER NOT NULL, owner_id TEXT NOT NULL, lease_generation INTEGER NOT NULL, lease_token_hash TEXT NOT NULL, repository TEXT, revision TEXT, branch TEXT, workspace TEXT, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, outcome_class TEXT, outcome_json TEXT, tokens_reserved INTEGER NOT NULL DEFAULT 0, tokens_consumed INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER NOT NULL DEFAULT 0, UNIQUE(work_unit_id,attempt_no))",
                "CREATE TABLE workflow_evidence (work_unit_id TEXT PRIMARY KEY REFERENCES work_units(id), workflow_json TEXT NOT NULL, workflow_sha256 TEXT NOT NULL, completion_token_json TEXT, recorded_at TEXT NOT NULL)",
                "CREATE TABLE acceptance_evidence (goal_id TEXT NOT NULL REFERENCES goals(id), criterion_id TEXT NOT NULL, evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(goal_id,criterion_id))",
                "CREATE TABLE effect_intents (idempotency_key TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), work_unit_id TEXT REFERENCES work_units(id), effect_class TEXT NOT NULL, operation TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE effect_receipts (id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE REFERENCES effect_intents(idempotency_key), outcome TEXT NOT NULL, before_sha256 TEXT, after_sha256 TEXT, evidence_json TEXT NOT NULL, performed_by TEXT NOT NULL, recorded_at TEXT NOT NULL)",
                "CREATE TABLE audit_events (sequence INTEGER PRIMARY KEY, legacy_event_id INTEGER REFERENCES events(id), goal_id TEXT REFERENCES goals(id), work_unit_id TEXT REFERENCES work_units(id), event_type TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
                "CREATE TABLE runtime_control (id INTEGER PRIMARY KEY CHECK(id=1), emergency_stopped INTEGER NOT NULL DEFAULT 0, reason TEXT, set_by TEXT, set_at TEXT, cleared_by TEXT, cleared_at TEXT)",
                "CREATE INDEX transition_approvals_lookup ON transition_approvals(goal_id,action,performer_id,valid_until)",
                "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END",
                "CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END",
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO runtime_control(id) VALUES(1)")
            previous = _GENESIS_HASH
            for event in connection.execute("SELECT id,goal_id,work_unit_id,event_type,payload,created_at FROM events ORDER BY id"):
                sequence = int(event["id"])
                digest = _audit_hash(previous, sequence, event["event_type"], event["goal_id"], event["work_unit_id"], event["payload"], event["created_at"])
                connection.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)", (sequence, sequence, event["goal_id"], event["work_unit_id"], event["event_type"], event["payload"], previous, digest, event["created_at"]))
                previous = digest
            connection.execute("PRAGMA user_version = 3")
            current = 3
        if current < 4:
            # Existing v3 authority rows intentionally receive no seal: they
            # must be inspected and explicitly re-established by a later
            # authority operation rather than silently becoming trusted.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS authority_seals (
                    table_name TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    row_hash TEXT NOT NULL,
                    sealed_at TEXT NOT NULL,
                    PRIMARY KEY(table_name, row_id, version)
                )"""
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS authority_seals_no_update BEFORE UPDATE ON authority_seals BEGIN SELECT RAISE(ABORT, 'authority seals are immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS authority_seals_no_delete BEFORE DELETE ON authority_seals BEGIN SELECT RAISE(ABORT, 'authority seals are immutable'); END"
            )
            connection.execute("PRAGMA user_version = 4")
            current = 4
        if current < 5:
            # Checkpoints are contract-owned execution gates.  The nullable
            # work-unit reference preserves v1-v4 goals that have no
            # checkpoints, while a contracted checkpointed goal requires it.
            if "checkpoint_id" not in {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}:
                connection.execute("ALTER TABLE work_units ADD COLUMN checkpoint_id TEXT")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS goal_checkpoints (
                    goal_id TEXT NOT NULL REFERENCES goals(id),
                    checkpoint_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT,
                    reached_at TEXT,
                    PRIMARY KEY(goal_id, checkpoint_id),
                    UNIQUE(goal_id, position)
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS goal_checkpoints_next ON goal_checkpoints(goal_id, position, status)")
            # v3/v4 contracts already owned their checkpoint ordering.  Do
            # not infer a work-unit binding, but reconstruct the deterministic
            # gate rows from each strictly valid contract.  They remain
            # unsealed: a human must attest the migrated authority state.
            try:
                from .authority import authority_envelope_sha256, validate_authority_envelope
            except ImportError as error:  # pragma: no cover - fail closed on a broken package
                raise StateError("authority validation is unavailable for migration") from error
            for contract_row in connection.execute("SELECT goal_id,contract,envelope_sha256 FROM goal_contracts"):
                try:
                    contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
                except (TypeError, ValueError) as error:
                    raise StateError(f"cannot migrate invalid authority contract {contract_row['goal_id']}: {error}") from error
                if contract["goal_id"] != contract_row["goal_id"] or authority_envelope_sha256(contract) != contract_row["envelope_sha256"]:
                    raise StateError(f"cannot migrate authority contract with mismatched identity or hash: {contract_row['goal_id']}")
                expected = [(checkpoint_id, position) for position, checkpoint_id in enumerate(contract["checkpoints"])]
                existing = connection.execute(
                    "SELECT checkpoint_id,position,status,evidence_json,reached_at FROM goal_checkpoints WHERE goal_id=? ORDER BY position",
                    (contract_row["goal_id"],),
                ).fetchall()
                if existing:
                    actual = [(row["checkpoint_id"], int(row["position"])) for row in existing]
                    if actual != expected or any(row["status"] != "pending" or row["evidence_json"] is not None or row["reached_at"] is not None for row in existing):
                        raise StateError(f"cannot migrate conflicting checkpoint rows for {contract_row['goal_id']}")
                else:
                    for checkpoint_id, position in expected:
                        connection.execute(
                            "INSERT INTO goal_checkpoints(goal_id,checkpoint_id,position,status) VALUES(?,?,?,'pending')",
                            (contract_row["goal_id"], checkpoint_id, position),
                        )
                # The sole legacy shorthand is normalized deterministically.
                # Any other malformed or expanded scope remains untrusted and
                # is rejected by attestation/claim rather than guessed here.
                for unit in connection.execute("SELECT id,scope FROM work_units WHERE goal_id=?", (contract_row["goal_id"],)):
                    try:
                        scope = _decode(unit["scope"], {})
                    except (TypeError, ValueError) as error:
                        raise StateError(f"cannot migrate malformed work-unit scope {unit['id']}: {error}") from error
                    if set(scope) == {"paths"} and isinstance(scope["paths"], list) and scope["paths"] and all(isinstance(path, str) and path for path in scope["paths"]):
                        connection.execute("UPDATE work_units SET scope=? WHERE id=?", (_encode({"paths": scope["paths"], "exclusions": []}), unit["id"]))
            connection.execute("PRAGMA user_version = 5")
            current = 5
        if current < 6:
            # Provider effects are deliberately an extension of, rather than a
            # replacement for, the v1 durable-intent ledger.  Old records keep
            # their v1 semantics and can never be mistaken for a provider
            # dispatch.  A v5 -> v6 migration is backed up by the common
            # pre-migration snapshot above before any of these changes occur.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(effect_intents)")}
            for column, definition in (
                ("protocol_version", "INTEGER NOT NULL DEFAULT 1"),
                ("provider", "TEXT"),
                ("capability", "TEXT"),
                ("envelope_sha256", "TEXT"),
                ("approval_id", "TEXT"),
                ("resource_scope", "TEXT"),
                ("work_attempt_id", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE effect_intents ADD COLUMN {column} {definition}")
            approval_columns = {row[1] for row in connection.execute("PRAGMA table_info(transition_approvals)")}
            if "resource_scope" not in approval_columns:
                connection.execute("ALTER TABLE transition_approvals ADD COLUMN resource_scope TEXT")
            connection.execute(
                "UPDATE effect_intents SET updated_at=created_at WHERE updated_at IS NULL"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS effect_attempts (
                    id TEXT PRIMARY KEY,
                    intent_key TEXT NOT NULL REFERENCES effect_intents(idempotency_key),
                    attempt_no INTEGER NOT NULL,
                    work_attempt_id TEXT NOT NULL REFERENCES work_attempts(id),
                    lease_generation INTEGER NOT NULL,
                    dispatched_by TEXT NOT NULL,
                    dispatched_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status='executing'),
                    UNIQUE(intent_key,attempt_no)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS effect_receipt_events (
                    id TEXT PRIMARY KEY,
                    intent_key TEXT NOT NULL REFERENCES effect_intents(idempotency_key),
                    effect_attempt_id TEXT REFERENCES effect_attempts(id),
                    event_type TEXT NOT NULL,
                    observation TEXT NOT NULL,
                    observed_by TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS effect_attempts_intent ON effect_attempts(intent_key,attempt_no)")
            connection.execute("CREATE INDEX IF NOT EXISTS effect_receipt_events_intent ON effect_receipt_events(intent_key,recorded_at)")
            connection.execute("CREATE TRIGGER IF NOT EXISTS effect_attempts_no_update BEFORE UPDATE ON effect_attempts BEGIN SELECT RAISE(ABORT, 'effect attempts are immutable'); END")
            connection.execute("CREATE TRIGGER IF NOT EXISTS effect_attempts_no_delete BEFORE DELETE ON effect_attempts BEGIN SELECT RAISE(ABORT, 'effect attempts are immutable'); END")
            connection.execute("CREATE TRIGGER IF NOT EXISTS effect_receipt_events_no_update BEFORE UPDATE ON effect_receipt_events BEGIN SELECT RAISE(ABORT, 'effect receipt events are immutable'); END")
            connection.execute("CREATE TRIGGER IF NOT EXISTS effect_receipt_events_no_delete BEFORE DELETE ON effect_receipt_events BEGIN SELECT RAISE(ABORT, 'effect receipt events are immutable'); END")
            connection.execute("PRAGMA user_version = 6")
            current = 6
        if current < 7:
            approval_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transition_approvals)")
            }
            if "protocol_version" not in approval_columns:
                connection.execute(
                    "ALTER TABLE transition_approvals ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                """UPDATE transition_approvals SET protocol_version=2
                   WHERE resource_scope IS NOT NULL OR EXISTS (
                     SELECT 1 FROM goal_contracts c
                     WHERE c.goal_id=transition_approvals.goal_id
                       AND c.envelope_sha256=transition_approvals.envelope_sha256
                       AND c.version='v2'
                   )"""
            )
            connection.execute("PRAGMA user_version = 7")
            current = 7
        if current < 8:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(effect_intents)")}
            if "last_reconciliation_event_id" not in columns:
                connection.execute("ALTER TABLE effect_intents ADD COLUMN last_reconciliation_event_id TEXT")
            # Schema 7 recorded reconciliation events and changed the intent
            # status, but did not bind the state transition to one immutable
            # event.  Backfill only an unambiguous, internally consistent
            # history; never guess which event authorized a retry.
            for intent in connection.execute(
                "SELECT * FROM effect_intents WHERE protocol_version=2"
            ).fetchall():
                events = connection.execute(
                    "SELECT * FROM effect_receipt_events WHERE intent_key=? AND event_type='reconciliation' ORDER BY recorded_at,id",
                    (intent["idempotency_key"],),
                ).fetchall()
                if not events:
                    if intent["status"] == "reconciled":
                        raise StateError(
                            f"cannot migrate provider reconciliation without an event: {intent['idempotency_key']}"
                        )
                    continue
                attempts = connection.execute(
                    "SELECT id,attempt_no,dispatched_at FROM effect_attempts WHERE intent_key=? ORDER BY attempt_no",
                    (intent["idempotency_key"],),
                ).fetchall()
                try:
                    request_binding = _decode(intent["request_json"], {})
                    performer = request_binding["authorized_performer_id"]
                    by_id = {row["id"]: row for row in attempts}
                    used_attempts: set[str] = set()
                    ordered: list[tuple[sqlite3.Row, sqlite3.Row, dict[str, Any]]] = []
                    for event in events:
                        observation = _decode(event["observation"], {})
                        if (
                            not isinstance(observation, dict)
                            or set(observation) != {"source", "resolution", "observation"}
                            or observation["source"] not in {"manual", "adapter"}
                            or observation["resolution"] not in {"applied", "absent", "conflict"}
                            or not isinstance(observation["observation"], dict)
                            or (observation["source"] == "manual" and observation["resolution"] == "absent")
                            or not _provider_observation_is_sanitized(observation)
                            or event["observed_by"] != performer
                        ):
                            raise ValueError
                        if event["effect_attempt_id"] is not None:
                            candidate = by_id.get(event["effect_attempt_id"])
                            candidates = [] if candidate is None else [candidate]
                        else:
                            candidates = [
                                row for row in attempts
                                if row["id"] not in used_attempts
                                and row["dispatched_at"] <= event["recorded_at"]
                            ]
                        if len(candidates) != 1:
                            raise ValueError
                        attempt = candidates[0]
                        if attempt["id"] in used_attempts:
                            raise ValueError
                        used_attempts.add(attempt["id"])
                        ordered.append((attempt, event, observation))
                    if [int(item[0]["attempt_no"]) for item in ordered] != sorted(
                        int(item[0]["attempt_no"]) for item in ordered
                    ):
                        raise ValueError
                    if any(
                        observation["source"] != "adapter" or observation["resolution"] != "absent"
                        for _, _, observation in ordered[:-1]
                    ):
                        raise ValueError
                    latest_attempt_no = int(attempts[-1]["attempt_no"]) if attempts else 0
                    committed_attempt, committed_event, committed_observation = ordered[-1]
                    committed_attempt_no = int(committed_attempt["attempt_no"])
                    resolution = committed_observation["resolution"]
                    if (
                        (resolution == "applied" and (intent["status"] != "reconciled" or committed_attempt_no != latest_attempt_no))
                        or (resolution == "conflict" and (intent["status"] != "failed" or committed_attempt_no != latest_attempt_no))
                        or (
                            resolution == "absent"
                            and (
                                (intent["status"] in {"reconciled", "pending"} and committed_attempt_no != latest_attempt_no)
                                or (intent["status"] in {"executing", "succeeded", "failed", "indeterminate"} and committed_attempt_no >= latest_attempt_no)
                            )
                        )
                    ):
                        raise ValueError
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    raise StateError(
                        f"cannot migrate invalid provider reconciliation history: {intent['idempotency_key']}"
                    )
                connection.execute(
                    "UPDATE effect_intents SET last_reconciliation_event_id=? WHERE idempotency_key=?",
                    (committed_event["id"], intent["idempotency_key"]),
                )
            connection.execute("PRAGMA user_version = 8")
            current = 8
        if current < 9:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(transition_approvals)")}
            if "provenance" not in columns:
                connection.execute("ALTER TABLE transition_approvals ADD COLUMN provenance TEXT")
            # Existing approval records remain sealed historical records, but
            # lack a human-ceremony/content binding and cannot satisfy v3-only
            # completion or consequential-effect gates. Re-record them through
            # the local ceremony with verified evidence where still needed.
            connection.execute("PRAGMA user_version = 9")
            current = 9
        if current < 10:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule_resume_idempotency'"
            ).fetchone()
            if existing is None:
                connection.execute("""CREATE TABLE schedule_resume_idempotency (idempotency_key TEXT PRIMARY KEY, invocation_sha256 TEXT NOT NULL, goal_id TEXT NOT NULL REFERENCES goals(id), work_unit_id TEXT NOT NULL REFERENCES work_units(id), state TEXT NOT NULL CHECK(state='consumed'), consumed_at TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES work_attempts(id))""")
            else:
                columns = connection.execute("PRAGMA table_info(schedule_resume_idempotency)").fetchall()
                expected = (
                    "idempotency_key", "invocation_sha256", "goal_id", "work_unit_id",
                    "state", "consumed_at", "attempt_id",
                )
                if tuple(row[1] for row in columns) != expected or columns[0][5] != 1:
                    raise StateError("cannot migrate invalid schedule resume idempotency table")
            connection.execute("CREATE INDEX IF NOT EXISTS schedule_resume_goal_unit ON schedule_resume_idempotency(goal_id,work_unit_id)")
            connection.execute("PRAGMA user_version = 10")
            current = 10
        if current < 11:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_unit_dependencies'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "CREATE TABLE work_unit_dependencies ("
                    "work_unit_id TEXT NOT NULL REFERENCES work_units(id), "
                    "prerequisite_id TEXT NOT NULL REFERENCES work_units(id), "
                    "PRIMARY KEY(work_unit_id,prerequisite_id))"
                )
            else:
                if connection.execute("SELECT 1 FROM work_unit_dependencies LIMIT 1").fetchone() is not None:
                    raise StateError("cannot migrate a pre-existing nonempty work unit dependencies table")
                columns = connection.execute("PRAGMA table_info(work_unit_dependencies)").fetchall()
                expected = (
                    ("work_unit_id", "TEXT", 1, 1),
                    ("prerequisite_id", "TEXT", 1, 2),
                )
                actual = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in columns)
                foreign_keys = {
                    (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]), str(row[7]))
                    for row in connection.execute("PRAGMA foreign_key_list(work_unit_dependencies)")
                }
                if actual != expected or foreign_keys != {
                    ("work_unit_id", "work_units", "id", "NO ACTION", "NO ACTION", "NONE"),
                    ("prerequisite_id", "work_units", "id", "NO ACTION", "NO ACTION", "NONE"),
                }:
                    raise StateError("cannot migrate invalid work unit dependencies table")
            invalid = connection.execute(
                "SELECT id,status FROM goals WHERE status NOT IN ('planned','active','paused','blocked','complete','stopped') LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StateError(f"cannot migrate invalid schema11 goal status: {invalid['id']}")
            connection.execute("PRAGMA user_version = 11")
            current = 11
        if current < 12:
            invalid = connection.execute(
                "SELECT id,status FROM goals WHERE status NOT IN ('planned','active','paused','blocked','complete','stopped') LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StateError(f"cannot migrate invalid schema11 goal status: {invalid['id']}")
            connection.execute("PRAGMA user_version = 12")
            current = 12
        if current < 13:
            # Every v13 object is new. A lower user_version that already
            # carries one is a malformed/shadow schema, never a partially
            # upgraded ledger that migration may silently adopt.
            intervention_tables = (
                "intervention_requests", "intervention_responses",
                "intervention_response_heads", "intervention_closures",
            )
            intervention_indexes = (
                "intervention_requests_goal_created", "intervention_requests_unit_created",
                "work_units_current_intervention", "intervention_responses_request_created",
                "intervention_response_heads_response", "intervention_closures_closed_at",
                "intervention_closures_closed_request", "intervention_responses_predecessor_once",
                "work_unit_dependencies_prerequisite",
            )
            intervention_triggers = (
                "intervention_requests_no_update", "intervention_requests_no_delete",
                "intervention_responses_no_update", "intervention_responses_no_delete",
                "intervention_closures_no_update", "intervention_closures_no_delete",
                "intervention_response_heads_no_delete",
            )
            # Remote schema 12 legitimately predates local dependency edges.
            # Create that independent lineage step before its v13 index is
            # installed; the sealed pre-migration shape is checked by the
            # lineage guard in migrate_with_evidence.
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_unit_dependencies'"
            ).fetchone() is None:
                connection.execute(
                    "CREATE TABLE work_unit_dependencies ("
                    "work_unit_id TEXT NOT NULL REFERENCES work_units(id), "
                    "prerequisite_id TEXT NOT NULL REFERENCES work_units(id), "
                    "PRIMARY KEY(work_unit_id,prerequisite_id))"
                )
            for object_type, names in (("table", intervention_tables), ("index", intervention_indexes), ("trigger", intervention_triggers)):
                placeholders = ",".join("?" for _ in names)
                shadow = connection.execute(
                    f"SELECT name FROM sqlite_master WHERE type=? AND name IN ({placeholders}) ORDER BY name LIMIT 1",
                    (object_type, *names),
                ).fetchone()
                if shadow is not None:
                    raise StateError(f"cannot migrate schema12 database with pre-existing intervention {object_type}: {shadow[0]}")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}
            if "current_intervention_id" in columns:
                raise StateError("cannot migrate schema12 database with pre-existing current intervention pointer")
            connection.execute(
                """CREATE TABLE intervention_requests (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    goal_id TEXT NOT NULL REFERENCES goals(id),
                    work_unit_id TEXT NOT NULL REFERENCES work_units(id),
                    attempt_id TEXT NOT NULL UNIQUE REFERENCES work_attempts(id),
                    producer_id TEXT NOT NULL,
                    outcome_class TEXT NOT NULL CHECK(outcome_class IN ('blocked','approval-required')),
                    request_json TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL UNIQUE,
                    yield_tokens_consumed INTEGER NOT NULL,
                    yield_elapsed_input_mode TEXT NOT NULL CHECK(yield_elapsed_input_mode IN ('explicit','measured')),
                    yield_elapsed_input_ms INTEGER,
                    yield_accounted_elapsed_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK((yield_elapsed_input_mode='explicit') = (yield_elapsed_input_ms IS NOT NULL))
                )"""
            )
            connection.execute(
                """CREATE TABLE intervention_responses (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    request_id TEXT NOT NULL REFERENCES intervention_requests(id),
                    request_sha256 TEXT NOT NULL,
                    revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
                    previous_response_id TEXT REFERENCES intervention_responses(id),
                    expected_previous_sha256 TEXT,
                    responder_kind TEXT NOT NULL CHECK(responder_kind IN ('human','steward')),
                    responder_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('answered','declined','cancelled')),
                    response_json TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(request_id,revision_no),
                    CHECK((revision_no=1 AND previous_response_id IS NULL AND expected_previous_sha256 IS NULL) OR
                          (revision_no>1 AND previous_response_id IS NOT NULL AND expected_previous_sha256 IS NOT NULL))
                )"""
            )
            connection.execute(
                """CREATE TABLE intervention_response_heads (
                    request_id TEXT PRIMARY KEY REFERENCES intervention_requests(id),
                    current_response_id TEXT NOT NULL UNIQUE REFERENCES intervention_responses(id),
                    current_response_sha256 TEXT NOT NULL UNIQUE,
                    revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE intervention_closures (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE REFERENCES intervention_requests(id),
                    response_id TEXT NOT NULL UNIQUE REFERENCES intervention_responses(id),
                    response_sha256 TEXT NOT NULL,
                    closure_kind TEXT NOT NULL CHECK(closure_kind='requeued'),
                    requeue_evidence_sha256 TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL,
                    closed_by TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                )"""
            )
            connection.execute("ALTER TABLE work_units ADD COLUMN current_intervention_id TEXT REFERENCES intervention_requests(id)")
            for statement in (
                "CREATE INDEX intervention_requests_goal_created ON intervention_requests(goal_id,created_at,id)",
                "CREATE INDEX intervention_requests_unit_created ON intervention_requests(work_unit_id,created_at,id)",
                "CREATE INDEX work_units_current_intervention ON work_units(current_intervention_id)",
                "CREATE INDEX intervention_responses_request_created ON intervention_responses(request_id,created_at,id)",
                "CREATE INDEX intervention_response_heads_response ON intervention_response_heads(current_response_id)",
                "CREATE INDEX intervention_closures_closed_at ON intervention_closures(closed_at,id)",
                "CREATE INDEX intervention_closures_closed_request ON intervention_closures(closed_at,request_id)",
                "CREATE INDEX work_unit_dependencies_prerequisite ON work_unit_dependencies(prerequisite_id,work_unit_id)",
                "CREATE UNIQUE INDEX intervention_responses_predecessor_once ON intervention_responses(previous_response_id) WHERE previous_response_id IS NOT NULL",
                "CREATE TRIGGER intervention_requests_no_update BEFORE UPDATE ON intervention_requests BEGIN SELECT RAISE(ABORT, 'intervention requests are immutable'); END",
                "CREATE TRIGGER intervention_requests_no_delete BEFORE DELETE ON intervention_requests BEGIN SELECT RAISE(ABORT, 'intervention requests are immutable'); END",
                "CREATE TRIGGER intervention_responses_no_update BEFORE UPDATE ON intervention_responses BEGIN SELECT RAISE(ABORT, 'intervention responses are immutable'); END",
                "CREATE TRIGGER intervention_responses_no_delete BEFORE DELETE ON intervention_responses BEGIN SELECT RAISE(ABORT, 'intervention responses are immutable'); END",
                "CREATE TRIGGER intervention_closures_no_update BEFORE UPDATE ON intervention_closures BEGIN SELECT RAISE(ABORT, 'intervention closures are immutable'); END",
                "CREATE TRIGGER intervention_closures_no_delete BEFORE DELETE ON intervention_closures BEGIN SELECT RAISE(ABORT, 'intervention closures are immutable'); END",
                "CREATE TRIGGER intervention_response_heads_no_delete BEFORE DELETE ON intervention_response_heads BEGIN SELECT RAISE(ABORT, 'intervention response heads are immutable except for compare-and-swap updates'); END",
            ):
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 13")
            current = 13
        if current < 14:
            # Schema 14 is intentionally fail-closed: a v13 ledger which
            # already has any execution receipt object cannot be safely
            # distinguished from a partial or attacker-created migration.
            receipt_tables = ("codex_run_preparations", "codex_run_starts", "codex_run_finishes")
            receipt_indexes = (
                "codex_run_preparations_attempt_run", "codex_run_preparations_goal_created",
                "codex_run_starts_agent", "codex_run_finishes_recorded",
            )
            receipt_triggers = (
                "codex_run_preparations_no_update", "codex_run_preparations_no_delete",
                "codex_run_starts_no_update", "codex_run_starts_no_delete",
                "codex_run_finishes_no_update", "codex_run_finishes_no_delete",
            )
            for object_type, names in (("table", receipt_tables), ("index", receipt_indexes), ("trigger", receipt_triggers)):
                placeholders = ",".join("?" for _ in names)
                shadow = connection.execute(
                    f"SELECT name FROM sqlite_master WHERE type=? AND name IN ({placeholders}) ORDER BY name LIMIT 1",
                    (object_type, *names),
                ).fetchone()
                if shadow is not None:
                    raise StateError(f"cannot migrate schema13 database with pre-existing codex run {object_type}: {shadow[0]}")
            attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(work_attempts)")}
            if "token_accounting_source" in attempt_columns:
                raise StateError("cannot migrate schema13 database with pre-existing token accounting source")
            connection.execute(
                "ALTER TABLE work_attempts ADD COLUMN token_accounting_source TEXT NOT NULL "
                "DEFAULT 'legacy-unspecified' CHECK(token_accounting_source IN "
                "('legacy-unspecified','pending','host-measured','caller-declared','unavailable'))"
            )
            connection.execute(
                """CREATE TABLE codex_run_preparations (
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES work_attempts(id),
                    run_no INTEGER NOT NULL CHECK(run_no>=1), idempotency_key TEXT NOT NULL UNIQUE,
                    prepared_by TEXT NOT NULL, goal_id TEXT NOT NULL REFERENCES goals(id),
                    work_unit_id TEXT NOT NULL REFERENCES work_units(id), lease_generation INTEGER NOT NULL,
                    envelope_sha256 TEXT NOT NULL, repository TEXT, revision TEXT, branch TEXT, workspace TEXT,
                    role TEXT NOT NULL, requested_model TEXT, requested_reasoning_effort TEXT,
                    sandbox_mode TEXT NOT NULL, request_sha256 TEXT NOT NULL, handoff_sha256 TEXT,
                    plan_sha256 TEXT NOT NULL, brief_sha256 TEXT NOT NULL,
                    requested_task_name TEXT NOT NULL UNIQUE, prepared_at TEXT NOT NULL,
                    lease_token_char_length INTEGER NOT NULL CHECK(lease_token_char_length BETWEEN 32 AND 512),
                    UNIQUE(attempt_id,run_no))"""
            )
            connection.execute(
                """CREATE TABLE codex_run_starts (
                    run_id TEXT PRIMARY KEY REFERENCES codex_run_preparations(id),
                    host_canonical_name TEXT NOT NULL UNIQUE, host_agent_id TEXT UNIQUE,
                    observed_by TEXT NOT NULL, recorded_at TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE codex_run_finishes (
                    run_id TEXT PRIMARY KEY REFERENCES codex_run_starts(run_id),
                    outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed','interrupted','needs-attention')),
                    result_status TEXT NOT NULL CHECK(result_status IN ('observed','unavailable')),
                    result_sha256 TEXT, usage_status TEXT NOT NULL CHECK(usage_status IN ('measured','unavailable')),
                    input_tokens INTEGER, output_tokens INTEGER, observed_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    CHECK((result_status='observed' AND result_sha256 IS NOT NULL) OR (result_status='unavailable' AND result_sha256 IS NULL)),
                    CHECK((usage_status='measured' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND input_tokens>=0 AND output_tokens>=0) OR (usage_status='unavailable' AND input_tokens IS NULL AND output_tokens IS NULL)))"""
            )
            for statement in (
                "CREATE INDEX codex_run_preparations_attempt_run ON codex_run_preparations(attempt_id,run_no)",
                "CREATE INDEX codex_run_preparations_goal_created ON codex_run_preparations(goal_id,prepared_at,id)",
                "CREATE UNIQUE INDEX codex_run_starts_agent ON codex_run_starts(host_agent_id) WHERE host_agent_id IS NOT NULL",
                "CREATE INDEX codex_run_finishes_recorded ON codex_run_finishes(recorded_at,run_id)",
                "CREATE TRIGGER codex_run_preparations_no_update BEFORE UPDATE ON codex_run_preparations BEGIN SELECT RAISE(ABORT, 'codex run preparations are immutable'); END",
                "CREATE TRIGGER codex_run_preparations_no_delete BEFORE DELETE ON codex_run_preparations BEGIN SELECT RAISE(ABORT, 'codex run preparations are immutable'); END",
                "CREATE TRIGGER codex_run_starts_no_update BEFORE UPDATE ON codex_run_starts BEGIN SELECT RAISE(ABORT, 'codex run starts are immutable'); END",
                "CREATE TRIGGER codex_run_starts_no_delete BEFORE DELETE ON codex_run_starts BEGIN SELECT RAISE(ABORT, 'codex run starts are immutable'); END",
                "CREATE TRIGGER codex_run_finishes_no_update BEFORE UPDATE ON codex_run_finishes BEGIN SELECT RAISE(ABORT, 'codex run finishes are immutable'); END",
                "CREATE TRIGGER codex_run_finishes_no_delete BEFORE DELETE ON codex_run_finishes BEGIN SELECT RAISE(ABORT, 'codex run finishes are immutable'); END",
            ):
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 14")
            current = 14
        if current < 15:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}
            if "verification_policy" not in columns:
                # A legacy row always retains the published Stage 2 path.  Do
                # not infer a weaker policy from a title, scope, or outcome.
                connection.execute(
                    "ALTER TABLE work_units ADD COLUMN verification_policy TEXT NOT NULL DEFAULT 'implementation-review'"
                )
            invalid = connection.execute(
                "SELECT id FROM work_units WHERE verification_policy NOT IN "
                "('implementation-review','implementation-deterministic-review',"
                "'research-review','documentation-review','deterministic-direct') LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StateError(f"cannot migrate invalid work-unit verification policy: {invalid['id']}")
        if current < 15:
            # Version 12 adds an explicit, shorter implementation route only
            # for work units whose authority names it.  Existing rows retain
            # their recorded policy; no title, outcome, or missing tester is
            # interpreted as permission to weaken that policy.
            work_columns = {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}
            if "acceptance_checks" not in work_columns:
                connection.execute(
                    "ALTER TABLE work_units ADD COLUMN acceptance_checks TEXT NOT NULL DEFAULT '[]'"
                )
            workflow_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_evidence)")}
            if "completion_evidence_json" not in workflow_columns:
                connection.execute("ALTER TABLE workflow_evidence ADD COLUMN completion_evidence_json TEXT")
            invalid = connection.execute(
                "SELECT id FROM work_units WHERE verification_policy NOT IN "
                "('implementation-review','implementation-deterministic-review',"
                "'research-review','documentation-review','deterministic-direct') LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StateError(f"cannot migrate invalid work-unit verification policy: {invalid['id']}")
            connection.execute("PRAGMA user_version = 15")
            current = 15
        return SCHEMA_VERSION

    @staticmethod
    def _assert_known_pre15_lineage_in_transaction(connection: sqlite3.Connection, version: int) -> None:
        """Reject colliding historical versions that are not complete known lineages."""
        if 10 <= version < 15:
            from .schema_history import KNOWN_SCHEMAS, schema_signature

            if schema_signature(connection) not in KNOWN_SCHEMAS.get(version, ()):
                raise StateError(f"schema{version} does not match a complete known lineage structure")
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        work_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_units)")}
        evidence_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(workflow_evidence)")}
        local_dependencies = "work_unit_dependencies" in tables
        remote_policy = "verification_policy" in work_columns
        remote_evidence = "acceptance_checks" in work_columns and "completion_evidence_json" in evidence_columns
        intervention_names = {
            "intervention_requests", "intervention_responses", "intervention_response_heads", "intervention_closures",
        }
        receipt_names = {"codex_run_preparations", "codex_run_starts", "codex_run_finishes"}
        local_interventions = intervention_names <= tables and "current_intervention_id" in work_columns
        local_receipts = receipt_names <= tables and "token_accounting_source" in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(work_attempts)")
        }
        has_later_artifact = (
            local_dependencies or remote_policy or remote_evidence
            or bool(intervention_names & tables) or "current_intervention_id" in work_columns
            or bool(receipt_names & tables) or "token_accounting_source" in {
                str(row[1]) for row in connection.execute("PRAGMA table_info(work_attempts)")
            }
        )
        if version < 11:
            if has_later_artifact:
                raise StateError("pre-schema11 database contains a later-lineage artifact")
            return
        if version == 11:
            local = local_dependencies and not remote_policy and not remote_evidence and not local_interventions and not local_receipts
            remote = not local_dependencies and remote_policy and not remote_evidence and not local_interventions and not local_receipts
            if not (local or remote):
                raise StateError("schema11 database does not match exactly one known lineage")
            return
        if version == 12:
            local = local_dependencies and not remote_policy and not remote_evidence and not local_interventions and not local_receipts
            remote = not local_dependencies and remote_policy and remote_evidence and not local_interventions and not local_receipts
            if not (local or remote):
                raise StateError("schema12 database does not match a complete known lineage")
            return
        if version == 13:
            if not (local_dependencies and local_interventions and not local_receipts and not remote_policy and not remote_evidence):
                raise StateError("schema13 database does not match the local published lineage")
            return
        if version == 14 and not (
            local_dependencies and local_interventions and local_receipts and not remote_policy and not remote_evidence
        ):
            raise StateError("schema14 database does not match the local published lineage")

    @staticmethod
    def _backup_before_migration(connection: sqlite3.Connection, version: int) -> Path:
        """Create one durable, non-overwriting snapshot before schema mutation."""
        database_name = connection.execute("PRAGMA database_list").fetchone()[2]
        source = Path(database_name)
        backup = source.with_name(f"{source.name}.v{version}.bak")
        if backup.exists():
            # Never silently reuse an old snapshot: preserve it and create a
            # distinct snapshot of this exact pre-migration state.
            backup = source.with_name(f"{source.name}.v{version}.{uuid4().hex}.bak")
        temporary = backup.with_name(f".{backup.name}.{uuid4().hex}.tmp")
        try:
            # A separate SQLite source and backup API captures committed WAL
            # pages; copying the main file alone does not.
            source_connection = sqlite3.connect(source)
            destination = sqlite3.connect(temporary)
            try:
                source_connection.backup(destination)
            finally:
                destination.close()
                source_connection.close()
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, backup)
        finally:
            temporary.unlink(missing_ok=True)
        return backup

    def migrate(self) -> int:
        return int(self.migrate_with_evidence()["after_schema"])

    def migrate_with_evidence(
        self, *, before_commit: Callable[[Mapping[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Migrate while returning recovery evidence, optionally preparing it before commit."""
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None
            verified_sealed_state = False
            if 4 <= version < SCHEMA_VERSION:
                # Never let a migration ratify direct SQL tampering. Older
                # schemas legitimately lack newer authoritative tables, so
                # verify exactly the tables that existed at that version.
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(
                    connection, existing_only=True,
                )
                self._assert_known_pre15_lineage_in_transaction(connection, version)
                verified_sealed_state = True
            backup: Path | None = None
            if 0 < version < SCHEMA_VERSION:
                backup = self._backup_before_migration(connection, version)
            result = self._migrate(connection, create_backup=False)
            if version == 0 and not has_tables:
                # Initial runtime-control state is authoritative too.  This
                # is bootstrap, not a migration attestation of old records.
                self._seal_current_state_in_transaction(connection, _now())
            elif verified_sealed_state:
                # Schema additions change the canonical shape of some rows
                # and may add an empty authoritative table. Bind that exact
                # deterministic result in the same migration transaction.
                self._seal_current_state_in_transaction(connection, _now())
            evidence = {
                "before_schema": version,
                "after_schema": result,
                "backup_path": str(backup) if backup is not None else None,
                "backup_sha256": _sha256_file(backup) if backup is not None else None,
            }
            # The callback runs while the transaction remains open.  A
            # journal failure therefore aborts the schema change instead of
            # leaving a committed runtime without durable recovery evidence.
            if before_commit is not None:
                before_commit(dict(evidence))
            return evidence

    def inspect_schema_version(self) -> int:
        """Read the on-disk schema without creating or migrating the database."""
        if not self.path.is_file():
            raise StateError(f"Runtime database does not exist: {self.path}")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def _ensure(self) -> None:
        if not self.path.is_file():
            raise StateError(f"Runtime database does not exist: {self.path}")
        version = self.inspect_schema_version()
        if version != SCHEMA_VERSION:
            raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")

    @staticmethod
    def _seal_on_commit(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='tasktra_seal_on_commit'"
        ).fetchone() is not None

    @staticmethod
    def _mark_seal_on_commit(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS tasktra_seal_on_commit (id INTEGER PRIMARY KEY CHECK(id=1))")
        connection.execute("INSERT OR IGNORE INTO tasktra_seal_on_commit(id) VALUES(1)")

    @staticmethod
    def _prepare_write(connection: sqlite3.Connection, *, allow_unsealed: bool = False) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        has_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None
        if version == SCHEMA_VERSION:
            StateStore._assert_audit_chain_in_transaction(connection)
            if not allow_unsealed:
                StateStore._assert_current_state_integrity_in_transaction(connection)
            StateStore._mark_seal_on_commit(connection)
            return
        if version == 0 and not has_tables:
            StateStore._migrate(connection)
            # A freshly initialized ledger has no prior records to review;
            # its first normal write creates the initial complete seal set.
            StateStore._mark_seal_on_commit(connection)
            return
        raise StateError(f"runtime schema {version} requires explicit migration to {SCHEMA_VERSION}")

    @staticmethod
    def _assert_audit_chain_in_transaction(connection: sqlite3.Connection) -> int:
        """Verify the append-only chain while the caller holds its write lock."""
        previous, expected = _GENESIS_HASH, 1
        for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
            actual = _audit_hash(previous, row["sequence"], row["event_type"], row["goal_id"], row["work_unit_id"], row["payload"], row["created_at"])
            if row["sequence"] != expected or row["previous_hash"] != previous or row["event_hash"] != actual:
                raise StateError(f"audit integrity verification failed at sequence {row['sequence']}")
            previous, expected = row["event_hash"], expected + 1
        return expected - 1

    @staticmethod
    def _append_event_in_transaction(
        connection: sqlite3.Connection,
        event_type: str,
        *,
        goal_id: str | None = None,
        work_unit_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        if not event_type.strip():
            raise StateError("event_type must be non-empty")
        timestamp = _now()
        encoded = _encode(payload or {})
        cursor = connection.execute(
            "INSERT INTO events(goal_id,work_unit_id,event_type,payload,created_at) VALUES(?,?,?,?,?)",
            (goal_id, work_unit_id, event_type, encoded, timestamp),
        )
        event_id = int(cursor.lastrowid)
        prior = connection.execute("SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence = 1 if prior is None else int(prior["sequence"]) + 1
        previous_hash = _GENESIS_HASH if prior is None else prior["event_hash"]
        digest = _audit_hash(previous_hash, sequence, event_type, goal_id, work_unit_id, encoded, timestamp)
        connection.execute(
            "INSERT INTO audit_events(sequence,legacy_event_id,goal_id,work_unit_id,event_type,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sequence, event_id, goal_id, work_unit_id, event_type, encoded, previous_hash, digest, timestamp),
        )
        return event_id

    @staticmethod
    def _seal_authority_row_in_transaction(connection: sqlite3.Connection, table: str,
                                            row_id: str, row: sqlite3.Row | dict[str, Any],
                                            timestamp: str) -> None:
        prior = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM authority_seals WHERE table_name=? AND row_id=?",
            (table, row_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO authority_seals(table_name,row_id,version,row_hash,sealed_at) VALUES(?,?,?,?,?)",
            (table, row_id, int(prior) + 1, _authority_row_hash(table, row), timestamp),
        )

    @staticmethod
    def _authoritative_row_id(table: str, row: sqlite3.Row | dict[str, Any]) -> str:
        """Return a stable identifier, including collision-free composite keys."""
        keys = _AUTHORITATIVE_TABLE_KEYS[table]
        values = [str(dict(row)[key]) for key in keys]
        return values[0] if len(values) == 1 else _encode(values)

    @staticmethod
    def _iter_authoritative_rows(
        connection: sqlite3.Connection, *, existing_only: bool = False,
    ) -> Iterator[tuple[str, str, sqlite3.Row]]:
        existing = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } if existing_only else set(_AUTHORITATIVE_TABLE_KEYS)
        for table in sorted(_AUTHORITATIVE_TABLE_KEYS):
            if table not in existing:
                continue
            keys = _AUTHORITATIVE_TABLE_KEYS[table]
            order = ",".join(keys)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                yield table, StateStore._authoritative_row_id(table, row), row

    @staticmethod
    def _state_manifest_hash(connection: sqlite3.Connection, *, existing_only: bool = False) -> str:
        # This produces the exact bytes of the former materialized
        # ``_encode(sorted(rows))`` algorithm without retaining every sealed
        # row in memory.  Table and primary-key order is the canonical
        # (table,row_id) order used by the previous sort.
        digest = hashlib.sha256()
        digest.update(b"[")
        first = True
        for table, row_id, row in StateStore._iter_authoritative_rows(
            connection, existing_only=existing_only,
        ):
            if not first:
                digest.update(b",")
            digest.update(_encode({"table": table, "row_id": row_id, "row_hash": _authority_row_hash(table, row)}).encode("utf-8"))
            first = False
        digest.update(b"]")
        return digest.hexdigest()

    @staticmethod
    def _latest_seal_hash(connection: sqlite3.Connection, table: str, row_id: str) -> str | None:
        row = connection.execute(
            "SELECT row_hash FROM authority_seals WHERE table_name=? AND row_id=? ORDER BY version DESC LIMIT 1",
            (table, row_id),
        ).fetchone()
        return None if row is None else str(row["row_hash"])

    @staticmethod
    def _assert_current_state_integrity_in_transaction(
        connection: sqlite3.Connection, *, existing_only: bool = False,
    ) -> None:
        """Fail closed before a normal mutation can ratify direct SQL tampering."""
        try:
            for table, row_id, row in StateStore._iter_authoritative_rows(connection, existing_only=existing_only):
                if StateStore._latest_seal_hash(connection, table, row_id) != _authority_row_hash(table, row):
                    raise StateError(f"authoritative state is unsealed or tampered ({table}:{row_id})")
            manifest = StateStore._latest_seal_hash(connection, _STATE_MANIFEST_TABLE, _STATE_MANIFEST_ID)
            if manifest != StateStore._state_manifest_hash(connection, existing_only=existing_only):
                raise StateError("authoritative current-state manifest is unsealed or tampered")
        except sqlite3.Error as error:
            raise StateError("runtime state is missing an authoritative table") from error
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 13:
            StateStore._assert_intervention_integrity_in_transaction(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 14:
            StateStore._assert_codex_run_integrity_in_transaction(connection)

    @staticmethod
    def _assert_intervention_integrity_in_transaction(connection: sqlite3.Connection) -> None:
        """Validate the relational facts that hashes and foreign keys cannot express."""
        try:
            from .interventions import (
                InterventionError,
                canonical_intervention_request,
                canonical_intervention_response,
                intervention_request_sha256,
                intervention_response_sha256,
            )
        except ImportError as error:  # pragma: no cover - package must be complete
            raise StateError("intervention validation is unavailable") from error
        try:
            for request in connection.execute("SELECT * FROM intervention_requests ORDER BY id"):
                request_id = str(request["id"])
                payload = _decode(request["request_json"], {})
                if (
                    canonical_intervention_request(payload) != request["request_json"]
                    or intervention_request_sha256(payload) != request["request_sha256"]
                    or payload["request_id"] != request_id or payload["version"] != request["version"]
                    or payload["source"] != {
                        "goal_id": request["goal_id"], "work_unit_id": request["work_unit_id"], "attempt_id": request["attempt_id"],
                    }
                    or payload["producer"]["actor_id"] != request["producer_id"]
                    or payload["outcome_class"] != request["outcome_class"]
                ):
                    raise ValueError("request identity does not match its immutable row")
                attempt = connection.execute(
                    """SELECT a.*,u.goal_id,u.id AS unit_id FROM work_attempts a
                       JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""", (request["attempt_id"],)
                ).fetchone()
                if (
                    attempt is None or attempt["status"] != "finished" or attempt["goal_id"] != request["goal_id"]
                    or attempt["unit_id"] != request["work_unit_id"] or attempt["owner_id"] != request["producer_id"]
                    or attempt["outcome_class"] != request["outcome_class"] or attempt["tokens_consumed"] != request["yield_tokens_consumed"]
                    or attempt["elapsed_ms"] != request["yield_accounted_elapsed_ms"]
                    or attempt["ended_at"] != request["created_at"]
                    or _decode(attempt["outcome_json"], {}) != {
                        "intervention_request_id": request_id, "request_sha256": request["request_sha256"],
                        "tokens_consumed": request["yield_tokens_consumed"], "elapsed_input_mode": request["yield_elapsed_input_mode"],
                        "elapsed_input_ms": request["yield_elapsed_input_ms"], "accounted_elapsed_ms": request["yield_accounted_elapsed_ms"],
                    }
                ):
                    raise ValueError("request is not bound to its finished attempt")
                explicit = request["yield_elapsed_input_mode"] == "explicit"
                if explicit != (request["yield_elapsed_input_ms"] is not None):
                    raise ValueError("request elapsed input mode is invalid")
                previous_response = None
                latest_response = None
                for index, response in enumerate(connection.execute(
                    "SELECT * FROM intervention_responses WHERE request_id=? ORDER BY revision_no", (request_id,)
                ), start=1):
                    payload = _decode(response["response_json"], {})
                    expected_previous = previous_response
                    if (
                        int(response["revision_no"]) != index
                        or canonical_intervention_response(payload) != response["response_json"]
                        or intervention_response_sha256(payload) != response["response_sha256"]
                        or payload["response_id"] != response["id"] or payload["version"] != response["version"]
                        or payload["request"] != {"request_id": request_id, "request_sha256": request["request_sha256"]}
                        or payload["responder"] != {"kind": response["responder_kind"], "actor_id": response["responder_id"]}
                        or payload["disposition"] != response["disposition"]
                        or response["request_sha256"] != request["request_sha256"]
                        or (expected_previous is None and (response["previous_response_id"] is not None or response["expected_previous_sha256"] is not None))
                        or (expected_previous is None and payload["expected_current_response"] is not None)
                        or (expected_previous is not None and (
                            response["previous_response_id"] != expected_previous["id"]
                            or response["expected_previous_sha256"] != expected_previous["response_sha256"]
                            or payload["expected_current_response"] != {
                                "response_id": expected_previous["id"],
                                "response_sha256": expected_previous["response_sha256"],
                            }
                        ))
                        or (request["outcome_class"] == "approval-required" and response["responder_kind"] != "human")
                    ):
                        raise ValueError("response chain is invalid")
                    previous_response = response
                    latest_response = response
                head = connection.execute(
                    "SELECT * FROM intervention_response_heads WHERE request_id=?", (request_id,)
                ).fetchone()
                if latest_response is None:
                    if head is not None:
                        raise ValueError("empty response history has a head")
                elif (
                    head is None or head["current_response_id"] != latest_response["id"]
                    or head["current_response_sha256"] != latest_response["response_sha256"]
                    or int(head["revision_no"]) != int(latest_response["revision_no"])
                ):
                    raise ValueError("response head does not match latest revision")
                closure = connection.execute("SELECT * FROM intervention_closures WHERE request_id=?", (request_id,)).fetchone()
                unit = connection.execute("SELECT * FROM work_units WHERE id=?", (request["work_unit_id"],)).fetchone()
                if unit is None:
                    raise ValueError("request work unit is absent")
                if closure is None:
                    if (
                        unit["current_intervention_id"] != request_id
                        or unit["status"] != request["outcome_class"]
                    ):
                        raise ValueError("unclosed request is not the current unit intervention")
                else:
                    response = connection.execute("SELECT * FROM intervention_responses WHERE id=?", (closure["response_id"],)).fetchone()
                    if (
                        response is None or head is None or head["current_response_id"] != closure["response_id"]
                        or response["request_id"] != request_id or response["response_sha256"] != closure["response_sha256"]
                        or response["disposition"] != "answered" or unit["current_intervention_id"] == request_id
                    ):
                        raise ValueError("closure is not bound to the answered current response")
            for unit in connection.execute(
                """SELECT u.id,u.goal_id,r.work_unit_id,r.goal_id AS request_goal_id,c.id AS closure_id
                   FROM work_units u JOIN intervention_requests r ON r.id=u.current_intervention_id
                   LEFT JOIN intervention_closures c ON c.request_id=r.id
                   WHERE u.current_intervention_id IS NOT NULL ORDER BY u.id"""
            ):
                if (
                    unit["work_unit_id"] != unit["id"] or unit["request_goal_id"] != unit["goal_id"]
                    or unit["closure_id"] is not None
                ):
                    raise ValueError("work unit points to an unrelated or closed intervention")
            StateStore._assert_intervention_audit_bindings_in_transaction(connection)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error, InterventionError) as error:
            raise StateError(f"intervention integrity verification failed: {error}") from error

    @staticmethod
    def _assert_intervention_audit_bindings_in_transaction(connection: sqlite3.Connection) -> None:
        """Stream exact event-to-row bindings without retaining audit or history rows."""
        def paired(expected_sql: str, audit_sql: str, check: Any) -> None:
            expected_rows = connection.execute(expected_sql)
            audit_rows = connection.execute(audit_sql)
            for expected, event in zip_longest(expected_rows, audit_rows):
                if expected is None or event is None:
                    raise ValueError("intervention audit event count does not match immutable rows")
                payload = _decode(event["payload"], {})
                if not check(expected, event, payload):
                    raise ValueError("intervention audit event does not match its immutable row")

        paired(
            "SELECT * FROM intervention_requests ORDER BY id",
            "SELECT * FROM audit_events WHERE event_type='intervention.requested' ORDER BY json_extract(payload,'$.request_id'),sequence",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "request_id": row["id"], "request_sha256": row["request_sha256"], "attempt_id": row["attempt_id"],
                "producer_id": row["producer_id"], "outcome_class": row["outcome_class"],
                "requires_human_approval": row["outcome_class"] == "approval-required",
                "timestamp": row["created_at"],
            },
        )
        paired(
            "SELECT * FROM intervention_requests ORDER BY id",
            """SELECT * FROM audit_events WHERE event_type='work.finished'
               AND json_type(payload,'$.request_id')='text' ORDER BY json_extract(payload,'$.request_id'),sequence""",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "attempt_id": row["attempt_id"], "outcome": row["outcome_class"],
                "request_id": row["id"], "request_sha256": row["request_sha256"], "timestamp": row["created_at"],
            },
        )
        paired(
            """SELECT s.*,r.goal_id,r.work_unit_id FROM intervention_responses s
               JOIN intervention_requests r ON r.id=s.request_id ORDER BY s.id""",
            "SELECT * FROM audit_events WHERE event_type='intervention.responded' ORDER BY json_extract(payload,'$.response_id'),sequence",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"]
            and payload == {
                "request_id": row["request_id"], "request_sha256": row["request_sha256"], "response_id": row["id"],
                "response_sha256": row["response_sha256"], "revision_no": row["revision_no"],
                "previous_response_id": row["previous_response_id"], "previous_response_sha256": row["expected_previous_sha256"],
                "responder_id": row["responder_id"], "responder_kind": row["responder_kind"], "disposition": row["disposition"],
                "timestamp": row["created_at"],
            },
        )
        paired(
            """SELECT c.*,r.goal_id,r.work_unit_id,r.request_sha256,r.outcome_class,s.revision_no FROM intervention_closures c
               JOIN intervention_requests r ON r.id=c.request_id
               JOIN intervention_responses s ON s.id=c.response_id ORDER BY c.request_id""",
            """SELECT * FROM audit_events WHERE event_type='work.requeued'
               AND json_type(payload,'$.request_id')='text' ORDER BY json_extract(payload,'$.request_id'),sequence""",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "performer_id": row["closed_by"], "previous_status": row["outcome_class"],
                "request_id": row["request_id"], "request_sha256": row["request_sha256"],
                "response_id": row["response_id"], "response_sha256": row["response_sha256"], "response_revision_no": row["revision_no"],
                "closure_id": row["id"], "envelope_sha256": row["envelope_sha256"],
                "evidence_sha256": row["requeue_evidence_sha256"], "timestamp": row["closed_at"],
            },
        )

    @staticmethod
    def _assert_codex_run_integrity_in_transaction(connection: sqlite3.Connection) -> None:
        """Bind every immutable execution receipt to its minimal audit event."""
        def contains_attempt_token(value: str | None, token_hash: str, token_length: int) -> bool:
            if not isinstance(value, str):
                return False
            candidates = [value]
            candidates.extend(value[index:index + token_length] for index in range(max(0, len(value) - token_length + 1)))
            return any(hashlib.sha256(candidate.encode("utf-8")).hexdigest() == token_hash for candidate in candidates)
        def canonical_timestamp(value: Any) -> bool:
            if not isinstance(value, str):
                return False
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                return False
            return parsed.isoformat(timespec="seconds").replace("+00:00", "Z") == value

        def canonical_identifier(value: Any) -> bool:
            try:
                require_identifier(value)
            except IdentifierError:
                return False
            return True

        def canonical_host_name(value: Any, requested_task_name: Any) -> bool:
            return (isinstance(value, str) and len(value) <= 256
                    and re.fullmatch(r"(?:/[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*)*|[a-z][a-z0-9_]*)", value) is not None
                    and value.rsplit("/", 1)[-1] == requested_task_name)

        def bounded_profile(value: Any, *, required: bool) -> bool:
            if value is None:
                return not required
            return isinstance(value, str) and bool(value) and len(value) <= 200 and not any(ord(char) < 32 for char in value)
        def paired(expected_sql: str, audit_sql: str, check: Any) -> None:
            expected_rows = connection.execute(expected_sql)
            audit_rows = connection.execute(audit_sql)
            for expected, event in zip_longest(expected_rows, audit_rows):
                if expected is None or event is None:
                    raise StateError("codex run audit event count does not match immutable rows")
                if not check(expected, event, _decode(event["payload"], {})):
                    raise StateError("codex run audit event does not match immutable row")

        for table in ("codex_run_starts", "codex_run_finishes"):
            orphan = connection.execute(
                f"SELECT receipt.run_id FROM {table} AS receipt WHERE NOT EXISTS "
                "(SELECT 1 FROM codex_run_preparations AS preparation "
                "WHERE preparation.id = receipt.run_id) LIMIT 1"
            ).fetchone()
            if orphan is not None:
                raise StateError("Codex receipt is not bound to a preparation")
        paired(
            "SELECT * FROM codex_run_preparations ORDER BY id",
            "SELECT * FROM audit_events WHERE event_type='codex_run.prepared' ORDER BY json_extract(payload,'$.run_id'),sequence",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "run_id": row["id"], "attempt_id": row["attempt_id"], "run_no": row["run_no"],
                "idempotency_key": row["idempotency_key"], "requested_task_name": row["requested_task_name"],
                "request_sha256": row["request_sha256"], "handoff_sha256": row["handoff_sha256"],
                "plan_sha256": row["plan_sha256"], "brief_sha256": row["brief_sha256"], "prepared_at": row["prepared_at"],
                "preparation_row_sha256": _authority_row_hash("codex_run_preparations", row),
                "attempt_binding_sha256": _attempt_binding_sha256(connection.execute("SELECT * FROM work_attempts WHERE id=?", (row["attempt_id"],)).fetchone()),
            },
        )
        for prep in connection.execute("SELECT * FROM codex_run_preparations ORDER BY attempt_id,run_no"):
            attempt = connection.execute("SELECT * FROM work_attempts WHERE id=?", (prep["attempt_id"],)).fetchone()
            unit = None if attempt is None else connection.execute("SELECT goal_id FROM work_units WHERE id=?", (attempt["work_unit_id"],)).fetchone()
            if attempt is None or unit is None or prep["goal_id"] != unit["goal_id"] or prep["prepared_by"] != attempt["owner_id"] or any(prep[key] != attempt[key] for key in ("work_unit_id", "lease_generation", "repository", "revision", "branch", "workspace")):
                raise StateError("Codex preparation is not bound to its originating attempt")
            expected_name = re.sub(r"[^a-z0-9_]+", "_", f"codex_{hashlib.sha256(prep['attempt_id'].encode('utf-8')).hexdigest()[:16]}_{prep['run_no']}_{prep['role']}".lower()).strip("_")
            digests = (prep["envelope_sha256"], prep["request_sha256"], prep["plan_sha256"], prep["brief_sha256"])
            metadata_values = tuple(value for value in dict(prep).values() if isinstance(value, str))
            attempt_token_hash = attempt["lease_token_hash"]
            if any(contains_attempt_token(value, attempt_token_hash, prep["lease_token_char_length"]) for value in metadata_values):
                raise StateError("Codex preparation contains a lease token")
            if (not isinstance(prep["lease_token_char_length"], int) or not 32 <= prep["lease_token_char_length"] <= 512
                    or prep["requested_task_name"] != expected_name or not canonical_timestamp(prep["prepared_at"])
                    or not all(canonical_identifier(prep[key]) for key in ("id", "attempt_id", "idempotency_key", "prepared_by", "goal_id", "work_unit_id"))
                    or not all(bounded_profile(prep[key], required=key in {"role", "sandbox_mode"}) for key in ("role", "requested_model", "requested_reasoning_effort", "sandbox_mode"))
                    or any(value is not None and (not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 for char in value)) for value in (prep["repository"], prep["revision"], prep["branch"], prep["workspace"]))
                    or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests)
                    or (prep["handoff_sha256"] is not None and re.fullmatch(r"[0-9a-f]{64}", prep["handoff_sha256"]) is None)):
                raise StateError("Codex preparation has invalid immutable metadata")
            prior = connection.execute("SELECT id FROM codex_run_preparations WHERE attempt_id=? AND run_no=?", (prep["attempt_id"], int(prep["run_no"]) - 1)).fetchone()
            if int(prep["run_no"]) < 1 or (int(prep["run_no"]) > 1 and prior is None) or (prior is not None and connection.execute("SELECT 1 FROM codex_run_finishes WHERE run_id=?", (prior["id"],)).fetchone() is None):
                raise StateError("Codex preparation sequence is invalid")
            event = connection.execute("SELECT sequence FROM audit_events WHERE event_type='codex_run.prepared' AND json_extract(payload,'$.run_id')=?", (prep["id"],)).fetchone()
            contract = None if event is None else connection.execute("SELECT payload FROM audit_events WHERE goal_id=? AND event_type='goal.contract_defined' AND sequence<? ORDER BY sequence DESC LIMIT 1", (prep["goal_id"], event["sequence"])).fetchone()
            if contract is None or _decode(contract["payload"], {}).get("envelope_sha256") != prep["envelope_sha256"]:
                raise StateError("Codex preparation envelope does not match preceding contract")
        for receipt in connection.execute(
            """SELECT s.host_canonical_name,s.host_agent_id,s.observed_by,a.lease_token_hash,p.lease_token_char_length
               FROM codex_run_starts s JOIN codex_run_preparations p ON p.id=s.run_id
               JOIN work_attempts a ON a.id=p.attempt_id"""
        ):
            if any(contains_attempt_token(receipt[key], receipt["lease_token_hash"], receipt["lease_token_char_length"])
                   for key in ("host_canonical_name", "host_agent_id", "observed_by")):
                raise StateError("Codex start receipt contains a lease token")
        for receipt in connection.execute(
            """SELECT f.observed_by,a.lease_token_hash,p.lease_token_char_length FROM codex_run_finishes f
               JOIN codex_run_preparations p ON p.id=f.run_id JOIN work_attempts a ON a.id=p.attempt_id"""
        ):
            if contains_attempt_token(receipt["observed_by"], receipt["lease_token_hash"], receipt["lease_token_char_length"]):
                raise StateError("Codex finish receipt contains a lease token")
        for receipt in connection.execute(
            "SELECT s.*,p.requested_task_name FROM codex_run_starts s JOIN codex_run_preparations p ON p.id=s.run_id"
        ):
            if (not canonical_identifier(receipt["run_id"]) or not canonical_identifier(receipt["observed_by"])
                    or (receipt["host_agent_id"] is not None and not canonical_identifier(receipt["host_agent_id"]))
                    or not canonical_host_name(receipt["host_canonical_name"], receipt["requested_task_name"])
                    or not canonical_timestamp(receipt["recorded_at"])):
                raise StateError("Codex start receipt has invalid persisted metadata")
        for receipt in connection.execute("SELECT * FROM codex_run_finishes"):
            try:
                from .codex_runs import CodexRunError, validate_finish
                validate_finish(outcome=receipt["outcome"], result_status=receipt["result_status"], result_sha256=receipt["result_sha256"], usage_status=receipt["usage_status"], input_tokens=receipt["input_tokens"], output_tokens=receipt["output_tokens"])
            except (CodexRunError, ValueError, TypeError) as error:
                raise StateError("Codex finish receipt has invalid persisted metadata") from error
            if (not canonical_identifier(receipt["run_id"]) or not canonical_identifier(receipt["observed_by"])
                    or not canonical_timestamp(receipt["recorded_at"])):
                raise StateError("Codex finish receipt has invalid persisted metadata")
        paired(
            "SELECT s.*,p.goal_id,p.work_unit_id FROM codex_run_starts s JOIN codex_run_preparations p ON p.id=s.run_id ORDER BY s.run_id",
            "SELECT * FROM audit_events WHERE event_type='codex_run.started' ORDER BY json_extract(payload,'$.run_id'),sequence",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "run_id": row["run_id"], "host_canonical_name": row["host_canonical_name"],
                "host_agent_id": row["host_agent_id"], "observed_by": row["observed_by"], "recorded_at": row["recorded_at"],
            },
        )
        paired(
            "SELECT f.*,p.goal_id,p.work_unit_id FROM codex_run_finishes f JOIN codex_run_preparations p ON p.id=f.run_id ORDER BY f.run_id",
            "SELECT * FROM audit_events WHERE event_type='codex_run.finished' ORDER BY json_extract(payload,'$.run_id'),sequence",
            lambda row, event, payload: event["goal_id"] == row["goal_id"] and event["work_unit_id"] == row["work_unit_id"] and payload == {
                "run_id": row["run_id"], "outcome": row["outcome"], "result_status": row["result_status"],
                "result_sha256": row["result_sha256"], "usage_status": row["usage_status"],
                "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
                "observed_by": row["observed_by"], "recorded_at": row["recorded_at"],
            },
        )
        event_sequences = {
            (row["event_type"], _decode(row["payload"], {}).get("run_id")): int(row["sequence"])
            for row in connection.execute("SELECT sequence,event_type,payload FROM audit_events WHERE event_type IN ('codex_run.prepared','codex_run.started','codex_run.finished')")
        }
        for prep in connection.execute("SELECT id,attempt_id,run_no FROM codex_run_preparations"):
            prepared_sequence = event_sequences.get(("codex_run.prepared", prep["id"]))
            started_sequence = event_sequences.get(("codex_run.started", prep["id"]))
            finished_sequence = event_sequences.get(("codex_run.finished", prep["id"]))
            if prepared_sequence is None:
                raise StateError("Codex preparation lacks an audit event")
            if started_sequence is not None and prepared_sequence >= started_sequence:
                raise StateError("Codex start audit event precedes preparation")
            if finished_sequence is not None and (started_sequence is None or started_sequence >= finished_sequence):
                raise StateError("Codex finish audit event precedes start")
            if int(prep["run_no"]) > 1:
                prior = connection.execute("SELECT id FROM codex_run_preparations WHERE attempt_id=? AND run_no=?", (prep["attempt_id"], int(prep["run_no"]) - 1)).fetchone()
                prior_finished = None if prior is None else event_sequences.get(("codex_run.finished", prior["id"]))
                if prior_finished is None or prior_finished >= prepared_sequence:
                    raise StateError("Codex preparation precedes prior finish")
    @staticmethod
    def _seal_current_state_in_transaction(
        connection: sqlite3.Connection, timestamp: str, *, existing_only: bool = False,
    ) -> None:
        """Append seals only for changed/new rows, then commit one full-state manifest."""
        for table, row_id, row in StateStore._iter_authoritative_rows(
            connection, existing_only=existing_only,
        ):
            digest = _authority_row_hash(table, row)
            if StateStore._latest_seal_hash(connection, table, row_id) != digest:
                StateStore._seal_authority_row_in_transaction(connection, table, row_id, row, timestamp)
        manifest = StateStore._state_manifest_hash(connection, existing_only=existing_only)
        if StateStore._latest_seal_hash(connection, _STATE_MANIFEST_TABLE, _STATE_MANIFEST_ID) != manifest:
            prior = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM authority_seals WHERE table_name=? AND row_id=?",
                (_STATE_MANIFEST_TABLE, _STATE_MANIFEST_ID),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO authority_seals(table_name,row_id,version,row_hash,sealed_at) VALUES(?,?,?,?,?)",
                (_STATE_MANIFEST_TABLE, _STATE_MANIFEST_ID, int(prior) + 1, manifest, timestamp),
            )

    @staticmethod
    def _checkpoint_seal_id(goal_id: str, checkpoint_id: str) -> str:
        """Make a collision-free, internal authority-seal key for a checkpoint."""
        return _encode([goal_id, checkpoint_id])

    @staticmethod
    def _verify_goal_checkpoints_in_transaction(
        connection: sqlite3.Connection, goal_id: str, contract: dict[str, Any]
    ) -> list[sqlite3.Row]:
        """Prove the persisted checkpoint gate still exactly mirrors its contract.

        A table mutation alone must never allow a later checkpoint to become
        claimable.  Each row therefore has both an exact contract position and
        a latest immutable seal.
        """
        expected = list(contract["checkpoints"])
        rows = connection.execute(
            "SELECT * FROM goal_checkpoints WHERE goal_id=? ORDER BY position", (goal_id,)
        ).fetchall()
        actual = [(row["checkpoint_id"], int(row["position"])) for row in rows]
        if actual != [(checkpoint_id, position) for position, checkpoint_id in enumerate(expected)]:
            raise StateError("persisted checkpoints do not match the immutable authority envelope")
        for row in rows:
            if row["status"] not in {"pending", "reached"}:
                raise StateError("persisted checkpoint has an invalid status")
            if row["status"] == "pending" and (row["evidence_json"] is not None or row["reached_at"] is not None):
                raise StateError("pending checkpoint has reached evidence")
            if row["status"] == "reached" and (row["evidence_json"] is None or row["reached_at"] is None):
                raise StateError("reached checkpoint lacks evidence")
            seal = connection.execute(
                "SELECT row_hash FROM authority_seals WHERE table_name='goal_checkpoints' AND row_id=? ORDER BY version DESC LIMIT 1",
                (StateStore._checkpoint_seal_id(goal_id, row["checkpoint_id"]),),
            ).fetchone()
            if seal is None or seal["row_hash"] != _authority_row_hash("goal_checkpoints", row):
                raise StateError("checkpoint authority row is unsealed or tampered")
        return rows

    @staticmethod
    def _dependencies_complete_in_transaction(connection: sqlite3.Connection, goal_id: str) -> None:
        """Fail closed until every declared prerequisite is durably complete."""
        contract_row = connection.execute(
            "SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if contract_row is None:
            raise StateError("goal lacks an authority envelope")
        try:
            from .authority import validate_authority_envelope
            contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
        except ValueError as error:
            raise StateError(f"stored authority envelope is invalid: {error}") from error
        for dependency_id in contract["dependencies"]:
            dependency = connection.execute("SELECT status FROM goals WHERE id=?", (dependency_id,)).fetchone()
            if dependency is None:
                raise StateError(f"goal dependency does not exist: {dependency_id}")
            if dependency["status"] != "complete":
                raise StateError(f"goal dependency is not complete: {dependency_id}")

    def get_goal_checkpoints(self, goal_id: str) -> list[dict[str, Any]]:
        """Return the durable ordered checkpoint ledger for human status views."""
        goal_id = _identifier(goal_id, label="goal_id")
        self._ensure()
        with self._connection(write=False) as connection:
            return [_row(row) or {} for row in connection.execute(
                "SELECT goal_id,checkpoint_id,position,status,evidence_json,reached_at FROM goal_checkpoints WHERE goal_id=? ORDER BY position",
                (goal_id,),
            )]

    def create_goal(self, *, title: str, description: str, goal_id: str | None = None,
                    priority: int = 0, authority: dict[str, Any] | None = None,
                    acceptance: list[str] | None = None, budget_tokens: int | None = None) -> dict[str, Any]:
        if not title.strip() or not description.strip():
            raise StateError("Goal title and description must be non-empty")
        if budget_tokens is not None and budget_tokens < 0:
            raise StateError("budget_tokens must be non-negative")
        identifier, timestamp = _identifier(goal_id or f"goal-{uuid4().hex[:12]}", label="goal_id"), _now()
        with self._connection() as connection:
            self._prepare_write(connection)
            try:
                connection.execute(
                    """INSERT INTO goals VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)""",
                    (identifier, title, description, priority, _encode(authority or {}), _encode(acceptance or []), budget_tokens, timestamp, timestamp),
                )
                connection.execute(
                    "INSERT INTO budgets(goal_id,total_tokens,updated_at) VALUES(?,?,?)",
                    (identifier, budget_tokens, timestamp),
                )
                self._append_event_in_transaction(connection, "goal.created", goal_id=identifier, payload={"title": title})
            except sqlite3.IntegrityError as error:
                raise StateError(f"Goal already exists: {identifier}") from error
        return self.get_goal(identifier) or {}

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        goal_id = _identifier(goal_id, label="goal_id")
        self._ensure()
        with self._connection(write=False) as connection:
            result = _row(connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        if result is not None:
            _persisted_identifier(result["id"], label="goal_id")
        return result

    def list_goals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self._ensure()
        with self._connection(write=False) as connection:
            query, params = "SELECT * FROM goals", ()
            if status is not None:
                query, params = f"{query} WHERE status = ?", (status,)
            results = [_row(row) for row in connection.execute(f"{query} ORDER BY priority DESC, created_at", params)]
        for result in results:
            _persisted_identifier(result["id"], label="goal_id")
        return results

    def set_goal_status(self, goal_id: str, status: str) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        if status != "planned":
            raise StateError("Stage 1 goals are planned-only; lifecycle activation requires the Stage 3 authority contract")
        with self._connection() as connection:
            self._prepare_write(connection)
            changed = connection.execute("UPDATE goals SET status=?, updated_at=? WHERE id=?", (status, _now(), goal_id)).rowcount
            if not changed:
                raise StateError(f"Unknown goal: {goal_id}")
            self._append_event_in_transaction(connection, "goal.status_changed", goal_id=goal_id, payload={"status": status})
        return self.get_goal(goal_id) or {}

    @staticmethod
    def _work_dependency_checkpoint_positions_in_transaction(
        connection: sqlite3.Connection, goal_id: str, *, require_seals: bool = True,
    ) -> dict[str, int]:
        """Return the immutable checkpoint order, or an empty order for plain goals."""
        contract_row = connection.execute(
            "SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if contract_row is None:
            return {}
        try:
            from .authority import validate_authority_envelope
            contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
        except ValueError as error:
            raise StateError(f"stored authority envelope is invalid: {error}") from error
        checkpoints = list(contract["checkpoints"])
        if checkpoints and require_seals:
            StateStore._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
        return {checkpoint_id: position for position, checkpoint_id in enumerate(checkpoints)}

    @staticmethod
    def _assert_work_dependency_checkpoint_relation(
        dependent: sqlite3.Row | dict[str, Any], prerequisite: sqlite3.Row | dict[str, Any],
        checkpoint_positions: dict[str, int],
    ) -> None:
        dependent_checkpoint = dict(dependent)["checkpoint_id"]
        prerequisite_checkpoint = dict(prerequisite)["checkpoint_id"]
        if not checkpoint_positions:
            if dependent_checkpoint is not None or prerequisite_checkpoint is not None:
                raise StateError("noncheckpoint work unit dependencies require null checkpoint ids")
            return
        if (
            dependent_checkpoint not in checkpoint_positions
            or prerequisite_checkpoint not in checkpoint_positions
        ):
            raise StateError("work unit dependency has an invalid checkpoint id")
        if checkpoint_positions[prerequisite_checkpoint] > checkpoint_positions[dependent_checkpoint]:
            raise StateError("work unit prerequisite cannot be in a later checkpoint")

    @staticmethod
    def _assert_work_unit_checkpoint_binding(
        unit: sqlite3.Row | dict[str, Any], checkpoint_positions: dict[str, int],
    ) -> None:
        checkpoint_id = dict(unit)["checkpoint_id"]
        if not checkpoint_positions and checkpoint_id is not None:
            raise StateError("noncheckpoint work unit has a checkpoint id")
        if checkpoint_positions and checkpoint_id not in checkpoint_positions:
            raise StateError("work unit has an invalid checkpoint id")

    @staticmethod
    def _work_prerequisite_state_in_transaction(
        connection: sqlite3.Connection, work_unit_id: str,
    ) -> dict[str, Any]:
        """Read one unit's direct readiness while proving its ancestry is sound.

        This helper deliberately checks only the requested unit's prerequisite
        ancestry. Callers that need a whole-goal report use the one-pass graph
        validator below.
        """
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        try:
            root = connection.execute(
                "SELECT id,goal_id,status,checkpoint_id FROM work_units WHERE id=?", (work_unit_id,)
            ).fetchone()
            if root is None:
                raise StateError(f"Unknown work unit: {work_unit_id}")
            goal_id = _persisted_identifier(root["goal_id"], label="goal_id")
            checkpoint_positions = StateStore._work_dependency_checkpoint_positions_in_transaction(
                connection, goal_id,
            )
            colors: dict[str, int] = {}
            root_prerequisites: list[dict[str, Any]] = []
            stack: list[tuple[sqlite3.Row | dict[str, Any], bool]] = [(root, False)]
            while stack:
                unit, exiting = stack.pop()
                identifier = _persisted_identifier(unit["id"], label="work_unit_id")
                if exiting:
                    colors[identifier] = 2
                    continue
                if unit["goal_id"] != goal_id:
                    raise StateError("work unit dependency crosses goals")
                if unit["status"] not in WORK_UNIT_STATUSES:
                    raise StateError("work unit dependency has an invalid status")
                StateStore._assert_work_unit_checkpoint_binding(unit, checkpoint_positions)
                if colors.get(identifier) == 1:
                    raise StateError("work unit dependency graph contains a cycle")
                if colors.get(identifier) == 2:
                    continue
                colors[identifier] = 1
                dependencies = connection.execute(
                    "SELECT d.prerequisite_id,p.id,p.goal_id,p.status,p.checkpoint_id "
                    "FROM work_unit_dependencies d "
                    "LEFT JOIN work_units p ON p.id=d.prerequisite_id "
                    "WHERE d.work_unit_id=? ORDER BY d.prerequisite_id",
                    (identifier,),
                ).fetchall()
                if len(dependencies) > 64:
                    raise StateError("work unit has more than 64 prerequisites")
                for dependency in dependencies:
                    if dependency["id"] is None:
                        raise StateError("work unit dependency references a missing prerequisite")
                    prerequisite = {
                        "id": dependency["id"], "goal_id": dependency["goal_id"],
                        "status": dependency["status"], "checkpoint_id": dependency["checkpoint_id"],
                    }
                    StateStore._assert_work_dependency_checkpoint_relation(
                        unit, prerequisite, checkpoint_positions,
                    )
                    if identifier == work_unit_id:
                        root_prerequisites.append({
                            "id": _persisted_identifier(dependency["id"], label="work_unit_id"),
                            "status": dependency["status"],
                            "checkpoint_id": dependency["checkpoint_id"],
                        })
                stack.append((unit, True))
                for dependency in reversed(dependencies):
                    stack.append(({
                        "id": dependency["id"], "goal_id": dependency["goal_id"],
                        "status": dependency["status"], "checkpoint_id": dependency["checkpoint_id"],
                    }, False))
        except sqlite3.OperationalError as error:
            raise StateError("runtime state is missing work unit dependencies") from error
        return {
            "work_unit_id": work_unit_id,
            "prerequisites": root_prerequisites,
            "ready": all(item["status"] == "complete" for item in root_prerequisites),
        }

    @staticmethod
    def _work_dependency_graph_in_transaction(
        connection: sqlite3.Connection, goal_id: str, *, require_checkpoint_seals: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Validate a goal graph in one pass and return report-ready unit data."""
        try:
            checkpoint_positions = StateStore._work_dependency_checkpoint_positions_in_transaction(
                connection, goal_id, require_seals=require_checkpoint_seals,
            )
            units = connection.execute(
                "SELECT id,goal_id,status,checkpoint_id FROM work_units WHERE goal_id=? ORDER BY id",
                (goal_id,),
            ).fetchall()
            graph: dict[str, dict[str, Any]] = {}
            for unit in units:
                identifier = _persisted_identifier(unit["id"], label="work_unit_id")
                if unit["status"] not in WORK_UNIT_STATUSES:
                    raise StateError("work unit dependency has an invalid status")
                StateStore._assert_work_unit_checkpoint_binding(unit, checkpoint_positions)
                graph[identifier] = {
                    "work_unit_id": identifier, "status": unit["status"],
                    "checkpoint_id": unit["checkpoint_id"], "prerequisites": [],
                }
            edges = connection.execute(
                "SELECT d.work_unit_id,d.prerequisite_id,p.id,p.goal_id,p.status,p.checkpoint_id "
                "FROM work_unit_dependencies d "
                "JOIN work_units u ON u.id=d.work_unit_id "
                "LEFT JOIN work_units p ON p.id=d.prerequisite_id "
                "WHERE u.goal_id=? ORDER BY d.work_unit_id,d.prerequisite_id",
                (goal_id,),
            ).fetchall()
            for edge in edges:
                dependent_id = _persisted_identifier(edge["work_unit_id"], label="work_unit_id")
                if dependent_id not in graph or edge["id"] is None:
                    raise StateError("work unit dependency references a missing prerequisite")
                prerequisite_id = _persisted_identifier(edge["id"], label="work_unit_id")
                if edge["goal_id"] != goal_id:
                    raise StateError("work unit dependency crosses goals")
                prerequisite = {
                    "id": prerequisite_id, "goal_id": edge["goal_id"], "status": edge["status"],
                    "checkpoint_id": edge["checkpoint_id"],
                }
                StateStore._assert_work_dependency_checkpoint_relation(
                    graph[dependent_id], prerequisite, checkpoint_positions,
                )
                graph[dependent_id]["prerequisites"].append({
                    "id": prerequisite_id, "status": edge["status"],
                    "checkpoint_id": edge["checkpoint_id"],
                })
                if len(graph[dependent_id]["prerequisites"]) > 64:
                    raise StateError("work unit has more than 64 prerequisites")
            colors: dict[str, int] = {}
            for identifier in graph:
                if colors.get(identifier) == 2:
                    continue
                stack: list[tuple[str, bool]] = [(identifier, False)]
                while stack:
                    current, exiting = stack.pop()
                    if exiting:
                        colors[current] = 2
                        continue
                    if colors.get(current) == 1:
                        raise StateError("work unit dependency graph contains a cycle")
                    if colors.get(current) == 2:
                        continue
                    colors[current] = 1
                    stack.append((current, True))
                    for prerequisite in reversed(graph[current]["prerequisites"]):
                        stack.append((prerequisite["id"], False))
        except sqlite3.OperationalError as error:
            raise StateError("runtime state is missing work unit dependencies") from error
        for value in graph.values():
            value["ready"] = all(item["status"] == "complete" for item in value["prerequisites"])
        return graph

    def work_dependencies(self, goal_id: str, work_unit_id: str | None = None, *,
                          limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Return a sealed, read-only dependency/readiness snapshot for one goal."""
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise StateError("limit must be an integer from 1 to 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise StateError("offset must be an integer from 0 to 1000000")
        if not self.path.is_file():
            raise StateError(f"Runtime database does not exist: {self.path}")
        try:
            with self._connection(write=False) as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != SCHEMA_VERSION:
                    raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(connection)
                if connection.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
                    raise StateError(f"Unknown goal: {goal_id}")
                if work_unit_id is not None:
                    unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
                    if unit is None or unit["goal_id"] != goal_id:
                        raise StateError("work unit belongs to a different or unknown goal")
                graph = self._work_dependency_graph_in_transaction(connection, goal_id)
        except sqlite3.Error as error:
            raise StateError("unable to read work unit dependency state") from error
        entries = [graph[work_unit_id]] if work_unit_id is not None else list(graph.values())
        total = len(entries)
        page = entries[offset:offset + limit]
        return {
            "goal_id": goal_id, "read_only": True,
            "notice": "Ready means only that every direct prerequisite is complete; it does not authorize lifecycle transitions.",
            "units": page, "total": total, "limit": limit, "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < total else None,
        }

    @staticmethod
    def _work_plan_digest(value: Any) -> str:
        return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_work_definition_graph(definitions: dict[str, dict[str, Any]],
                                        checkpoint_positions: dict[str, int]) -> None:
        """Validate one combined immutable definition graph without database I/O."""
        for identifier, unit in definitions.items():
            prerequisites = unit.get("prerequisite_ids", ())
            if len(prerequisites) > 64:
                raise StateError("work unit has more than 64 prerequisites")
            for prerequisite_id in prerequisites:
                prerequisite = definitions.get(prerequisite_id)
                if prerequisite is None:
                    raise StateError("work unit dependency references a missing prerequisite")
                StateStore._assert_work_dependency_checkpoint_relation(unit, prerequisite, checkpoint_positions)
        colors: dict[str, int] = {}
        for root in sorted(definitions):
            if colors.get(root) == 2:
                continue
            stack: list[tuple[str, bool]] = [(root, False)]
            while stack:
                current, exiting = stack.pop()
                if exiting:
                    colors[current] = 2
                    continue
                if colors.get(current) == 1:
                    raise StateError("work unit dependency graph contains a cycle")
                if colors.get(current) == 2:
                    continue
                colors[current] = 1
                stack.append((current, True))
                stack.extend((child, False) for child in reversed(definitions[current].get("prerequisite_ids", ())))

    @staticmethod
    def _insert_work_definitions_in_transaction(connection: sqlite3.Connection, goal_id: str,
                                                definitions: Sequence[dict[str, Any]]) -> None:
        """Insert canonical definitions, edges, and attributable immutable evidence."""
        timestamp = _now()
        for unit in sorted(definitions, key=lambda value: value["id"]):
            connection.execute(
                "INSERT INTO work_units (id,goal_id,title,status,scope,checkpoint_id,created_at,updated_at) VALUES(?,?,?,'planned',?,?,?,?)",
                (unit["id"], goal_id, unit["title"], _encode(unit["scope"]), unit["checkpoint_id"], timestamp, timestamp),
            )
        for unit in sorted(definitions, key=lambda value: value["id"]):
            for prerequisite_id in unit["prerequisite_ids"]:
                connection.execute(
                    "INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)",
                    (unit["id"], prerequisite_id),
                )
        for unit in sorted(definitions, key=lambda value: value["id"]):
            StateStore._append_event_in_transaction(
                connection, "work_unit.created", goal_id=goal_id, work_unit_id=unit["id"],
                payload={"title": unit["title"], "checkpoint_id": unit["checkpoint_id"]},
            )
            if unit["prerequisite_ids"]:
                StateStore._append_event_in_transaction(
                    connection, "work_unit.dependencies_defined", goal_id=goal_id, work_unit_id=unit["id"],
                    payload={"prerequisite_ids": list(unit["prerequisite_ids"])},
                )

    @staticmethod
    def _validate_work_definition_contract_in_transaction(
        connection: sqlite3.Connection, goal_id: str, scope: dict[str, Any] | None,
        checkpoint_id: str | None, *, require_contract: bool, allow_reached_checkpoint: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, int]]:
        """Apply the shared contract/scope/checkpoint gate for one definition."""
        contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        if contract_row is None:
            if require_contract:
                raise StateError("work-plan loading requires a stored goal contract")
            if checkpoint_id is not None:
                raise StateError("work unit checkpoint_id requires an authority envelope")
            return None, {}
        try:
            from .authority import validate_authority_envelope
            contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
        except ValueError as error:
            raise StateError(f"stored authority envelope or work unit scope is invalid: {error}") from error
        if not isinstance(scope, dict) or set(scope) != {"paths", "exclusions"}:
            raise StateError("contracted work units require an explicit closed scope")
        try:
            from .authority import validate_project_scope
            validate_project_scope(scope, label="contracted work unit scope")
        except ValueError as error:
            raise StateError(f"stored authority envelope or work unit scope is invalid: {error}") from error
        if not StateStore._scope_within_contract(scope or {}, contract["scope"]):
            raise StateError("work unit scope is outside the authority envelope")
        checkpoints = contract["checkpoints"]
        positions = {value: position for position, value in enumerate(checkpoints)}
        if checkpoints and checkpoint_id is None:
            raise StateError("checkpointed goals require an explicit checkpoint_id for every work unit")
        if not checkpoints and checkpoint_id is not None or checkpoint_id is not None and checkpoint_id not in positions:
            raise StateError("work unit checkpoint_id is not in this authority envelope")
        if checkpoint_id is not None:
            rows = StateStore._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
            selected = next(row for row in rows if row["checkpoint_id"] == checkpoint_id)
            if selected["status"] != "pending" and not allow_reached_checkpoint:
                raise StateError("work units cannot be added to a reached checkpoint")
        return contract, positions

    def _evaluate_work_plan_in_transaction(self, connection: sqlite3.Connection,
                                           manifest: dict[str, Any]) -> dict[str, Any]:
        """Validate one normalized batch against one sealed ledger snapshot."""
        from .authority import validate_authority_envelope, validate_project_scope
        from .workplans import WorkPlanError, normalize_work_plan, work_plan_sha256

        try:
            manifest = normalize_work_plan(manifest)
        except WorkPlanError:
            raise
        goal_id = manifest["goal_id"]
        goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None:
            raise WorkPlanError("invalid_manifest", f"unknown goal: {goal_id}", details={"goal_id": goal_id})
        contract_row = connection.execute(
            "SELECT version,envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if contract_row is None:
            raise WorkPlanError("contract_required", "work-plan loading requires a stored goal contract", details={"goal_id": goal_id})
        try:
            contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
        except ValueError as error:
            raise WorkPlanError("stored_contract_invalid", f"stored authority envelope is invalid: {error}") from error
        if contract["goal_id"] != goal_id:
            raise WorkPlanError("stored_contract_invalid", "stored authority envelope is bound to another goal")
        if contract_row["version"] != f"v{contract['version']}":
            raise WorkPlanError("stored_contract_invalid", "stored authority envelope has an unsupported persisted version")
        checkpoints = list(contract["checkpoints"])
        checkpoint_positions = {identifier: position for position, identifier in enumerate(checkpoints)}
        try:
            checkpoint_rows = self._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
        except StateError as error:
            raise WorkPlanError("stored_contract_invalid", str(error)) from error
        checkpoint_statuses = {row["checkpoint_id"]: row["status"] for row in checkpoint_rows}
        stored_units = connection.execute(
            "SELECT id,goal_id,title,scope,checkpoint_id FROM work_units WHERE goal_id=? ORDER BY id", (goal_id,)
        ).fetchall()
        stored_edges: dict[str, list[str]] = {row["id"]: [] for row in stored_units}
        for row in connection.execute(
            "SELECT d.work_unit_id,d.prerequisite_id FROM work_unit_dependencies d "
            "JOIN work_units u ON u.id=d.work_unit_id WHERE u.goal_id=? ORDER BY d.work_unit_id,d.prerequisite_id", (goal_id,)
        ):
            stored_edges[row["work_unit_id"]].append(row["prerequisite_id"])
        stored_definitions: dict[str, dict[str, Any]] = {}
        for row in stored_units:
            # Bind every immutable definition without applying manifest-only
            # limits to unrelated legacy rows. Exact requested rows receive a
            # narrower canonical comparison below.
            stored_definitions[row["id"]] = {
                "id": row["id"], "title": row["title"], "scope": _decode(row["scope"], {}),
                "checkpoint_id": row["checkpoint_id"], "prerequisite_ids": sorted(stored_edges[row["id"]]),
            }
        proposed = {unit["id"]: unit for unit in manifest["units"]}
        create_ids: list[str] = []
        unchanged_ids: list[str] = []
        for identifier, unit in proposed.items():
            existing = stored_definitions.get(identifier)
            if existing is None:
                elsewhere = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (identifier,)).fetchone()
                if elsewhere is not None:
                    raise WorkPlanError("conflict", f"work unit id belongs to another goal: {identifier}", details={"unit_id": identifier})
                create_ids.append(identifier)
            else:
                try:
                    raw_scope = existing["scope"]
                    validate_project_scope(raw_scope, label=f"stored work unit {identifier} scope")
                    comparable = {
                        "id": existing["id"], "title": existing["title"],
                        "scope": {"paths": sorted(raw_scope["paths"], key=str.casefold), "exclusions": sorted(raw_scope["exclusions"], key=str.casefold)},
                        "checkpoint_id": existing["checkpoint_id"], "prerequisite_ids": sorted(existing["prerequisite_ids"]),
                    }
                except (ValueError, TypeError) as error:
                    raise WorkPlanError("existing_definition_invalid", f"stored work unit {identifier} is invalid: {error}") from error
                if comparable == unit:
                    unchanged_ids.append(identifier)
                    continue
                differing = sorted(key for key in ("title", "scope", "checkpoint_id", "prerequisite_ids") if comparable[key] != unit[key])
                raise WorkPlanError("conflict", f"existing work unit conflicts with manifest: {identifier}", details={"unit_id": identifier, "fields": differing})
        # Reuse the same contracted scope and checkpoint gate as the legacy
        # single-unit API. Exact existing entries may remain after a checkpoint
        # was reached because this operation is not adding work at that gate.
        for identifier, unit in proposed.items():
            try:
                self._validate_work_definition_contract_in_transaction(
                    connection, goal_id, unit["scope"], unit["checkpoint_id"], require_contract=True,
                    allow_reached_checkpoint=identifier in unchanged_ids,
                )
            except StateError as error:
                message = str(error)
                code = "scope_outside_envelope" if "scope is outside" in message else (
                    "checkpoint_reached" if "reached checkpoint" in message else "checkpoint_invalid"
                )
                raise WorkPlanError(code, message, details={"unit_id": identifier}) from error
        if create_ids and goal["status"] not in {"planned", "active"}:
            raise WorkPlanError("goal_not_accepting_work", "goal is not accepting new work", details={"status": goal["status"]})
        for identifier in create_ids:
            checkpoint_id = proposed[identifier]["checkpoint_id"]
            if checkpoint_id is not None and checkpoint_statuses[checkpoint_id] != "pending":
                raise WorkPlanError("checkpoint_reached", f"work unit checkpoint has already been reached: {checkpoint_id}", details={"unit_id": identifier, "checkpoint_id": checkpoint_id})
        all_units = {**stored_definitions, **proposed}
        for identifier in proposed:
            for prerequisite_id in proposed[identifier]["prerequisite_ids"]:
                prerequisite = all_units.get(prerequisite_id)
                if prerequisite is None:
                    other = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (prerequisite_id,)).fetchone()
                    if other is not None and other["goal_id"] != goal_id:
                        raise WorkPlanError("cross_goal_prerequisite", f"prerequisite belongs to another goal: {prerequisite_id}")
                    raise WorkPlanError("missing_prerequisite", f"unknown prerequisite work unit: {prerequisite_id}")
                try:
                    self._assert_work_dependency_checkpoint_relation(proposed[identifier], prerequisite, checkpoint_positions)
                except StateError as error:
                    raise WorkPlanError("checkpoint_invalid", str(error), details={"unit_id": identifier, "prerequisite_id": prerequisite_id}) from error
        try:
            self._validate_work_definition_graph(all_units, checkpoint_positions)
        except StateError as error:
            code = "cycle" if "cycle" in str(error) else "checkpoint_invalid"
            raise WorkPlanError(code, str(error)) from error
        pending = set(proposed)
        waves: list[list[str]] = []
        while pending:
            wave = sorted(identifier for identifier in pending if not (set(proposed[identifier]["prerequisite_ids"]) & pending))
            if not wave:  # Covered above; retained as a defensive totality check.
                raise WorkPlanError("cycle", "work-plan dependency graph contains a cycle")
            waves.append(wave)
            pending.difference_update(wave)
        external_prerequisites = sorted({prerequisite for unit in proposed.values() for prerequisite in unit["prerequisite_ids"] if prerequisite not in proposed})
        definition_graph = {
            "units": [stored_definitions[key] for key in sorted(stored_definitions)],
            "edges": [[identifier, prerequisite] for identifier in sorted(stored_edges) for prerequisite in stored_edges[identifier]],
        }
        manifest_sha256 = work_plan_sha256(manifest)
        runtime_binding_sha256 = self._work_plan_digest({"database": str(self.path.resolve()), "schema_version": SCHEMA_VERSION})
        contract_binding = {"version": contract_row["version"], "envelope_sha256": contract_row["envelope_sha256"]}
        gate = {"required": bool(create_ids)}
        if create_ids:
            gate.update({"goal_accepts_new_work": goal["status"] in {"planned", "active"}, "checkpoints": {identifier: checkpoint_statuses[identifier] for identifier in sorted({proposed[item]["checkpoint_id"] for item in create_ids if proposed[item]["checkpoint_id"] is not None})}})
        preview_sha256 = self._work_plan_digest({"kind": "tasktra.work-plan-preview", "version": 1, "manifest_sha256": manifest_sha256, "runtime_binding_sha256": runtime_binding_sha256, "contract_binding": contract_binding, "existing_definition_graph_sha256": self._work_plan_digest(definition_graph), "creation_gate_sha256": self._work_plan_digest(gate)})
        return {"manifest": manifest, "manifest_sha256": manifest_sha256, "preview_sha256": preview_sha256,
                "runtime_binding_sha256": runtime_binding_sha256, "contract_binding": contract_binding,
                "create_ids": create_ids, "unchanged_ids": unchanged_ids, "dependency_waves": waves,
                "external_prerequisite_ids": external_prerequisites,
                "counts": {"units": len(proposed), "edges": sum(len(unit["prerequisite_ids"]) for unit in proposed.values()), "create": len(create_ids), "unchanged": len(unchanged_ids), "conflict": 0}}

    @staticmethod
    def _work_plan_report(evaluation: dict[str, Any], *, read_only: bool) -> dict[str, Any]:
        return {"kind": "tasktra.work-plan-preview", "version": 1, "goal_id": evaluation["manifest"]["goal_id"], "read_only": read_only,
                "manifest_sha256": evaluation["manifest_sha256"], "preview_sha256": evaluation["preview_sha256"],
                "runtime_binding_sha256": evaluation["runtime_binding_sha256"], "contract_binding": evaluation["contract_binding"],
                "normalized_units": evaluation["manifest"]["units"], "dependency_waves": evaluation["dependency_waves"],
                "external_prerequisite_ids": evaluation["external_prerequisite_ids"], "counts": evaluation["counts"],
                "notice": "Dependency waves are structural only. Loading a plan grants no authority and does not make a unit claimable."}

    def preview_work_plan(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Validate a plan against one verified read-only SQLite snapshot."""
        from .workplans import WorkPlanError, normalize_work_plan
        manifest = normalize_work_plan(manifest)
        if not self.path.is_file():
            raise WorkPlanError("runtime_mismatch", f"runtime database does not exist: {self.path}")
        try:
            with self._connection(write=False) as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != SCHEMA_VERSION:
                    raise WorkPlanError("runtime_mismatch", f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(connection)
                evaluation = self._evaluate_work_plan_in_transaction(connection, manifest)
        except WorkPlanError:
            raise
        except StateError as error:
            raise WorkPlanError("ledger_integrity", str(error)) from error
        except sqlite3.Error as error:
            raise WorkPlanError("runtime_mismatch", "unable to read work-plan runtime state") from error
        return self._work_plan_report(evaluation, read_only=True)

    def apply_work_plan(self, manifest: dict[str, Any], *, expected_preview_sha256: str) -> dict[str, Any]:
        """Atomically insert every new immutable definition after exact revalidation."""
        from .workplans import WorkPlanError, normalize_work_plan
        manifest = normalize_work_plan(manifest)
        if not isinstance(expected_preview_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_preview_sha256):
            raise WorkPlanError("invalid_manifest", "expected_preview_sha256 must be a lowercase SHA-256 digest")
        if not self.path.is_file():
            raise WorkPlanError("runtime_mismatch", f"runtime database does not exist: {self.path}")
        try:
            with self._connection() as connection:
                self._prepare_write(connection)
                evaluation = self._evaluate_work_plan_in_transaction(connection, manifest)
                if evaluation["preview_sha256"] != expected_preview_sha256:
                    raise WorkPlanError("stale_preview", "work-plan preview no longer matches current runtime state", details={"expected_preview_sha256": expected_preview_sha256, "actual_preview_sha256": evaluation["preview_sha256"]})
                create_ids = set(evaluation["create_ids"])
                new_definitions = [unit for unit in evaluation["manifest"]["units"] if unit["id"] in create_ids]
                self._insert_work_definitions_in_transaction(connection, evaluation["manifest"]["goal_id"], new_definitions)
                if evaluation["create_ids"]:
                    self._work_dependency_graph_in_transaction(connection, evaluation["manifest"]["goal_id"])
                    self._append_event_in_transaction(connection, "work_plan.applied", goal_id=evaluation["manifest"]["goal_id"], payload={"manifest_sha256": evaluation["manifest_sha256"], "preview_sha256": evaluation["preview_sha256"], "created": len(evaluation["create_ids"]), "unchanged": len(evaluation["unchanged_ids"]), "units": evaluation["counts"]["units"], "edges": evaluation["counts"]["edges"], "created_ids_sha256": self._work_plan_digest(evaluation["create_ids"])})
        except WorkPlanError:
            raise
        except StateError as error:
            raise WorkPlanError("ledger_integrity", str(error)) from error
        except sqlite3.Error as error:
            raise WorkPlanError("ledger_failure", "unable to apply work-plan runtime state") from error
        return self._work_plan_report(evaluation, read_only=False)

    def create_work_unit(self, *, goal_id: str, title: str, scope: dict[str, Any] | None = None,
                         work_unit_id: str | None = None, checkpoint_id: str | None = None,
                         prerequisite_ids: Sequence[str] = (),
                         verification_policy: str = "implementation-review",
                         acceptance_checks: list[list[str]] | None = None) -> dict[str, Any]:
        if not title.strip():
            raise StateError("Work unit title must be non-empty")
        if isinstance(prerequisite_ids, (str, bytes)) or not isinstance(prerequisite_ids, Sequence):
            raise StateError("prerequisite_ids must be a sequence of work unit ids")
        goal_id = _identifier(goal_id, label="goal_id")
        identifier, timestamp = _identifier(work_unit_id or f"work-{uuid4().hex[:12]}", label="work_unit_id"), _now()
        checkpoint_id = _optional_identifier(checkpoint_id, label="checkpoint_id")
        prerequisites = [_identifier(value, label="prerequisite_id") for value in prerequisite_ids]
        if len(prerequisites) > 64:
            raise StateError("work unit may have at most 64 prerequisites")
        if len(set(prerequisites)) != len(prerequisites):
            raise StateError("work unit prerequisites must not contain duplicates")
        prerequisites.sort()
        if identifier in prerequisites:
            raise StateError("work unit cannot depend on itself")
        if verification_policy not in VERIFICATION_POLICIES:
            raise StateError("work-unit verification_policy is not supported")
        normalized_checks = _acceptance_checks(
            acceptance_checks,
            required=verification_policy == "implementation-deterministic-review",
        )
        with self._connection() as connection:
            self._prepare_write(connection)
            goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise StateError(f"Unknown goal: {goal_id}")
            if goal["status"] not in {"planned", "active"}:
                raise StateError("work units may only be created for planned or active goals")
            contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
            persisted_scope = scope or {}
            checkpoint_positions: dict[str, int] = {}
            if contract_row is not None:
                _, checkpoint_positions = self._validate_work_definition_contract_in_transaction(
                    connection, goal_id, scope, checkpoint_id, require_contract=False,
                    allow_reached_checkpoint=False,
                )
                if verification_policy != "implementation-review":
                    contract = _decode(contract_row["contract"], {})
                    from .authority import verification_policy_allowed
                    if not verification_policy_allowed(contract, verification_policy):
                        raise StateError("authority envelope does not explicitly permit this verification policy")
            elif verification_policy != "implementation-review":
                raise StateError("non-legacy verification policies require an authority envelope")
            elif checkpoint_id is not None:
                raise StateError("work unit checkpoint_id requires an authority envelope")
            try:
                prerequisite_rows: list[sqlite3.Row] = []
                for prerequisite_id in prerequisites:
                    prerequisite = connection.execute(
                        "SELECT id,goal_id,status,checkpoint_id FROM work_units WHERE id=?",
                        (prerequisite_id,),
                    ).fetchone()
                    if prerequisite is None:
                        raise StateError(f"Unknown prerequisite work unit: {prerequisite_id}")
                    if prerequisite["goal_id"] != goal_id:
                        raise StateError("work unit prerequisite belongs to a different goal")
                    if prerequisite["status"] not in WORK_UNIT_STATUSES:
                        raise StateError("work unit prerequisite has an invalid status")
                    prerequisite_rows.append(prerequisite)
                definitions = {
                    row["id"]: {"checkpoint_id": row["checkpoint_id"], "prerequisite_ids": []}
                    for row in connection.execute("SELECT id,checkpoint_id FROM work_units WHERE goal_id=?", (goal_id,))
                }
                for edge in connection.execute(
                    "SELECT d.work_unit_id,d.prerequisite_id FROM work_unit_dependencies d JOIN work_units u ON u.id=d.work_unit_id WHERE u.goal_id=?",
                    (goal_id,),
                ):
                    definitions[edge["work_unit_id"]]["prerequisite_ids"].append(edge["prerequisite_id"])
                definitions[identifier] = {"checkpoint_id": checkpoint_id, "prerequisite_ids": prerequisites}
                self._validate_work_definition_graph(definitions, checkpoint_positions)
                self._insert_work_definitions_in_transaction(connection, goal_id, [{
                    "id": identifier, "title": title, "scope": persisted_scope,
                    "checkpoint_id": checkpoint_id, "prerequisite_ids": prerequisites,
                }])
                connection.execute(
                    "UPDATE work_units SET verification_policy=?,acceptance_checks=? WHERE id=?",
                    (verification_policy, _encode(normalized_checks), identifier),
                )
            except sqlite3.IntegrityError as error:
                raise StateError(f"Cannot create work unit {identifier}; verify its id and goal") from error
        return self.get_work_unit(identifier) or {}

    def get_work_unit(self, work_unit_id: str) -> dict[str, Any] | None:
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        self._ensure()
        with self._connection(write=False) as connection:
            result = _row(connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone())
        if result is not None:
            _persisted_identifier(result["id"], label="work_unit_id")
            _persisted_identifier(result["goal_id"], label="goal_id")
            # The structured-intervention pointer is internal ledger state.
            # Operator projections use the sealed relation directly; keeping
            # it out of this longstanding public work-unit shape preserves
            # the closed work-unit contract for existing callers.
            result.pop("current_intervention_id", None)
        return result

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """Return the immutable claim context a host must bind before execution."""
        attempt_id = _identifier(attempt_id, label="attempt_id")
        self._ensure()
        with self._connection(write=False) as connection:
            row = connection.execute(
                """SELECT a.*,u.goal_id,u.title AS work_unit_title,u.scope AS work_unit_scope,
                          u.checkpoint_id,u.verification_policy,u.status AS work_unit_status
                   FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""",
                (attempt_id,),
            ).fetchone()
        return None if row is None else _row(row)

    def work_unit_execution_health(self, work_unit_id: str) -> dict[str, Any]:
        """Report derived execution health without changing goal lifecycle state."""
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        self._ensure()
        with self._connection(write=False) as connection:
            unit = connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None:
                raise StateError(f"Unknown work unit: {work_unit_id}")
            outcomes = [row[0] for row in connection.execute(
                "SELECT outcome_class FROM work_attempts WHERE work_unit_id=? AND outcome_class IS NOT NULL ORDER BY attempt_no",
                (work_unit_id,),
            )]
        status = unit["status"]
        return {
            "work_unit_id": work_unit_id, "status": status,
            "verification_policy": unit["verification_policy"], "attempt_outcomes": outcomes,
            "execution_state": "healthy" if status in {"planned", "eligible", "retry-wait", "leased", "complete"}
            else "needs-authority" if status in {"blocked", "approval-required", "failed", "exhausted"}
            else "inactive",
            "requires_requeue_authority": status in {"blocked", "approval-required", "failed", "exhausted"},
        }

    def goal_execution_health(self, goal_id: str) -> dict[str, Any]:
        """Summarize unit execution health; permanent failures never block a goal by assertion."""
        goal_id = _identifier(goal_id, label="goal_id")
        self._ensure()
        with self._connection(write=False) as connection:
            goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise StateError(f"Unknown goal: {goal_id}")
            units = [row["id"] for row in connection.execute("SELECT id FROM work_units WHERE goal_id=? ORDER BY id", (goal_id,))]
        health = [self.work_unit_execution_health(unit_id) for unit_id in units]
        return {"goal_id": goal_id, "goal_status": goal["status"], "work_units": health,
                "unresolved_work_unit_ids": [item["work_unit_id"] for item in health if item["requires_requeue_authority"]]}

    def assign_work_unit_checkpoint(self, work_unit_id: str, checkpoint_id: str, *, actor_id: str,
                                    actor_kind: str,
                                    at: str | datetime | None = None) -> dict[str, Any]:
        """Explicitly bind a legacy planned unit to one immutable checkpoint.

        Migration intentionally does not infer this binding.  An operator must
        make the decision while the unit is still unleased and auditable.
        """
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        checkpoint_id = _identifier(checkpoint_id, label="checkpoint_id")
        actor_id = _identifier(actor_id, label="actor_id")
        if actor_kind != "human":
            raise StateError("legacy checkpoint assignment requires an explicit human actor")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires explicit migration to {SCHEMA_VERSION}")
            self._assert_audit_chain_in_transaction(connection)
            unit = connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None:
                raise StateError(f"Unknown work unit: {work_unit_id}")
            if unit["status"] not in {"planned", "eligible"} or unit["current_attempt_id"] is not None:
                raise StateError("checkpoint assignment is allowed only for an unleased planned or eligible work unit")
            if unit["checkpoint_id"] is not None:
                raise StateError("work unit is already bound to a checkpoint")
            if self._latest_seal_hash(connection, "work_units", work_unit_id) is not None:
                raise StateError("only an unsealed migrated work unit may be assigned a checkpoint")
            contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (unit["goal_id"],)).fetchone()
            if contract_row is None:
                raise StateError("work unit goal lacks an authority envelope")
            try:
                from .authority import validate_authority_envelope
                contract = validate_authority_envelope(_decode(contract_row["contract"], {}))
            except ValueError as error:
                raise StateError(f"stored authority envelope is invalid: {error}") from error
            if checkpoint_id not in contract["checkpoints"]:
                raise StateError("checkpoint_id is not in this authority envelope")
            checkpoint_rows = connection.execute(
                "SELECT * FROM goal_checkpoints WHERE goal_id=? ORDER BY position", (unit["goal_id"],)
            ).fetchall()
            selected_checkpoint = next(
                (row for row in checkpoint_rows if row["checkpoint_id"] == checkpoint_id), None
            )
            if selected_checkpoint is None or selected_checkpoint["status"] != "pending":
                raise StateError("legacy work may only be assigned to a pending contract checkpoint")
            connection.execute(
                "UPDATE work_units SET checkpoint_id=?,updated_at=? WHERE id=?",
                (checkpoint_id, timestamp, work_unit_id),
            )
            self._append_event_in_transaction(
                connection, "work_unit.checkpoint_assigned", goal_id=unit["goal_id"], work_unit_id=work_unit_id,
                payload={"actor_id": actor_id, "actor_kind": actor_kind, "checkpoint_id": checkpoint_id},
            )
        return self.get_work_unit(work_unit_id) or {}

    def record_approval(self, *, goal_id: str, decision: str, authority_clause: str, rationale: str,
                        approver_id: str, performer_id: str, work_unit_id: str | None = None,
                        receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        approver_id = _identifier(approver_id, label="approver_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if decision not in APPROVAL_DECISIONS:
            raise StateError(f"Invalid approval decision: {decision}")
        if not authority_clause.strip() or not rationale.strip():
            raise StateError("Approval authority_clause and rationale must be non-empty")
        if not approver_id.strip() or not performer_id.strip():
            raise StateError("Approval approver_id and performer_id must be non-empty")
        if approver_id == performer_id:
            raise StateError("An approver cannot approve work they performed")
        identifier, timestamp = f"approval-{uuid4().hex[:12]}", _now()
        with self._connection() as connection:
            self._prepare_write(connection)
            goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise StateError(f"Unknown goal: {goal_id}")
            if goal["status"] != "planned":
                raise StateError("Stage 1 approvals may only be recorded against planned goals")
            if work_unit_id is not None:
                work_unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
                if work_unit is None:
                    raise StateError(f"Unknown work unit: {work_unit_id}")
                if work_unit["goal_id"] != goal_id:
                    raise StateError("Approval work unit belongs to a different goal")
            connection.execute(
                """INSERT INTO approvals(
                    id,goal_id,work_unit_id,decision,authority_clause,rationale,receipt,created_at,
                    approver_id,performer_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identifier, goal_id, work_unit_id, decision, authority_clause, rationale, _encode(receipt or {}), timestamp, approver_id, performer_id),
            )
            self._append_event_in_transaction(
                connection, "approval.recorded", goal_id=goal_id, work_unit_id=work_unit_id,
                payload={"approval_id": identifier, "decision": decision, "approver_id": approver_id, "performer_id": performer_id},
            )
        return self.get_approval(identifier) or {}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        approval_id = _identifier(approval_id, label="approval_id")
        self._ensure()
        with self._connection(write=False) as connection:
            result = _row(connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
        if result is not None:
            _persisted_identifier(result["id"], label="approval_id")
            _persisted_identifier(result["goal_id"], label="goal_id")
            _persisted_optional_identifier(result["work_unit_id"], label="work_unit_id")
        return result

    def define_goal_contract(self, goal_id: str, contract: dict[str, Any], *, actor_id: str,
                             at: str | datetime | None = None) -> dict[str, Any]:
        """Persist the exact v1 authority envelope used by later approvals."""
        goal_id = _identifier(goal_id, label="goal_id")
        actor_id = _identifier(actor_id, label="actor_id")
        if not isinstance(contract, dict):
            raise StateError("goal contract must be an object")
        try:
            from .authority import validate_authority_envelope, serialize_authority_envelope, authority_envelope_sha256
            validate_authority_envelope(contract)
            if contract.get("goal_id") != goal_id:
                raise StateError("authority envelope goal_id does not match goal")
            serialized = serialize_authority_envelope(contract)
            envelope_hash = authority_envelope_sha256(contract)
        except ValueError as error:
            raise StateError(f"goal contract is invalid: {error}") from error
        timestamp = _timestamp(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            goal = connection.execute("SELECT acceptance FROM goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise StateError(f"Unknown goal: {goal_id}")
            goal_state = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()["status"]
            if goal_state != "planned":
                raise StateError("authority envelope may only be defined or redefined while the goal is planned")
            persisted_acceptance = _decode(goal["acceptance"], [])
            envelope_acceptance = [criterion["statement"] for criterion in contract["acceptance_criteria"]]
            if sorted(persisted_acceptance) != sorted(envelope_acceptance):
                raise StateError("authority envelope acceptance criteria must match the goal acceptance exactly")
            if goal_id in contract["dependencies"]:
                raise StateError("authority envelope cannot depend on itself")
            # Replacing a planned-goal envelope must not strand pre-existing
            # scope or checkpoint bindings.  The old implementation rebuilt
            # the checkpoint table unconditionally, making a narrow rewrite
            # such as `.` -> `docs` leave a `src` unit impossible to claim.
            existing_units = connection.execute(
                "SELECT id,scope,checkpoint_id,current_attempt_id,verification_policy FROM work_units WHERE goal_id=?", (goal_id,)
            ).fetchall()
            for unit in existing_units:
                try:
                    unit_scope = _decode(unit["scope"], {})
                except (TypeError, ValueError) as error:
                    raise StateError(f"existing work unit has malformed scope: {unit['id']}") from error
                if not self._scope_within_contract(unit_scope, contract["scope"]):
                    raise StateError("authority envelope would strand an existing work unit scope")
                from .authority import verification_policy_allowed
                if not verification_policy_allowed(contract, unit["verification_policy"]):
                    raise StateError("authority envelope would revoke an existing work unit verification policy")
                checkpoint_id = unit["checkpoint_id"]
                if checkpoint_id is not None and checkpoint_id not in contract["checkpoints"]:
                    raise StateError("authority envelope would strand an existing work unit checkpoint")
                if checkpoint_id is None and contract["checkpoints"]:
                    raise StateError("checkpointed authority cannot strand an unbound existing work unit")
                if unit["current_attempt_id"] is not None:
                    raise StateError("authority envelope cannot be replaced while a work unit is leased")
            usage = connection.execute(
                "SELECT consumed_tokens,reserved_tokens,consumed_attempts,consumed_elapsed_ms FROM budgets WHERE goal_id=?",
                (goal_id,),
            ).fetchone()
            budgets = contract["budgets"]
            live_attempts = connection.execute(
                "SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'",
                (goal_id,),
            ).fetchone()[0]
            live_elapsed = connection.execute(
                "SELECT COALESCE(sum(max(0,CAST((julianday(expires_at)-julianday(acquired_at))*86400000 AS INTEGER))),0) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'",
                (goal_id,),
            ).fetchone()[0]
            if budgets["tokens"] is not None and budgets["tokens"] < usage["consumed_tokens"] + usage["reserved_tokens"]:
                raise StateError("authority token budget cannot be below consumed and reserved usage")
            if budgets["attempts"] < usage["consumed_attempts"]:
                raise StateError("authority attempt budget cannot be below consumed usage")
            if budgets["elapsed_seconds"] * 1000 < usage["consumed_elapsed_ms"] + live_elapsed:
                raise StateError("authority elapsed budget cannot be below consumed and reserved usage")
            if budgets["concurrency"] < live_attempts:
                raise StateError("authority concurrency budget cannot be below active leases")
            connection.execute(
                "INSERT INTO goal_contracts(goal_id,version,contract,envelope_sha256,defined_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET version=excluded.version,contract=excluded.contract,envelope_sha256=excluded.envelope_sha256,defined_by=excluded.defined_by,updated_at=excluded.updated_at",
                (goal_id, f"v{contract['version']}", serialized, envelope_hash, actor_id, timestamp, timestamp),
            )
            contract_row = connection.execute("SELECT * FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
            self._seal_authority_row_in_transaction(connection, "goal_contracts", goal_id, contract_row, timestamp)
            existing_checkpoints = connection.execute(
                "SELECT checkpoint_id,status,evidence_json,reached_at FROM goal_checkpoints WHERE goal_id=? ORDER BY position", (goal_id,)
            ).fetchall()
            # A contract may be re-recorded for a planned goal, but recorded
            # checkpoint evidence is immutable and therefore cannot be erased.
            if any(row["status"] == "reached" for row in existing_checkpoints):
                raise StateError("authority envelope cannot replace reached checkpoints")
            connection.execute("DELETE FROM goal_checkpoints WHERE goal_id=?", (goal_id,))
            for position, checkpoint_id in enumerate(contract["checkpoints"]):
                connection.execute(
                    "INSERT INTO goal_checkpoints(goal_id,checkpoint_id,position,status) VALUES(?,?,?,'pending')",
                    (goal_id, checkpoint_id, position),
                )
                row = connection.execute("SELECT * FROM goal_checkpoints WHERE goal_id=? AND checkpoint_id=?", (goal_id, checkpoint_id)).fetchone()
                self._seal_authority_row_in_transaction(connection, "goal_checkpoints", self._checkpoint_seal_id(goal_id, checkpoint_id), row, timestamp)
            connection.execute("UPDATE budgets SET total_tokens=?,total_attempts=?,total_elapsed_ms=?,max_concurrency=?,updated_at=? WHERE goal_id=?", (budgets["tokens"], budgets["attempts"], budgets["elapsed_seconds"] * 1000, budgets["concurrency"], timestamp, goal_id))
            self._append_event_in_transaction(connection, "goal.contract_defined", goal_id=goal_id,
                                              payload={"actor_id": actor_id, "envelope_sha256": envelope_hash})
        return self.get_goal_contract(goal_id) or {}

    def get_goal_contract(self, goal_id: str) -> dict[str, Any] | None:
        goal_id = _identifier(goal_id, label="goal_id")
        self._ensure()
        with self._connection(write=False) as connection:
            return _row(connection.execute("SELECT * FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone())

    def attest_ledger(self, *, actor_id: str,
                      at: str | datetime | None = None) -> dict[str, Any]:
        """Explicitly validate and seal every migrated authoritative row."""
        actor_id = _identifier(actor_id, label="actor_id")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            # A migration deliberately leaves existing rows unsealed.  The
            # human attestation path is the only way to pass that boundary;
            # validate every authority record first because commit-time state
            # sealing covers the full ledger, not merely this goal.
            self._prepare_write(connection, allow_unsealed=True)
            self._validate_authority_rows_for_attestation(connection)
            self._validate_current_state_for_attestation(connection)
            reviewed = list(self._iter_authoritative_rows(connection))
            manifest_sha256 = self._state_manifest_hash(connection)
            self._append_event_in_transaction(
                connection, "ledger.attested",
                payload={"actor_id": actor_id, "row_count": len(reviewed),
                         "state_manifest_sha256": manifest_sha256},
            )
        return {"actor_id": actor_id, "sealed_row_count": len(reviewed),
                "state_manifest_sha256": manifest_sha256}

    @staticmethod
    def _validate_authority_rows_for_attestation(connection: sqlite3.Connection) -> None:
        """Validate persisted contracts and transition approvals before sealing them.

        This is intentionally stricter than merely checking their JSON shape:
        approvals must still bind to a current valid envelope and an existing
        goal/work unit.  An old malformed record can be revoked or repaired,
        but may not be turned into trusted authority by attestation.
        """
        try:
            from .authority import (
                _validate_scope,
                authority_envelope_sha256,
                resource_scope_within,
                validate_authority_envelope,
                validate_transition_approval,
            )
        except ImportError as error:  # pragma: no cover - packaging failure must fail closed
            raise StateError("authority validation is unavailable") from error
        envelopes: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM goal_contracts"):
            try:
                envelope = validate_authority_envelope(_decode(row["contract"], {}))
            except ValueError as error:
                raise StateError(f"cannot attest invalid authority contract {row['goal_id']}: {error}") from error
            if envelope["goal_id"] != row["goal_id"] or authority_envelope_sha256(envelope) != row["envelope_sha256"]:
                raise StateError(f"cannot attest authority contract with mismatched identity or hash: {row['goal_id']}")
            if row["version"] != f"v{envelope['version']}":
                raise StateError(f"cannot attest authority contract with mismatched version: {row['goal_id']}")
            envelopes[str(row["goal_id"])] = envelope
        for goal_id, envelope in envelopes.items():
            checkpoint_rows = connection.execute(
                "SELECT checkpoint_id,position,status,evidence_json,reached_at FROM goal_checkpoints WHERE goal_id=? ORDER BY position",
                (goal_id,),
            ).fetchall()
            expected_checkpoints = [(checkpoint_id, position) for position, checkpoint_id in enumerate(envelope["checkpoints"])]
            actual_checkpoints = [(row["checkpoint_id"], int(row["position"])) for row in checkpoint_rows]
            if actual_checkpoints != expected_checkpoints:
                raise StateError(f"cannot attest checkpoints that differ from authority contract: {goal_id}")
            for checkpoint in checkpoint_rows:
                if checkpoint["status"] not in {"pending", "reached"}:
                    raise StateError(f"cannot attest invalid checkpoint status: {goal_id}/{checkpoint['checkpoint_id']}")
                if checkpoint["status"] == "pending" and (checkpoint["evidence_json"] is not None or checkpoint["reached_at"] is not None):
                    raise StateError(f"cannot attest pending checkpoint with evidence: {goal_id}/{checkpoint['checkpoint_id']}")
                if checkpoint["status"] == "reached" and (checkpoint["evidence_json"] is None or checkpoint["reached_at"] is None):
                    raise StateError(f"cannot attest reached checkpoint without evidence: {goal_id}/{checkpoint['checkpoint_id']}")
            for unit in connection.execute("SELECT id,scope,checkpoint_id FROM work_units WHERE goal_id=?", (goal_id,)):
                try:
                    scope = _decode(unit["scope"], {})
                except (TypeError, ValueError) as error:
                    raise StateError(f"cannot attest malformed work-unit scope {unit['id']}: {error}") from error
                if not isinstance(scope, dict) or set(scope) != {"paths", "exclusions"} or not isinstance(scope.get("paths"), list) or not scope["paths"] or not isinstance(scope.get("exclusions"), list):
                    raise StateError(f"cannot attest non-closed work-unit scope {unit['id']}")
                try:
                    _validate_scope(scope, label="work-unit scope")
                except ValueError as error:
                    raise StateError(f"cannot attest invalid work-unit scope {unit['id']}: {error}") from error
                if not StateStore._scope_within_contract(scope, envelope["scope"]):
                    raise StateError(f"cannot attest work-unit scope outside authority envelope: {unit['id']}")
                if envelope["checkpoints"] and unit["checkpoint_id"] is None:
                    raise StateError(f"cannot attest checkpointed work unit without an assignment: {unit['id']}")
                if unit["checkpoint_id"] is not None and unit["checkpoint_id"] not in envelope["checkpoints"]:
                    raise StateError(f"cannot attest work unit with invalid checkpoint: {unit['id']}")
        for row in connection.execute("SELECT * FROM transition_approvals"):
            goal_id = str(row["goal_id"])
            envelope = envelopes.get(goal_id)
            if envelope is None:
                raise StateError(f"cannot attest approval without a goal authority envelope: {row['id']}")
            if row["work_unit_id"] is not None:
                unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (row["work_unit_id"],)).fetchone()
                if unit is None or unit["goal_id"] != goal_id:
                    raise StateError(f"cannot attest approval with an invalid work unit: {row['id']}")
            protocol_version = int(row["protocol_version"])
            if protocol_version not in {1, 2, 3, 4}:
                raise StateError(f"cannot attest unsupported transition approval version: {row['id']}")
            record = {
                "kind": "tasktra.transition-approval", "version": protocol_version,
                "approval_id": row["id"], "goal_id": goal_id,
                "work_unit_id": row["work_unit_id"], "action": row["action"],
                "effect": row["effect"], "scope": _decode(row["scope"], {}),
                "envelope_sha256": row["envelope_sha256"], "decision": row["decision"],
                "approver": {"kind": row["approver_kind"], "id": row["approver_id"]},
                "performer_id": row["performer_id"], "authority_clause": row["authority_clause"],
                "evidence": _decode(row["evidence"], []), "valid_until": row["valid_until"],
                "revoked_at": row["revoked_at"],
            }
            if protocol_version in {2, 3, 4}:
                record["resource_scope"] = _decode(row["resource_scope"], None)
            elif row["resource_scope"] is not None:
                raise StateError(f"cannot attest v1 approval with a resource scope: {row['id']}")
            if protocol_version >= 3:
                record["provenance"] = _decode(row["provenance"], {})
            try:
                validate_transition_approval(record)
            except ValueError as error:
                raise StateError(f"cannot attest invalid transition approval {row['id']}: {error}") from error
            if row["envelope_sha256"] == authority_envelope_sha256(envelope):
                if row["action"] not in envelope["allowed_actions"] or row["action"] in envelope["prohibited_actions"]:
                    raise StateError(f"cannot attest current approval for a prohibited action: {row['id']}")
                if row["effect"] not in envelope["allowed_effects"]:
                    raise StateError(f"cannot attest current approval for a disallowed effect: {row['id']}")
                if not StateStore._scope_within_contract(record["scope"], envelope["scope"]):
                    raise StateError(f"cannot attest current approval outside its envelope scope: {row['id']}")
                if protocol_version not in {int(envelope["version"]), 3, 4}:
                    raise StateError(f"cannot attest current approval with a mismatched protocol version: {row['id']}")
                if protocol_version in {2, 3, 4} and row["effect"] not in {"read-only", "local-reversible-write"}:
                    approval_resource = record.get("resource_scope")
                    if not isinstance(approval_resource, dict) or not any(
                        resource_scope_within(approval_resource, allowed)
                        for allowed in envelope["resource_scopes"]
                    ):
                        raise StateError(f"cannot attest current approval outside its resource scope: {row['id']}")

    @staticmethod
    def _validate_current_state_for_attestation(connection: sqlite3.Connection) -> None:
        """Validate migrated lifecycle facts before the complete ledger is sealed."""
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateError("cannot attest state with foreign-key violations")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 14:
            StateStore._assert_codex_run_integrity_in_transaction(connection)
        previous, expected_sequence = _GENESIS_HASH, 1
        for event in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
            digest = _audit_hash(previous, event["sequence"], event["event_type"], event["goal_id"], event["work_unit_id"], event["payload"], event["created_at"])
            if event["sequence"] != expected_sequence or event["previous_hash"] != previous or event["event_hash"] != digest:
                raise StateError("cannot attest state with an invalid audit chain")
            previous, expected_sequence = event["event_hash"], expected_sequence + 1
        last_emergency_stop = connection.execute(
            "SELECT COALESCE(max(sequence),0) FROM audit_events "
            "WHERE event_type='runtime.emergency_stop_set' AND goal_id IS NULL"
        ).fetchone()[0]
        for goal in connection.execute("SELECT * FROM goals"):
            if goal["status"] not in GOAL_STATUSES:
                raise StateError(f"cannot attest invalid goal status: {goal['id']}")
            lifecycle = connection.execute(
                """SELECT sequence,event_type,payload FROM audit_events WHERE goal_id=? AND event_type IN
                   ('goal.created','goal.status_changed','goal.active','goal.draining','goal.paused','goal.stopped','goal.completed')
                   ORDER BY sequence DESC LIMIT 1""",
                (goal["id"],),
            ).fetchone()
            if lifecycle is None:
                raise StateError(f"cannot attest goal without lifecycle evidence: {goal['id']}")
            expected_status = {
                "goal.created": "planned", "goal.active": "active", "goal.draining": "draining", "goal.paused": "paused",
                "goal.stopped": "stopped", "goal.completed": "complete",
            }.get(lifecycle["event_type"])
            if lifecycle["event_type"] == "goal.status_changed":
                expected_status = _decode(lifecycle["payload"], {}).get("status")
            # A global emergency stop pauses only goals active at that point.
            # Clearing the runtime stop does not resume them; a later goal
            # lifecycle event supersedes the global event for that goal.
            if expected_status in {"active", "draining"} and last_emergency_stop > lifecycle["sequence"]:
                expected_status = "paused"
            if goal["status"] != expected_status:
                raise StateError(f"cannot attest goal status without matching lifecycle evidence: {goal['id']}")
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal["id"],)).fetchone()
            if budget is None or any(int(budget[name]) < 0 for name in ("allocated_tokens", "consumed_tokens", "reserved_tokens", "consumed_attempts", "consumed_elapsed_ms")):
                raise StateError(f"cannot attest invalid budget state: {goal['id']}")
            for name in ("total_tokens", "total_attempts", "total_elapsed_ms", "max_concurrency"):
                if budget[name] is not None and int(budget[name]) < 0:
                    raise StateError(f"cannot attest invalid budget limit: {goal['id']}")
            if budget["total_tokens"] is not None and budget["consumed_tokens"] + budget["reserved_tokens"] > budget["total_tokens"]:
                # A host may measure actual CLI usage after a soft reservation
                # was crossed.  That debt is valid only when a finished,
                # exhausted attempt carries a compact coordinator attestation
                # to execution receipts; a generic counter edit can never
                # turn into a budget overrun.
                required_debt = (
                    int(budget["consumed_tokens"]) + int(budget["reserved_tokens"])
                    - int(budget["total_tokens"])
                )
                evidenced_debt = 0
                attempts = connection.execute(
                    """SELECT a.* FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
                       WHERE u.goal_id=? AND a.tokens_consumed>a.tokens_reserved""",
                    (goal["id"],),
                ).fetchall()
                for attempt in attempts:
                    if attempt["status"] != "finished" or attempt["outcome_class"] != "exhausted":
                        raise StateError(f"cannot attest unbounded token overrun: {attempt['id']}")
                    evidence = _decode(attempt["outcome_json"], {})
                    if not isinstance(evidence, dict):
                        raise StateError(f"cannot attest token overrun without evidence: {attempt['id']}")
                    try:
                        validate_observed_usage_evidence(
                            evidence.get("observed_usage"),
                            tokens_consumed=int(attempt["tokens_consumed"]),
                        )
                    except StateError as error:
                        raise StateError(f"cannot attest invalid token-overrun evidence: {attempt['id']}") from error
                    evidenced_debt += int(attempt["tokens_consumed"]) - int(attempt["tokens_reserved"])
                if evidenced_debt < required_debt:
                    raise StateError(f"cannot attest exceeded token budget: {goal['id']}")
            if budget["total_attempts"] is not None and budget["consumed_attempts"] > budget["total_attempts"]:
                raise StateError(f"cannot attest exceeded attempt budget: {goal['id']}")
            if budget["total_elapsed_ms"] is not None and budget["consumed_elapsed_ms"] > budget["total_elapsed_ms"]:
                raise StateError(f"cannot attest exceeded elapsed budget: {goal['id']}")
            live_attempts = connection.execute(
                "SELECT count(*),COALESCE(sum(a.tokens_reserved),0) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'",
                (goal["id"],),
            ).fetchone()
            if budget["reserved_tokens"] != live_attempts[1]:
                raise StateError(f"cannot attest inconsistent token reservations: {goal['id']}")
            if budget["max_concurrency"] is not None and live_attempts[0] > budget["max_concurrency"]:
                raise StateError(f"cannot attest exceeded concurrency budget: {goal['id']}")
            attempt_count = connection.execute(
                "SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=?",
                (goal["id"],),
            ).fetchone()[0]
            if budget["consumed_attempts"] != attempt_count:
                raise StateError(f"cannot attest inconsistent attempt accounting: {goal['id']}")
            if goal["status"] == "complete":
                if connection.execute("SELECT 1 FROM work_units WHERE goal_id=? AND status!='complete' LIMIT 1", (goal["id"],)).fetchone():
                    raise StateError(f"cannot attest completed goal with incomplete work: {goal['id']}")
                contract = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal["id"],)).fetchone()
                criteria = [] if contract is None else [item["id"] for item in _decode(contract["contract"], {})["acceptance_criteria"]]
                recorded = {row[0] for row in connection.execute("SELECT criterion_id FROM acceptance_evidence WHERE goal_id=?", (goal["id"],))}
                if any(criterion not in recorded for criterion in criteria):
                    raise StateError(f"cannot attest completed goal without acceptance evidence: {goal['id']}")
                if connection.execute("SELECT 1 FROM goal_checkpoints WHERE goal_id=? AND status!='reached' LIMIT 1", (goal["id"],)).fetchone():
                    raise StateError(f"cannot attest completed goal with pending checkpoints: {goal['id']}")
                if connection.execute("SELECT 1 FROM effect_intents WHERE goal_id=? AND ((protocol_version=1 AND status!='received') OR (protocol_version=2 AND status NOT IN ('succeeded','reconciled'))) LIMIT 1", (goal["id"],)).fetchone():
                    raise StateError(f"cannot attest completed goal with unresolved effects: {goal['id']}")
            if goal["status"] == "draining" and connection.execute(
                "SELECT 1 FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id "
                "WHERE u.goal_id=? AND a.status IN ('leased','active') LIMIT 1", (goal["id"],)
            ).fetchone() is None:
                raise StateError(f"cannot attest draining goal without a stored lease: {goal['id']}")
        for unit in connection.execute("SELECT * FROM work_units"):
            if unit["status"] not in WORK_UNIT_STATUSES:
                raise StateError(f"cannot attest invalid work-unit status: {unit['id']}")
            if unit["verification_policy"] not in VERIFICATION_POLICIES:
                raise StateError(f"cannot attest unsupported work-unit verification policy: {unit['id']}")
            try:
                _acceptance_checks(
                    _decode(unit["acceptance_checks"], None),
                    required=unit["verification_policy"] == "implementation-deterministic-review",
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise StateError(f"cannot attest invalid work-unit acceptance_checks: {unit['id']}") from error
            if unit["verification_policy"] != "implementation-review":
                try:
                    from .authority import validate_authority_envelope, verification_policy_allowed
                    contract_row = connection.execute(
                        "SELECT contract FROM goal_contracts WHERE goal_id=?", (unit["goal_id"],)
                    ).fetchone()
                    contract = None if contract_row is None else validate_authority_envelope(_decode(contract_row["contract"], {}))
                except ValueError as error:
                    raise StateError(f"cannot attest invalid authority for work unit policy: {unit['id']}") from error
                if contract is None or not verification_policy_allowed(contract, unit["verification_policy"]):
                    raise StateError(f"cannot attest revoked work-unit verification policy: {unit['id']}")
            attempts = connection.execute("SELECT * FROM work_attempts WHERE work_unit_id=? ORDER BY attempt_no", (unit["id"],)).fetchall()
            if unit["attempt_count"] != len(attempts) or [row["attempt_no"] for row in attempts] != list(range(1, len(attempts) + 1)):
                raise StateError(f"cannot attest inconsistent attempt sequence: {unit['id']}")
            for attempt in attempts:
                if attempt["status"] not in {"leased", "active", "finished", "expired", "paused", "stopped"}:
                    raise StateError(f"cannot attest invalid attempt status: {attempt['id']}")
                if any(int(attempt[name]) < 0 for name in ("attempt_no", "lease_generation", "tokens_reserved", "tokens_consumed", "elapsed_ms")):
                    raise StateError(f"cannot attest invalid attempt accounting: {attempt['id']}")
                if not isinstance(attempt["lease_token_hash"], str) or len(attempt["lease_token_hash"]) != 64:
                    raise StateError(f"cannot attest invalid attempt token hash: {attempt['id']}")
                if attempt["status"] in {"leased", "active"} and attempt["ended_at"] is not None:
                    raise StateError(f"cannot attest ended live attempt: {attempt['id']}")
                if attempt["status"] not in {"leased", "active"} and attempt["ended_at"] is None:
                    raise StateError(f"cannot attest unterminated finished attempt: {attempt['id']}")
            if unit["status"] == "leased":
                attempt = connection.execute("SELECT * FROM work_attempts WHERE id=?", (unit["current_attempt_id"],)).fetchone()
                if attempt is None or attempt["status"] != "leased" or attempt["work_unit_id"] != unit["id"] or attempt["owner_id"] != unit["lease_holder"]:
                    raise StateError(f"cannot attest inconsistent leased work unit: {unit['id']}")
            elif unit["current_attempt_id"] is not None or unit["lease_holder"] is not None or unit["lease_expires_at"] is not None:
                raise StateError(f"cannot attest stale lease fields on work unit: {unit['id']}")
            if unit["status"] == "complete" and connection.execute("SELECT 1 FROM workflow_evidence WHERE work_unit_id=?", (unit["id"],)).fetchone() is None:
                raise StateError(f"cannot attest completed work without workflow evidence: {unit['id']}")
        # Dependency rows are authority-bearing: validate every goal graph
        # before an explicit attestation can seal a migrated/unsealed state.
        for goal in connection.execute("SELECT id FROM goals"):
            StateStore._work_dependency_graph_in_transaction(
                connection, goal["id"], require_checkpoint_seals=False,
            )
        try:
            from .workflow import load_workflow, validate_workflow_completion_token
        except ImportError as error:  # pragma: no cover
            raise StateError("workflow validation is unavailable") from error
        for evidence in connection.execute("SELECT w.*,u.goal_id,u.verification_policy,u.acceptance_checks FROM workflow_evidence w JOIN work_units u ON u.id=w.work_unit_id"):
            if hashlib.sha256(evidence["workflow_json"].encode("utf-8")).hexdigest() != evidence["workflow_sha256"]:
                raise StateError(f"cannot attest workflow hash mismatch: {evidence['work_unit_id']}")
            try:
                workflow = load_workflow(evidence["workflow_json"])
                token = validate_workflow_completion_token(workflow, _decode(evidence["completion_token_json"], {}))
            except (ValueError, TypeError) as error:
                raise StateError(f"cannot attest invalid workflow evidence: {evidence['work_unit_id']}") from error
            if token["source"] != {"goal_id": evidence["goal_id"], "work_unit_id": evidence["work_unit_id"]}:
                raise StateError(f"cannot attest cross-bound workflow evidence: {evidence['work_unit_id']}")
            workflow_policy = "implementation-review" if workflow.get("version") == 1 else workflow.get("verification_policy")
            if workflow_policy != evidence["verification_policy"]:
                raise StateError(f"cannot attest workflow with a mismatched verification policy: {evidence['work_unit_id']}")
            if workflow_policy == "implementation-deterministic-review":
                try:
                    proof = validate_deterministic_review_completion_evidence(
                        workflow, _decode(evidence["acceptance_checks"], None),
                        _decode(evidence["completion_evidence_json"], None),
                    )
                    if _encode(proof) != evidence["completion_evidence_json"]:
                        raise ValueError("completion evidence is not canonical")
                    outcome = connection.execute(
                        "SELECT outcome_json FROM work_attempts WHERE work_unit_id=? AND outcome_class='success' ORDER BY attempt_no DESC LIMIT 1",
                        (evidence["work_unit_id"],),
                    ).fetchone()
                    if outcome is not None:
                        recorded_proof = validate_deterministic_review_completion_evidence(
                            workflow, _decode(evidence["acceptance_checks"], None),
                            _decode(outcome["outcome_json"], None),
                        )
                        if recorded_proof != proof:
                            raise ValueError("completion evidence does not match successful attempt outcome")
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                    raise StateError(f"cannot attest invalid deterministic-review completion evidence: {evidence['work_unit_id']}") from error
        for checkpoint in connection.execute("SELECT * FROM goal_checkpoints"):
            if checkpoint["status"] not in {"pending", "reached"}:
                raise StateError(f"cannot attest invalid checkpoint status: {checkpoint['goal_id']}/{checkpoint['checkpoint_id']}")
            if checkpoint["status"] == "reached":
                try:
                    evidence = _decode(checkpoint["evidence_json"], {})
                    required = {"checkpoint_id", "trigger_work_unit_id", "work_unit_count", "work_units_sha256", "workflow_sha256", "outcome_evidence_sha256"}
                    if set(evidence) != required or evidence["checkpoint_id"] != checkpoint["checkpoint_id"] or not isinstance(evidence["work_unit_count"], int) or evidence["work_unit_count"] < 1:
                        raise ValueError("invalid checkpoint evidence fields")
                    for name in ("work_units_sha256", "workflow_sha256", "outcome_evidence_sha256"):
                        if not isinstance(evidence[name], str) or len(evidence[name]) != 64 or any(char not in "0123456789abcdef" for char in evidence[name]):
                            raise ValueError("invalid checkpoint evidence hash")
                    trigger = connection.execute("SELECT checkpoint_id,status FROM work_units WHERE id=? AND goal_id=?", (evidence["trigger_work_unit_id"], checkpoint["goal_id"])).fetchone()
                    if trigger is None or trigger["checkpoint_id"] != checkpoint["checkpoint_id"] or trigger["status"] != "complete":
                        raise ValueError("invalid checkpoint trigger")
                    complete_units = connection.execute("SELECT id FROM work_units WHERE goal_id=? AND checkpoint_id=? AND status='complete' ORDER BY id", (checkpoint["goal_id"], checkpoint["checkpoint_id"])).fetchall()
                    if len(complete_units) != evidence["work_unit_count"]:
                        raise ValueError("checkpoint work count mismatch")
                    unit_digest = hashlib.sha256()
                    for complete_unit in complete_units:
                        unit_digest.update(complete_unit["id"].encode("utf-8"))
                        unit_digest.update(b"\0")
                    if unit_digest.hexdigest() != evidence["work_units_sha256"]:
                        raise ValueError("checkpoint work digest mismatch")
                    workflow_row = connection.execute("SELECT workflow_sha256 FROM workflow_evidence WHERE work_unit_id=?", (evidence["trigger_work_unit_id"],)).fetchone()
                    if workflow_row is None or workflow_row["workflow_sha256"] != evidence["workflow_sha256"]:
                        raise ValueError("checkpoint workflow digest mismatch")
                    outcome_row = connection.execute("SELECT outcome_json FROM work_attempts WHERE work_unit_id=? AND outcome_class='success' ORDER BY attempt_no DESC LIMIT 1", (evidence["trigger_work_unit_id"],)).fetchone()
                    if outcome_row is None or hashlib.sha256(outcome_row["outcome_json"].encode("utf-8")).hexdigest() != evidence["outcome_evidence_sha256"]:
                        raise ValueError("checkpoint outcome digest mismatch")
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                    raise StateError(f"cannot attest invalid checkpoint evidence: {checkpoint['goal_id']}/{checkpoint['checkpoint_id']}") from error
        for intent in connection.execute("SELECT * FROM effect_intents"):
            if intent["protocol_version"] == 2:
                if intent["status"] not in {"pending", "executing", "succeeded", "failed", "indeterminate", "reconciled"}:
                    raise StateError(f"cannot attest invalid provider-effect status: {intent['idempotency_key']}")
                required = ("provider", "capability", "envelope_sha256", "approval_id", "resource_scope", "work_attempt_id", "updated_at")
                if any(intent[name] is None or intent[name] == "" for name in required):
                    raise StateError(f"cannot attest incomplete provider-effect binding: {intent['idempotency_key']}")
                try:
                    scope = _decode(intent["resource_scope"], {})
                    from .authority import _validate_resource_scope, resource_scope_within, validate_authority_envelope
                    _validate_resource_scope(scope, label="provider intent resource_scope")
                    if (
                        scope["provider"] != intent["provider"]
                        or len(intent["envelope_sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in intent["envelope_sha256"])
                    ):
                        raise ValueError
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise StateError(f"cannot attest invalid provider-effect scope: {intent['idempotency_key']}") from error
                try:
                    provider = _persisted_identifier(intent["provider"], label="provider")
                    _persisted_identifier(intent["capability"], label="capability")
                    _persisted_identifier(intent["operation"], label="operation")
                    request_binding = _decode(intent["request_json"], {})
                    if set(request_binding) != {"authorized_performer_id", "request"}:
                        raise ValueError("invalid request binding")
                    performer = _persisted_identifier(
                        request_binding["authorized_performer_id"], label="performer_id"
                    )
                    request = request_binding["request"]
                    if not isinstance(request, dict):
                        raise ValueError("request is not an object")
                    canonical_request = json.dumps(
                        request, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False,
                    )
                    if hashlib.sha256(canonical_request.encode("utf-8")).hexdigest() != intent["request_sha256"]:
                        raise ValueError("request hash mismatch")
                    if intent["effect_class"] not in {
                        "repository-history", "remote-mutation", "external-communication",
                        "deployment", "merge", "destructive",
                    }:
                        raise ValueError("provider effect is not consequential")
                    unit = connection.execute(
                        "SELECT goal_id,scope FROM work_units WHERE id=?", (intent["work_unit_id"],)
                    ).fetchone()
                    work_attempt = connection.execute(
                        "SELECT work_unit_id,owner_id,lease_generation FROM work_attempts WHERE id=?",
                        (intent["work_attempt_id"],),
                    ).fetchone()
                    if (
                        unit is None or unit["goal_id"] != intent["goal_id"]
                        or work_attempt is None or work_attempt["work_unit_id"] != intent["work_unit_id"]
                        or work_attempt["owner_id"] != performer
                    ):
                        raise ValueError("work binding mismatch")
                    contract = connection.execute(
                        "SELECT version,envelope_sha256,contract FROM goal_contracts WHERE goal_id=?",
                        (intent["goal_id"],),
                    ).fetchone()
                    if contract is None or contract["version"] != "v2" or contract["envelope_sha256"] != intent["envelope_sha256"]:
                        raise ValueError("contract binding mismatch")
                    envelope = validate_authority_envelope(_decode(contract["contract"], {}))
                    if (
                        intent["operation"] not in envelope["allowed_actions"]
                        or intent["operation"] in envelope["prohibited_actions"]
                        or intent["effect_class"] not in envelope["allowed_effects"]
                        or not any(resource_scope_within(scope, allowed) for allowed in envelope["resource_scopes"])
                    ):
                        raise ValueError("intent is outside authority")
                    approval = connection.execute(
                        "SELECT * FROM transition_approvals WHERE id=?", (intent["approval_id"],)
                    ).fetchone()
                    if approval is None:
                        raise ValueError("missing approval")
                    approval_resource = _decode(approval["resource_scope"], {})
                    if (
                        int(approval["protocol_version"]) < 3
                        or approval["goal_id"] != intent["goal_id"]
                        or approval["work_unit_id"] not in {None, intent["work_unit_id"]}
                        or approval["action"] != intent["operation"]
                        or approval["effect"] != intent["effect_class"]
                        or approval["envelope_sha256"] != intent["envelope_sha256"]
                        or approval["performer_id"] != performer
                        or approval["decision"] != "approved"
                        or approval["revoked_at"] is not None
                        or not resource_scope_within(scope, approval_resource)
                        or not StateStore._scope_within_contract(
                            _decode(unit["scope"], {}), _decode(approval["scope"], {})
                        )
                    ):
                        raise ValueError("approval binding mismatch")
                    from .autonomy import AutonomyStore
                    AutonomyStore._verify_approval_evidence(
                        connection, intent["goal_id"], _row(approval) or {}
                    )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                    raise StateError(f"cannot attest invalid provider-effect binding: {intent['idempotency_key']}") from error
                attempts = connection.execute("SELECT * FROM effect_attempts WHERE intent_key=? ORDER BY attempt_no", (intent["idempotency_key"],)).fetchall()
                if (
                    [row["attempt_no"] for row in attempts] != list(range(1, len(attempts) + 1))
                    or any(
                        row["status"] != "executing"
                        or row["dispatched_by"] != performer
                        for row in attempts
                    )
                ):
                    raise StateError(f"cannot attest invalid provider-effect attempts: {intent['idempotency_key']}")
                for effect_attempt in attempts:
                    historical_work_attempt = connection.execute(
                        "SELECT work_unit_id,owner_id,lease_generation FROM work_attempts WHERE id=?",
                        (effect_attempt["work_attempt_id"],),
                    ).fetchone()
                    if (
                        historical_work_attempt is None
                        or historical_work_attempt["work_unit_id"] != intent["work_unit_id"]
                        or historical_work_attempt["owner_id"] != performer
                        or historical_work_attempt["lease_generation"] != effect_attempt["lease_generation"]
                    ):
                        raise StateError(f"cannot attest invalid provider-effect attempts: {intent['idempotency_key']}")
                reconciliation_events: list[tuple[sqlite3.Row, dict[str, Any]]] = []
                receipt_events: list[tuple[sqlite3.Row, dict[str, Any]]] = []
                for event in connection.execute("SELECT * FROM effect_receipt_events WHERE intent_key=?", (intent["idempotency_key"],)):
                    try:
                        observation = _decode(event["observation"], {})
                        reconciliation_is_valid = True
                        receipt_is_valid = True
                        if event["event_type"] == "reconciliation":
                            reconciliation_is_valid = (
                                isinstance(observation, dict)
                                and set(observation) == {"source", "resolution", "observation"}
                                and observation["source"] in {"manual", "adapter"}
                                and observation["resolution"] in {"applied", "absent", "conflict"}
                                and isinstance(observation["observation"], dict)
                                and not (
                                    observation["source"] == "manual"
                                    and observation["resolution"] == "absent"
                                )
                            )
                            reconciliation_events.append((event, observation))
                        elif event["event_type"] == "receipt":
                            receipt_is_valid = (
                                isinstance(observation, dict)
                                and set(observation) == {"outcome", "receipt"}
                                and observation["outcome"] in {"succeeded", "failed", "indeterminate"}
                                and isinstance(observation["receipt"], dict)
                                and event["effect_attempt_id"] is not None
                            )
                            receipt_events.append((event, observation))
                        if (
                            event["event_type"] not in {"receipt", "observation", "reconciliation"}
                            or len(event["observation"].encode("utf-8")) > 64 * 1024
                            or not isinstance(observation, dict)
                            or not _provider_observation_is_sanitized(observation)
                            or not reconciliation_is_valid
                            or not receipt_is_valid
                            or event["observed_by"] != performer
                            or (
                                event["effect_attempt_id"] is not None
                                and not any(row["id"] == event["effect_attempt_id"] for row in attempts)
                            )
                        ):
                            raise ValueError
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise StateError(f"cannot attest invalid provider-effect receipt event: {intent['idempotency_key']}") from error
                reconciliation_id = intent["last_reconciliation_event_id"]
                if intent["status"] == "reconciled" and not reconciliation_id:
                    raise StateError(f"cannot attest provider effect without committed reconciliation: {intent['idempotency_key']}")
                if reconciliation_id is None and reconciliation_events:
                    raise StateError(f"cannot attest uncommitted provider reconciliation: {intent['idempotency_key']}")
                attempt_numbers = {row["id"]: int(row["attempt_no"]) for row in attempts}
                ordered_reconciliations: list[tuple[int, sqlite3.Row, dict[str, Any]]] = []
                explicit_attempt_ids = {
                    event["effect_attempt_id"] for event, _ in reconciliation_events
                    if event["effect_attempt_id"] is not None
                }
                assigned_attempt_ids: set[str] = set()
                for event, observation in sorted(
                    reconciliation_events, key=lambda item: (item[0]["recorded_at"], item[0]["id"])
                ):
                    if event["effect_attempt_id"] is None:
                        # Schema-7 reconciliation did not retain its attempt
                        # identity.  Infer it only when event time and already
                        # assigned cycles leave exactly one possible dispatch.
                        candidates = [
                            row for row in attempts
                            if row["id"] not in assigned_attempt_ids
                            and row["id"] not in explicit_attempt_ids
                            and row["dispatched_at"] <= event["recorded_at"]
                        ]
                        if len(candidates) != 1:
                            raise StateError(f"cannot attest ambiguous provider reconciliation: {intent['idempotency_key']}")
                        attempt_id = candidates[0]["id"]
                    else:
                        attempt_id = event["effect_attempt_id"]
                    if attempt_id in assigned_attempt_ids:
                        raise StateError(f"cannot attest duplicate provider reconciliation: {intent['idempotency_key']}")
                    assigned_attempt_ids.add(attempt_id)
                    attempt_no = attempt_numbers.get(attempt_id, 0)
                    if attempt_no <= 0:
                        raise StateError(f"cannot attest invalid provider reconciliation attempt: {intent['idempotency_key']}")
                    ordered_reconciliations.append((attempt_no, event, observation))
                ordered_reconciliations.sort(key=lambda item: (item[0], item[1]["recorded_at"], item[1]["id"]))
                reconciliation_attempts = [item[0] for item in ordered_reconciliations]
                if (
                    reconciliation_attempts != sorted(set(reconciliation_attempts))
                    or (
                        ordered_reconciliations
                        and reconciliation_id != ordered_reconciliations[-1][1]["id"]
                    )
                    or any(
                        observation["source"] != "adapter" or observation["resolution"] != "absent"
                        for _, _, observation in ordered_reconciliations[:-1]
                    )
                ):
                    raise StateError(f"cannot attest invalid provider reconciliation sequence: {intent['idempotency_key']}")
                committed_resolution: str | None = None
                committed_attempt_no: int | None = None
                if reconciliation_id is not None:
                    committed = connection.execute(
                        "SELECT * FROM effect_receipt_events WHERE id=? AND intent_key=? AND event_type='reconciliation'",
                        (reconciliation_id, intent["idempotency_key"]),
                    ).fetchone()
                    try:
                        committed_observation = _decode(committed["observation"], {}) if committed is not None else None
                        if (
                            not isinstance(committed_observation, dict)
                            or set(committed_observation) != {"source", "resolution", "observation"}
                            or committed_observation["source"] not in {"manual", "adapter"}
                            or committed_observation["resolution"] not in {"applied", "absent", "conflict"}
                            or not isinstance(committed_observation["observation"], dict)
                            or (committed_observation["source"] == "manual" and committed_observation["resolution"] == "absent")
                            or (
                                committed_observation["resolution"] == "applied"
                                and intent["status"] != "reconciled"
                            )
                            or (
                                committed_observation["resolution"] == "conflict"
                                and intent["status"] != "failed"
                            )
                        ):
                            raise ValueError
                        committed_resolution = committed_observation["resolution"]
                        committed_attempt_no = next(
                            attempt_no for attempt_no, event, _ in ordered_reconciliations
                            if event["id"] == reconciliation_id
                        )
                        latest_attempt_no = int(attempts[-1]["attempt_no"]) if attempts else 0
                        if (
                            committed_observation["resolution"] in {"applied", "conflict"}
                            and committed_attempt_no != latest_attempt_no
                        ):
                            raise ValueError
                        if (
                            committed_observation["resolution"] == "absent"
                            and committed_attempt_no == latest_attempt_no
                            and intent["status"] not in {"reconciled", "pending"}
                        ):
                            raise ValueError
                    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                        raise StateError(f"cannot attest invalid committed provider reconciliation: {intent['idempotency_key']}") from error
                if (
                    intent["status"] == "succeeded"
                    or (intent["status"] == "failed" and committed_resolution != "conflict")
                ):
                    if not receipt_events:
                        raise StateError(f"cannot attest provider effect without a terminal receipt: {intent['idempotency_key']}")
                    latest_attempt_id = attempts[-1]["id"] if attempts else None
                    matching = [
                        observation for event, observation in receipt_events
                        if event["effect_attempt_id"] == latest_attempt_id
                    ]
                    if len(matching) != 1 or matching[0]["outcome"] != intent["status"]:
                        raise StateError(f"cannot attest inconsistent provider receipt state: {intent['idempotency_key']}")
                continue
            if intent["protocol_version"] != 1 or intent["status"] not in {
                "pending", "reconciliation-required", "received", "recovery-required",
            }:
                raise StateError(f"cannot attest invalid effect status: {intent['idempotency_key']}")
            try:
                request = _decode(intent["request_json"], {})
                performer = request["authorized_performer_id"]
                payload = request["request"]
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                raise StateError(f"cannot attest invalid effect request: {intent['idempotency_key']}") from error
            if not isinstance(performer, str) or not isinstance(payload, dict) or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != intent["request_sha256"]:
                raise StateError(f"cannot attest mismatched effect request: {intent['idempotency_key']}")
            receipt = connection.execute("SELECT * FROM effect_receipts WHERE intent_key=?", (intent["idempotency_key"],)).fetchone()
            receipt_required = intent["status"] in {"received", "recovery-required"}
            if receipt_required != (receipt is not None):
                raise StateError(f"cannot attest inconsistent effect receipt state: {intent['idempotency_key']}")
            if receipt is not None:
                if receipt["performed_by"] != performer or not receipt["outcome"]:
                    raise StateError(f"cannot attest invalid effect receipt: {intent['idempotency_key']}")
                outcome = receipt["outcome"]
                if intent["status"] == "received":
                    if outcome in {"applied", "success", "failed-before-effect"}:
                        pass
                    elif (
                        outcome == "failed" and intent["operation"] == "local-effect"
                        and intent["effect_class"] == "local-reversible-write"
                        and set(payload) == {"action", "plan_sha256"}
                        and payload["action"] == "upgrade-apply"
                        and isinstance(payload["plan_sha256"], str)
                        and re.fullmatch(r"[0-9a-f]{64}", payload["plan_sha256"])
                        and receipt["before_sha256"] is None and receipt["after_sha256"] is None
                    ):
                        # Preserve the exact pre-15 failed-upgrade bridge
                        # record. Current clients cannot create this outcome.
                        pass
                    elif outcome in {"indeterminate", "recovery-required"}:
                        recovery_events = connection.execute(
                            """SELECT * FROM events WHERE event_type='effect.recovery_resolved'
                               AND goal_id=? AND (work_unit_id IS ? OR work_unit_id=?)""",
                            (intent["goal_id"], intent["work_unit_id"], intent["work_unit_id"]),
                        ).fetchall()
                        matching_events: list[tuple[sqlite3.Row, dict[str, Any]]] = []
                        for event in recovery_events:
                            try:
                                payload = _decode(event["payload"], {})
                                if (
                                    set(payload) != {
                                        "idempotency_key", "resolution", "performer_id",
                                        "evidence_sha256", "evidence",
                                    }
                                    or payload["idempotency_key"] != intent["idempotency_key"]
                                    or payload["resolution"] not in {"applied", "failed-before-effect"}
                                    or not isinstance(payload["performer_id"], str)
                                    or not isinstance(payload["evidence"], dict)
                                    or not isinstance(payload["evidence_sha256"], str)
                                    or not re.fullmatch(r"[0-9a-f]{64}", payload["evidence_sha256"])
                                ):
                                    continue
                                canonical_evidence = json.dumps(
                                    payload["evidence"], sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False,
                                )
                                if hashlib.sha256(canonical_evidence.encode("utf-8")).hexdigest() != payload["evidence_sha256"]:
                                    continue
                                audit = connection.execute(
                                    """SELECT sequence FROM audit_events WHERE legacy_event_id=? AND event_type=?
                                       AND goal_id=? AND (work_unit_id IS ? OR work_unit_id=?) AND payload=?""",
                                    (event["id"], event["event_type"], event["goal_id"], event["work_unit_id"], event["work_unit_id"], event["payload"]),
                                ).fetchone()
                                contract_event = None if audit is None else connection.execute(
                                    """SELECT e.payload FROM events e JOIN audit_events a ON a.legacy_event_id=e.id
                                       WHERE e.event_type='goal.contract_defined' AND e.goal_id=?
                                         AND a.sequence<? ORDER BY a.sequence DESC LIMIT 1""",
                                    (intent["goal_id"], audit["sequence"]),
                                ).fetchone()
                                try:
                                    historical_contract = (
                                        None if contract_event is None
                                        else _decode(contract_event["payload"], {}).get("envelope_sha256")
                                    )
                                    if not isinstance(historical_contract, str):
                                        raise ValueError
                                    unit_scope = None
                                    if intent["work_unit_id"] is not None:
                                        unit = connection.execute(
                                            "SELECT scope FROM work_units WHERE id=?", (intent["work_unit_id"],)
                                        ).fetchone()
                                        unit_scope = None if unit is None else _decode(unit["scope"], {})
                                        if not isinstance(unit_scope, dict):
                                            raise ValueError
                                    event_at = _timestamp(event["created_at"])
                                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                                    continue
                                approvals = connection.execute(
                                    """SELECT * FROM transition_approvals
                                       WHERE goal_id=? AND action='effect-recovery-resolve'
                                         AND performer_id=? AND decision='approved' AND approver_kind='human'
                                         AND approver_id!=? AND effect='local-reversible-write'
                                         AND envelope_sha256=? AND valid_until IS NOT NULL
                                         AND (work_unit_id IS NULL OR work_unit_id=?)""",
                                    (
                                        intent["goal_id"], payload["performer_id"], payload["performer_id"],
                                        historical_contract, intent["work_unit_id"],
                                    ),
                                ).fetchall()
                                approval_is_durable = False
                                if audit is not None:
                                    for approval in approvals:
                                        try:
                                            approval_scope = _decode(approval["scope"], {})
                                            created_at = _timestamp(approval["created_at"])
                                            valid_until = _timestamp(approval["valid_until"])
                                            revoked_at = (
                                                None if approval["revoked_at"] is None else _timestamp(approval["revoked_at"])
                                            )
                                            # The approval scope is the durable historical
                                            # goal scope for a goal-bound recovery. `_authorize`
                                            # required exact equality when it recorded the event;
                                            # comparing to a later replacement contract would
                                            # rewrite that historical binding. Unit-bound effects
                                            # still prove coverage against their immutable scope.
                                            scope_matches = (
                                                StateStore._scope_within_contract(unit_scope, approval_scope)
                                                if unit_scope is not None
                                                else StateStore._scope_within_contract(approval_scope, approval_scope)
                                            )
                                            if not (
                                                isinstance(approval_scope, dict)
                                                and scope_matches
                                                and created_at <= event_at < valid_until
                                                and (revoked_at is None or event_at < revoked_at)
                                            ):
                                                continue
                                        except (TypeError, ValueError, json.JSONDecodeError):
                                            continue
                                        approval_events = connection.execute(
                                            """SELECT e.payload FROM events e JOIN audit_events a ON a.legacy_event_id=e.id
                                               WHERE e.event_type='transition_approval.recorded' AND e.goal_id=?
                                                 AND a.sequence<?""",
                                            (intent["goal_id"], audit["sequence"]),
                                        ).fetchall()
                                        for approval_event in approval_events:
                                            recorded = _decode(approval_event["payload"], {})
                                            if recorded == {
                                                "action": "effect-recovery-resolve", "approval_id": approval["id"],
                                            }:
                                                approval_is_durable = True
                                                break
                                        if approval_is_durable:
                                            break
                                if audit is not None and approval_is_durable:
                                    matching_events.append((event, payload))
                            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                                continue
                        if len(matching_events) != 1:
                            raise StateError(
                                f"cannot attest unresolved indeterminate effect receipt: {intent['idempotency_key']}"
                            )
                    else:
                        raise StateError(f"cannot attest incompatible effect receipt outcome: {intent['idempotency_key']}")
                elif outcome not in {"indeterminate", "recovery-required"}:
                    raise StateError(f"cannot attest incompatible effect receipt outcome: {intent['idempotency_key']}")
                for name in ("before_sha256", "after_sha256"):
                    digest = receipt[name]
                    if digest is not None and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                        raise StateError(f"cannot attest invalid effect receipt digest: {intent['idempotency_key']}")
                try:
                    if not isinstance(_decode(receipt["evidence_json"], {}), dict):
                        raise ValueError
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise StateError(f"cannot attest invalid effect receipt evidence: {intent['idempotency_key']}") from error
        control = connection.execute("SELECT * FROM runtime_control WHERE id=1").fetchone()
        if control is None or (control["emergency_stopped"] and (not control["reason"] or not control["set_by"] or not control["set_at"])):
            raise StateError("cannot attest invalid runtime control state")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 13:
            StateStore._assert_intervention_integrity_in_transaction(connection)

    def repair_unsealed_transition_approval(
        self,
        approval_id: str,
        approval_record: dict[str, Any],
        *,
        actor_id: str,
        actor_kind: str,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Human-only repair of a migrated approval scope before attestation.

        The complete supplied record is validated and must match every
        persisted field except ``scope``.  This prevents a convenient scope
        repair command from becoming a covert authority rewrite.
        """
        approval_id = _identifier(approval_id, label="approval_id")
        actor_id = _identifier(actor_id, label="actor_id")
        if actor_kind != "human":
            raise StateError("repairing migrated transition authority requires an explicit human actor")
        if not isinstance(approval_record, dict):
            raise StateError("replacement transition approval must be an object")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires explicit migration to {SCHEMA_VERSION}")
            self._assert_audit_chain_in_transaction(connection)
            row = connection.execute("SELECT * FROM transition_approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise StateError(f"Unknown transition approval: {approval_id}")
            if self._latest_seal_hash(connection, "transition_approvals", approval_id) is not None:
                raise StateError("only an unsealed migrated transition approval may be repaired")
            try:
                from .authority import authority_envelope_sha256, validate_authority_envelope, validate_transition_approval
                replacement = validate_transition_approval(approval_record)
            except (ImportError, ValueError) as error:
                raise StateError(f"replacement transition approval is invalid: {error}") from error
            if replacement["approval_id"] != approval_id or replacement["goal_id"] != row["goal_id"]:
                raise StateError("replacement approval identity does not match the migrated row")
            contract_row = connection.execute("SELECT contract,envelope_sha256 FROM goal_contracts WHERE goal_id=?", (row["goal_id"],)).fetchone()
            if contract_row is None:
                raise StateError("replacement approval goal lacks a current authority envelope")
            try:
                envelope = validate_authority_envelope(_decode(contract_row["contract"], {}))
            except ValueError as error:
                raise StateError(f"replacement approval goal has an invalid authority envelope: {error}") from error
            current_hash = authority_envelope_sha256(envelope)
            if current_hash != contract_row["envelope_sha256"] or replacement["envelope_sha256"] != row["envelope_sha256"]:
                raise StateError("replacement approval identity or envelope hash does not match the migrated row")
            if replacement["envelope_sha256"] == current_hash and not self._scope_within_contract(replacement["scope"], envelope["scope"]):
                raise StateError("replacement approval scope is outside its authority envelope")
            expected = {
                "goal_id": row["goal_id"], "work_unit_id": row["work_unit_id"], "action": row["action"],
                "effect": row["effect"], "envelope_sha256": row["envelope_sha256"], "decision": row["decision"],
                "approver_kind": row["approver_kind"], "approver_id": row["approver_id"],
                "performer_id": row["performer_id"], "authority_clause": row["authority_clause"],
                "evidence": _decode(row["evidence"], []), "valid_until": row["valid_until"],
                "revoked_at": row["revoked_at"],
            }
            actual = {
                "goal_id": replacement["goal_id"], "work_unit_id": replacement["work_unit_id"], "action": replacement["action"],
                "effect": replacement["effect"], "envelope_sha256": replacement["envelope_sha256"], "decision": replacement["decision"],
                "approver_kind": replacement["approver"]["kind"], "approver_id": replacement["approver"]["id"],
                "performer_id": replacement["performer_id"], "authority_clause": replacement["authority_clause"],
                "evidence": replacement["evidence"], "valid_until": replacement["valid_until"],
                "revoked_at": replacement["revoked_at"],
            }
            if actual != expected:
                raise StateError("replacement approval may change only the explicit scope")
            old_scope = _decode(row["scope"], {})
            if replacement["scope"] == old_scope:
                raise StateError("replacement approval scope does not change the migrated row")
            connection.execute("UPDATE transition_approvals SET scope=? WHERE id=?", (_encode(replacement["scope"]), approval_id))
            # Repair remains deliberately unsealed. Multiple invalid migrated
            # rows can be repaired one by one; the later human attestation
            # validates the complete authority set and seals it atomically.
            self._append_event_in_transaction(
                connection, "transition_approval.scope_repaired", goal_id=row["goal_id"],
                payload={"approval_id": approval_id, "actor_id": actor_id, "old_scope": old_scope, "new_scope": replacement["scope"]},
            )
        return self.get_transition_approval(approval_id) or {}

    def record_transition_approval(self, *, goal_id: str, action: str, envelope_sha256: str,
                                   approver_id: str, performer_id: str, work_unit_id: str | None = None, scope: dict[str, Any] | None = None,
                                   effect: str | None = None, valid_until: str | datetime | None = None,
                                   decision: str = "approved", authority_clause: str = "human approval",
                                   evidence: list[str] | None = None, approver_kind: str = "human", approval_id: str | None = None,
                                   resource_scope: dict[str, Any] | None = None,
                                   provenance: dict[str, Any] | None = None,
                                   at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        approver_id = _identifier(approver_id, label="approver_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(action, str) or not action or len(envelope_sha256) != 64:
            raise StateError("action and exact envelope_sha256 are required")
        if approver_id == performer_id:
            raise StateError("An approver cannot approve work they performed")
        if decision not in APPROVAL_DECISIONS or not authority_clause.strip() or approver_kind not in {"human", "steward"}:
            raise StateError("transition approval decision and authority clause are invalid")
        timestamp = _timestamp(at)
        expiry = _timestamp(valid_until) if valid_until is not None else None
        identifier = _identifier(approval_id or f"transition-{uuid4().hex[:12]}", label="approval_id")
        compatibility_write = False
        stored_result: dict[str, Any] | None = None
        with self._connection() as connection:
            runtime_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            compatibility_write = runtime_schema in {8, 9, 10, 11}
            if compatibility_write:
                # A self-hosted upgrade needs one authorization record before
                # the current schema can exist. Keep this bridge deliberately
                # narrower than normal approval recording: a steward may only
                # authorize the exact local, reversible upgrade effect. No
                # consequential action or human-ceremony claim can cross it.
                if (
                    action != "local-effect"
                    or (effect or "read-only") != "local-reversible-write"
                    or approver_kind != "steward"
                    or provenance is not None
                    or decision != "approved"
                ):
                    raise StateError(
                        "pre-migration approval bridge only permits a steward-approved local reversible effect"
                    )
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(
                    connection, existing_only=True,
                )
            else:
                self._prepare_write(connection)
            if connection.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
                raise StateError(f"Unknown goal: {goal_id}")
            if work_unit_id is not None:
                unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
                if unit is None or unit["goal_id"] != goal_id:
                    raise StateError("transition approval work unit belongs to a different or unknown goal")
            if decision == "approved":
                contract_row = connection.execute("SELECT envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
                if contract_row is None or contract_row["envelope_sha256"] != envelope_sha256:
                    raise StateError("approved transition requires the exact current authority envelope")
                try:
                    from .authority import validate_authority_envelope
                    envelope = validate_authority_envelope(_decode(contract_row["contract"], {}))
                except ValueError as error:
                    raise StateError(f"stored authority envelope is invalid: {error}") from error
                if action not in envelope["allowed_actions"] or action in envelope["prohibited_actions"]:
                    raise StateError("transition action is not allowed by the authority envelope")
                if (effect or "read-only") not in envelope["allowed_effects"]:
                    raise StateError("transition effect is not allowed by the authority envelope")
                canonical_scope = envelope["scope"] if scope is None else scope
                if not self._scope_within_contract(canonical_scope, envelope["scope"]):
                    raise StateError("transition scope is outside the authority envelope")
            else:
                envelope_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
                if envelope_row is None:
                    raise StateError("transition decision requires an explicit authority envelope")
                try:
                    from .authority import validate_authority_envelope
                    envelope = validate_authority_envelope(_decode(envelope_row["contract"], {}))
                except ValueError as error:
                    raise StateError(f"stored authority envelope is invalid: {error}") from error
            canonical_scope = envelope["scope"] if scope is None else scope
            approval_version = (
                4 if isinstance(provenance, dict) and provenance.get("kind") == "codex-user-message"
                else 3 if provenance is not None
                else int(envelope["version"])
            )
            consequential = (effect or "read-only") in {
                "repository-history", "remote-mutation", "external-communication",
                "deployment", "merge", "destructive",
            }
            if consequential and approval_version < 3:
                raise StateError(
                    "consequential approval requires v3+ human-bound provenance; re-record it through approval record"
                )
            if (
                decision == "approved"
                and approval_version in {2, 3, 4}
                and consequential
            ):
                try:
                    from .authority import resource_scope_within
                    resource_is_bounded = resource_scope is not None and any(
                        resource_scope_within(resource_scope, allowed)
                        for allowed in envelope["resource_scopes"]
                    )
                except (TypeError, ValueError) as error:
                    raise StateError(f"transition resource scope is invalid: {error}") from error
                if not resource_is_bounded:
                    raise StateError("transition resource scope is outside the authority envelope")
            approval_record = {"kind": "tasktra.transition-approval", "version": approval_version, "approval_id": identifier,
                "goal_id": goal_id, "work_unit_id": work_unit_id, "action": action, "effect": effect or "read-only",
                "scope": canonical_scope, "envelope_sha256": envelope_sha256, "decision": decision, "approver": {"kind": approver_kind, "id": approver_id},
                "performer_id": performer_id, "authority_clause": authority_clause, "evidence": evidence or [],
                "valid_until": expiry, "revoked_at": None}
            if approval_version == 2:
                approval_record["resource_scope"] = resource_scope
            elif approval_version >= 3:
                approval_record["resource_scope"] = resource_scope
                approval_record["provenance"] = provenance
            try:
                from .authority import validate_transition_approval
                validate_transition_approval(approval_record)
            except ValueError as error:
                raise StateError(f"transition approval is invalid: {error}") from error
            values = (
                identifier, goal_id, work_unit_id, action, effect or "read-only",
                envelope_sha256, _encode(canonical_scope), decision, approver_kind,
                approver_id, performer_id, authority_clause, _encode(evidence or []),
                expiry, timestamp,
                _encode(resource_scope) if resource_scope is not None else None,
                approval_version,
            )
            if runtime_schema == 8:
                connection.execute(
                    "INSERT INTO transition_approvals(id,goal_id,work_unit_id,action,effect,envelope_sha256,scope,decision,approver_kind,approver_id,performer_id,authority_clause,evidence,valid_until,created_at,resource_scope,protocol_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO transition_approvals(id,goal_id,work_unit_id,action,effect,envelope_sha256,scope,decision,approver_kind,approver_id,performer_id,authority_clause,evidence,valid_until,created_at,resource_scope,protocol_version,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, _encode(provenance) if provenance is not None else None),
                )
            approval_row = connection.execute("SELECT * FROM transition_approvals WHERE id=?", (identifier,)).fetchone()
            self._seal_authority_row_in_transaction(connection, "transition_approvals", identifier, approval_row, timestamp)
            self._append_event_in_transaction(connection, "transition_approval.recorded", goal_id=goal_id,
                                              payload={"approval_id": identifier, "action": action})
            if compatibility_write:
                self._seal_current_state_in_transaction(
                    connection, timestamp, existing_only=True,
                )
                stored_result = _row(approval_row)
        if compatibility_write:
            return stored_result or {}
        return self.get_transition_approval(identifier) or {}

    def get_transition_approval(self, approval_id: str) -> dict[str, Any] | None:
        approval_id = _identifier(approval_id, label="approval_id")
        self._ensure()
        with self._connection(write=False) as connection:
            return _row(connection.execute("SELECT * FROM transition_approvals WHERE id=?", (approval_id,)).fetchone())

    @staticmethod
    def _scope_within_contract(scope: dict[str, Any], contract_scope: dict[str, Any]) -> bool:
        """Return whether a requested scope is a semantic subset of a container."""
        paths = scope.get("paths", [])
        requested_exclusions = scope.get("exclusions", [])
        if (
            set(scope) != {"paths", "exclusions"}
            or not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) for path in paths)
            or not isinstance(requested_exclusions, list)
            or not all(isinstance(path, str) for path in requested_exclusions)
        ):
            return False
        allowed, excluded = contract_scope["paths"], contract_scope["exclusions"]
        def contains(parent: str, child: str) -> bool:
            return parent == "." or child == parent or child.startswith(parent + "/")
        for path in paths:
            if not any(contains(root, path) for root in allowed):
                return False
            if any(contains(root, path) for root in excluded):
                return False
            for denied in excluded:
                if contains(path, denied) and not any(
                    contains(requested_denied, denied)
                    for requested_denied in requested_exclusions
                ):
                    return False
        return True

    def revoke_transition_approval(self, approval_id: str, *, actor_id: str,
                                   at: str | datetime | None = None) -> dict[str, Any]:
        approval_id = _identifier(approval_id, label="approval_id")
        actor_id = _identifier(actor_id, label="actor_id")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("SELECT goal_id,revoked_at FROM transition_approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None: raise StateError(f"Unknown transition approval: {approval_id}")
            if row["revoked_at"] is not None: raise StateError("Transition approval is already revoked")
            connection.execute("UPDATE transition_approvals SET revoked_at=?,revoked_by=? WHERE id=?", (timestamp, actor_id, approval_id))
            approval_row = connection.execute("SELECT * FROM transition_approvals WHERE id=?", (approval_id,)).fetchone()
            self._seal_authority_row_in_transaction(connection, "transition_approvals", approval_id, approval_row, timestamp)
            self._append_event_in_transaction(connection, "transition_approval.revoked", goal_id=row["goal_id"], payload={"approval_id": approval_id})
        return self.get_transition_approval(approval_id) or {}

    def _check_authorization_in_transaction(self, connection: sqlite3.Connection, *, goal_id: str, action: str,
                                             envelope_sha256: str, performer_id: str,
                                             scope: dict[str, Any] | None, effect: str | None,
                                             timestamp: str) -> dict[str, Any]:
        if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
            raise StateError("runtime is emergency-stopped")
        contract = connection.execute("SELECT version,envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        if contract is None or contract["version"] not in {"v1", "v2"} or contract["envelope_sha256"] != envelope_sha256:
            raise StateError("authorization requires the exact current envelope hash")
        try:
            from .authority import validate_authority_envelope
            envelope = validate_authority_envelope(_decode(contract["contract"], {}))
        except ValueError as error:
            raise StateError(f"stored authority envelope is invalid: {error}") from error
        if action not in envelope["allowed_actions"] or action in envelope["prohibited_actions"]:
            raise StateError("action is not allowed by the authority envelope")
        if effect is not None and effect not in envelope["allowed_effects"]:
            raise StateError("effect is not allowed by the authority envelope")
        requested_scope = envelope["scope"] if scope is None else scope
        if not self._scope_within_contract(requested_scope, envelope["scope"]):
            raise StateError("requested scope is outside the authority envelope")
        rows = connection.execute("SELECT * FROM transition_approvals WHERE goal_id=? AND action=? AND envelope_sha256=? AND performer_id=? AND decision='approved' AND revoked_at IS NULL", (goal_id, action, envelope_sha256, performer_id))
        for row in rows:
            value = _row(row) or {}
            if value["approver_id"] == performer_id: continue
            if action in {"goal-activate", "goal-resume"} and value["approver_kind"] != "human": continue
            # Approved transitions are deliberately time-bounded. The public
            # validator rejects NULL, and this guard prevents malformed or
            # legacy rows from becoming perpetual authority.
            if value["valid_until"] is None or value["valid_until"] <= timestamp: continue
            if effect is not None and value["effect"] != effect: continue
            if not self._scope_within_contract(value["scope"], envelope["scope"]): continue
            if value["scope"] == requested_scope: return value
        raise StateError("no current approval bound to the exact action, envelope, performer, effect, and scope")

    def check_authorization(self, *, goal_id: str, action: str, envelope_sha256: str,
                            performer_id: str, scope: dict[str, Any] | None = None,
                            effect: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        """Central exact envelope/action/effect/scope/expiry/revocation/SoD gate."""
        goal_id = _identifier(goal_id, label="goal_id")
        performer_id = _identifier(performer_id, label="performer_id")
        timestamp = _timestamp(at)
        with self._connection(write=False) as connection:
            return self._check_authorization_in_transaction(connection, goal_id=goal_id, action=action,
                envelope_sha256=envelope_sha256, performer_id=performer_id, scope=scope,
                effect=effect, timestamp=timestamp)

    @staticmethod
    def _goal_drain_observation_in_transaction(connection: sqlite3.Connection, goal_id: str,
                                                *, limit: int, offset: int, timestamp: str,
                                                status_before: str, status_after: str,
                                                read_only: bool) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT a.id,a.work_unit_id,a.expires_at,a.status FROM work_attempts a "
            "JOIN work_units u ON u.id=a.work_unit_id "
            "WHERE u.goal_id=? AND a.status IN ('leased','active') ORDER BY a.id", (goal_id,)
        ).fetchall()
        attempt_ids = [str(row["id"]) for row in rows]
        lease_hash = hashlib.sha256(_encode(attempt_ids).encode("utf-8")).hexdigest()
        entries = [{"attempt_id": row["id"], "work_unit_id": row["work_unit_id"],
                    "expires_at": row["expires_at"],
                    "state": "expired" if row["expires_at"] <= timestamp else "live"}
                   for row in rows]
        total = len(entries)
        return {
            "goal_id": goal_id, "status_before": status_before, "status_after": status_after,
            "stored_leases": total, "live_leases": sum(item["state"] == "live" for item in entries),
            "expired_leases": sum(item["state"] == "expired" for item in entries),
            "attempts": entries[offset:offset + limit], "total": total, "limit": limit,
            "offset": offset, "next_offset": offset + len(entries[offset:offset + limit]) if offset + len(entries[offset:offset + limit]) < total else None,
            "lease_set_sha256": lease_hash, "read_only": read_only,
        }

    @staticmethod
    def _finalize_drain_if_empty_in_transaction(connection: sqlite3.Connection, goal_id: str,
                                                 *, timestamp: str, actor_id: str) -> bool:
        goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None or goal["status"] != "draining":
            return False
        if connection.execute(
            "SELECT 1 FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id "
            "WHERE u.goal_id=? AND a.status IN ('leased','active') LIMIT 1", (goal_id,)
        ).fetchone() is not None:
            return False
        connection.execute("UPDATE goals SET status='paused',updated_at=? WHERE id=?", (timestamp, goal_id))
        StateStore._append_event_in_transaction(
            connection, "goal.paused", goal_id=goal_id,
            payload={"actor_id": actor_id, "reason": "drain_completed"},
        )
        return True

    @staticmethod
    def _drain_pagination(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise StateError("limit must be an integer from 1 to 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise StateError("offset must be an integer from 0 to 1000000")

    def preview_goal_drain(self, goal_id: str, *, limit: int = 20, offset: int = 0,
                           at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        self._drain_pagination(limit, offset)
        timestamp = _timestamp(at)
        self._ensure()
        with self._connection(write=False) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            self._assert_audit_chain_in_transaction(connection)
            self._assert_current_state_integrity_in_transaction(connection)
            row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise StateError(f"Unknown goal: {goal_id}")
            report = self._goal_drain_observation_in_transaction(
                connection, goal_id, limit=limit, offset=offset, timestamp=timestamp,
                status_before=row["status"], status_after=row["status"],
                read_only=True,
            )
            if row["status"] == "active":
                report["status_after"] = "draining" if report["stored_leases"] else "paused"
            elif row["status"] == "draining" and not report["stored_leases"]:
                report["status_after"] = "paused"
            return report

    def drain_goal(self, goal_id: str, *, actor_id: str, limit: int = 20, offset: int = 0,
                   at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        actor_id = _identifier(actor_id, label="actor_id")
        self._drain_pagination(limit, offset)
        timestamp = _timestamp(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise StateError(f"Unknown goal: {goal_id}")
            before = row["status"]
            if before not in {"active", "draining", "paused"}:
                raise StateError(f"Cannot drain goal from {before}")
            if before == "paused":
                report = self._goal_drain_observation_in_transaction(
                    connection, goal_id, limit=limit, offset=offset, timestamp=timestamp,
                    status_before=before, status_after="paused", read_only=False,
                )
                if report["stored_leases"]:
                    raise StateError("paused goal holds a stored lease")
                return report
            if before == "active":
                connection.execute("UPDATE goals SET status='draining',updated_at=? WHERE id=?", (timestamp, goal_id))
                self._append_event_in_transaction(connection, "goal.draining", goal_id=goal_id, payload={"actor_id": actor_id})
            self._finalize_drain_if_empty_in_transaction(connection, goal_id, timestamp=timestamp, actor_id=actor_id)
            after = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()["status"]
            return self._goal_drain_observation_in_transaction(
                connection, goal_id, limit=limit, offset=offset, timestamp=timestamp,
                status_before=before, status_after=after, read_only=False,
            )

    def _lifecycle(self, goal_id: str, target: str, *, actor_id: str,
                   envelope_sha256: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        actor_id = _identifier(actor_id, label="actor_id")
        timestamp = _timestamp(at)
        allowed = {"active": {"planned", "paused", "draining"}, "paused": {"active", "draining"}, "stopped": {"planned", "active", "draining", "paused"}}
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("SELECT status,acceptance FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None: raise StateError(f"Unknown goal: {goal_id}")
            if row["status"] not in allowed[target]: raise StateError(f"Cannot transition goal from {row['status']} to {target}")
            if target == "active":
                if row["status"] == "planned" and not _decode(row["acceptance"], []): raise StateError("activation requires nonempty acceptance criteria")
                contract_row = connection.execute("SELECT envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
                exact = envelope_sha256 or (contract_row["envelope_sha256"] if contract_row else None)
                if not exact: raise StateError("activation/resume requires exact envelope_sha256")
                try:
                    from .authority import validate_authority_envelope
                    contract_value = validate_authority_envelope(_decode(contract_row["contract"], {}))
                except ValueError as error:
                    raise StateError(f"stored authority envelope is invalid: {error}") from error
                action = "goal-resume" if row["status"] in {"paused", "draining"} else "goal-activate"
                self._check_authorization_in_transaction(connection, goal_id=goal_id, action=action,
                    envelope_sha256=exact, performer_id=actor_id, scope=contract_value["scope"],
                    effect="local-reversible-write", timestamp=timestamp)
                self._dependencies_complete_in_transaction(connection, goal_id)
                self._verify_goal_checkpoints_in_transaction(connection, goal_id, contract_value)
            connection.execute("UPDATE goals SET status=?,updated_at=? WHERE id=?", (target, timestamp, goal_id))
            if target in {"paused", "stopped"}:
                attempt_status = "paused" if target == "paused" else "stopped"
                live_attempts = connection.execute(
                    """SELECT id,tokens_reserved FROM work_attempts
                       WHERE work_unit_id IN (SELECT id FROM work_units WHERE goal_id=?)
                       AND status IN ('leased','active')""",
                    (goal_id,),
                ).fetchall()
                reserved = sum(int(attempt["tokens_reserved"]) for attempt in live_attempts)
                elapsed = connection.execute("SELECT COALESCE(sum(min(max(0,CAST((julianday(?) - julianday(acquired_at))*86400000 AS INTEGER)),max(0,CAST((julianday(expires_at) - julianday(acquired_at))*86400000 AS INTEGER)))),0) FROM work_attempts WHERE work_unit_id IN (SELECT id FROM work_units WHERE goal_id=?) AND status IN ('leased','active')", (timestamp, goal_id)).fetchone()[0]
                connection.execute("UPDATE budgets SET consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?", (elapsed, timestamp, goal_id))
                connection.execute("UPDATE work_attempts SET elapsed_ms=elapsed_ms+min(max(0,CAST((julianday(?) - julianday(acquired_at))*86400000 AS INTEGER)),max(0,CAST((julianday(expires_at) - julianday(acquired_at))*86400000 AS INTEGER))) WHERE work_unit_id IN (SELECT id FROM work_units WHERE goal_id=?) AND status IN ('leased','active')", (timestamp, goal_id))
                for attempt in live_attempts:
                    charge = int(attempt["tokens_reserved"])
                    outcome_evidence: dict[str, Any] = {"actor_id": actor_id, "reason": f"goal {target}"}
                    if charge:
                        outcome_evidence["unmeasured_usage"] = unmeasured_usage_evidence(
                            charged_tokens=charge, reason=f"goal-{target}",
                        )
                    connection.execute(
                        """UPDATE work_attempts SET status=?,ended_at=?,outcome_class=?,outcome_json=?,
                           tokens_consumed=tokens_consumed+?,token_accounting_source='unavailable' WHERE id=?""",
                        (attempt_status, timestamp, attempt_status, _encode(outcome_evidence), charge, attempt["id"]),
                    )
                connection.execute("UPDATE work_units SET status=?,current_attempt_id=NULL,lease_holder=NULL,lease_expires_at=NULL,updated_at=? WHERE goal_id=? AND status='leased'", (target, timestamp, goal_id))
                connection.execute("UPDATE budgets SET reserved_tokens=max(0,reserved_tokens-?),consumed_tokens=consumed_tokens+?,updated_at=? WHERE goal_id=?", (reserved, reserved, timestamp, goal_id))
            elif target == "active" and row["status"] == "paused":
                connection.execute("UPDATE work_units SET status='eligible',updated_at=? WHERE goal_id=? AND status='paused'", (timestamp, goal_id))
            self._append_event_in_transaction(connection, f"goal.{target}", goal_id=goal_id, payload={"actor_id": actor_id})
        return self.get_goal(goal_id) or {}

    def activate_goal(self, goal_id: str, *, actor_id: str, envelope_sha256: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        return self._lifecycle(goal_id, "active", actor_id=actor_id, envelope_sha256=envelope_sha256, at=at)

    def pause_goal(self, goal_id: str, *, actor_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        return self._lifecycle(goal_id, "paused", actor_id=actor_id, at=at)

    def resume_goal(self, goal_id: str, *, actor_id: str, envelope_sha256: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        return self._lifecycle(goal_id, "active", actor_id=actor_id, envelope_sha256=envelope_sha256, at=at)

    def stop_goal(self, goal_id: str, *, actor_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        return self._lifecycle(goal_id, "stopped", actor_id=actor_id, at=at)

    def set_emergency_stop(self, *, actor_id: str, reason: str, at: str | datetime | None = None) -> bool:
        actor_id = _identifier(actor_id, label="actor_id")
        if not isinstance(reason, str) or not reason.strip(): raise StateError("emergency stop reason must be non-empty")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                return False
            connection.execute("UPDATE runtime_control SET emergency_stopped=1,reason=?,set_by=?,set_at=? WHERE id=1", (reason, actor_id, timestamp))
            live_attempts = connection.execute(
                "SELECT id,tokens_reserved FROM work_attempts WHERE status IN ('leased','active')"
            ).fetchall()
            connection.execute("UPDATE budgets SET consumed_elapsed_ms=consumed_elapsed_ms+COALESCE((SELECT sum(min(max(0,CAST((julianday(?) - julianday(a.acquired_at))*86400000 AS INTEGER)),max(0,CAST((julianday(a.expires_at) - julianday(a.acquired_at))*86400000 AS INTEGER)))) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=budgets.goal_id AND a.status IN ('leased','active')),0),consumed_tokens=consumed_tokens+reserved_tokens,reserved_tokens=0,updated_at=?", (timestamp, timestamp))
            for attempt in live_attempts:
                charge = int(attempt["tokens_reserved"])
                outcome_evidence: dict[str, Any] = {"actor_id": actor_id, "reason": reason}
                if charge:
                    outcome_evidence["unmeasured_usage"] = unmeasured_usage_evidence(
                        charged_tokens=charge, reason="emergency-stop",
                    )
                connection.execute(
                    """UPDATE work_attempts SET elapsed_ms=elapsed_ms+min(max(0,CAST((julianday(?) - julianday(acquired_at))*86400000 AS INTEGER)),max(0,CAST((julianday(expires_at) - julianday(acquired_at))*86400000 AS INTEGER))),
                       status='paused',ended_at=?,outcome_class='paused',outcome_json=?,tokens_consumed=tokens_consumed+?,
                       token_accounting_source='unavailable' WHERE id=?""",
                    (timestamp, timestamp, _encode(outcome_evidence), charge, attempt["id"]),
                )
            connection.execute("UPDATE work_units SET status='paused',current_attempt_id=NULL,lease_holder=NULL,lease_expires_at=NULL,updated_at=? WHERE status='leased'", (timestamp,))
            connection.execute("UPDATE goals SET status='paused',updated_at=? WHERE status IN ('active','draining')", (timestamp,))
            connection.execute("UPDATE budgets SET reserved_tokens=0,updated_at=? WHERE reserved_tokens > 0", (timestamp,))
            self._append_event_in_transaction(connection, "runtime.emergency_stop_set", payload={"actor_id": actor_id, "reason": reason})
        return True

    def clear_emergency_stop(self, *, actor_id: str, approver_kind: str,
                             at: str | datetime | None = None) -> bool:
        actor_id = _identifier(actor_id, label="actor_id")
        if approver_kind != "human":
            raise StateError("clearing an emergency stop requires an explicit human actor")
        timestamp = _timestamp(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            if not connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                return False
            connection.execute("UPDATE runtime_control SET emergency_stopped=0,cleared_by=?,cleared_at=? WHERE id=1", (actor_id, timestamp))
            self._append_event_in_transaction(connection, "runtime.emergency_stop_cleared", payload={"actor_id": actor_id, "approver_kind": approver_kind})
        return True

    def verify_audit(self, *, limit: int = 8) -> dict[str, Any]:
        """Check SQLite/FKs/sequence/hash chain and return a bounded diagnostic."""
        self._ensure(); issues: list[str] = []; limit = max(1, min(int(limit), 32))
        with self._connection(write=False) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok": issues.append("sqlite integrity check failed")
            for row in connection.execute("PRAGMA foreign_key_check"):
                if len(issues) >= limit: break
                issues.append(f"foreign key violation in {row[0]}")
            previous, expected = _GENESIS_HASH, 1
            for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
                if len(issues) >= limit: break
                actual = _audit_hash(previous, row["sequence"], row["event_type"], row["goal_id"], row["work_unit_id"], row["payload"], row["created_at"])
                if row["sequence"] != expected: issues.append(f"audit sequence expected {expected}, found {row['sequence']}")
                if row["previous_hash"] != previous or row["event_hash"] != actual: issues.append(f"audit hash mismatch at sequence {row['sequence']}")
                previous, expected = row["event_hash"], row["sequence"] + 1
            for table, row_id, row in self._iter_authoritative_rows(connection):
                if len(issues) >= limit:
                    break
                seal = self._latest_seal_hash(connection, table, row_id)
                if seal is None:
                    label = "authority row" if table in {"goal_contracts", "transition_approvals", "goal_checkpoints"} else "authoritative row"
                    issues.append(f"unsealed {label} in {table} ({row_id})")
                elif seal != _authority_row_hash(table, row):
                    label = "authority row" if table in {"goal_contracts", "transition_approvals", "goal_checkpoints"} else "authoritative row"
                    issues.append(f"{label} tampered in {table} ({row_id})")
            if len(issues) < limit:
                manifest = self._latest_seal_hash(connection, _STATE_MANIFEST_TABLE, _STATE_MANIFEST_ID)
                if manifest is None:
                    issues.append("unsealed authoritative current-state manifest")
                elif manifest != self._state_manifest_hash(connection):
                    issues.append("authoritative current-state manifest tampered")
            if len(issues) < limit and int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 13:
                try:
                    self._assert_intervention_integrity_in_transaction(connection)
                except StateError as error:
                    issues.append(str(error))
            if len(issues) < limit and int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 14:
                try:
                    self._assert_codex_run_integrity_in_transaction(connection)
                except StateError as error:
                    issues.append(str(error))
            if len(issues) < limit:
                try:
                    # Hashes establish provenance; this second pass verifies
                    # that recorded short-policy proof still has the required
                    # command, receipt, and reviewed-artifact semantics.
                    self._validate_current_state_for_attestation(connection)
                except StateError as error:
                    issues.append(f"current-state evidence invalid: {error}")
        return {"ok": not issues, "issues": issues, "checked": expected - 1}

    def append_event(self, event_type: str, *, goal_id: str | None = None, work_unit_id: str | None = None,
                     payload: dict[str, Any] | None = None) -> int:
        goal_id = _optional_identifier(goal_id, label="goal_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        with self._connection() as connection:
            self._prepare_write(connection)
            return self._append_event_in_transaction(connection, event_type, goal_id=goal_id, work_unit_id=work_unit_id, payload=payload)

    def consume_budget(self, goal_id: str, tokens: int) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        if tokens < 0:
            raise StateError("tokens must be non-negative")
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("SELECT total_tokens, consumed_tokens, reserved_tokens FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                raise StateError(f"Unknown goal: {goal_id}")
            if row["total_tokens"] is not None and row["consumed_tokens"] + row["reserved_tokens"] + tokens > row["total_tokens"]:
                raise StateError("Budget would be exceeded")
            connection.execute("UPDATE budgets SET consumed_tokens=consumed_tokens+?, updated_at=? WHERE goal_id=?", (tokens, _now(), goal_id))
            self._append_event_in_transaction(connection, "budget.consumed", goal_id=goal_id, payload={"tokens": tokens})
        return self.budget_summary(goal_id)

    def budget_summary(self, goal_id: str) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        self._ensure()
        with self._connection(write=False) as connection:
            row = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                raise StateError(f"Unknown goal: {goal_id}")
            result = dict(row)
            result["remaining_tokens"] = None if result["total_tokens"] is None else result["total_tokens"] - result["consumed_tokens"] - result["reserved_tokens"]
            return result

    def status(self) -> dict[str, Any]:
        self._ensure()
        with self._connection(write=False) as connection:
            return {
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "goals": connection.execute("SELECT count(*) FROM goals").fetchone()[0],
                "active_goals": connection.execute("SELECT count(*) FROM goals WHERE status='active'").fetchone()[0],
                "work_units": connection.execute("SELECT count(*) FROM work_units").fetchone()[0],
                "events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
            }
