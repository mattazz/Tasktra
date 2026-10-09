"""Atomic, resumable execution on top of the Stage 3 SQLite ledger."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
from pathlib import PurePosixPath
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

from .state import (
    SCHEMA_VERSION, StateError, StateStore, _attempt_binding_sha256, _authority_row_hash, _decode, _encode, _identifier, _optional_identifier,
    _row, _timestamp, unmeasured_usage_evidence, validate_deterministic_review_completion_evidence,
    validate_observed_usage_evidence,
)
from .authority import load_authority_envelope
from .providers import OperationDescriptor, ProviderError, ResourceScope
from .workflow import (
    WorkflowError,
    serialize_workflow,
    validate_workflow_completion_token,
    workflow_completion_token,
)
from .interventions import (
    InterventionError,
    canonical_intervention_request,
    canonical_intervention_response,
    intervention_request_sha256,
    intervention_response_sha256,
    validate_intervention_request,
    validate_intervention_response,
)
from .codex_runs import CodexRunError, contains_secret, require_digest, sha256_json, task_name, validate_finish
from .delegation import DelegationError, delegation_plan


WORK_CLAIM_ACTION = "work-claim"
WORK_COMPLETE_ACTION = "work-complete"
WORK_REQUEUE_ACTION = "work-requeue"
LOCAL_REVERSIBLE_WRITE = "local-reversible-write"
OUTCOMES = frozenset({"success", "transient", "permanent", "blocked", "approval-required", "exhausted"})
LOCAL_EFFECT_RECEIPT_OUTCOMES = frozenset({"applied", "success", "failed-before-effect", "indeterminate", "recovery-required"})
MAX_LEASE_SECONDS = 3_600
MAX_CONTEXT_CHARS = 500
MAX_CANONICAL_JSON_BYTES = 64 * 1024
_PROVIDER_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "indeterminate"})
_SECRET_FIELD_MARKERS = ("access", "api_key", "authorization", "bearer", "cookie", "credential", "password", "secret", "token")
_SECRET_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+\S+|\b(?:authorization|access[-_ ]?token|api[-_ ]?key|token|password|cookie)\s*[:=]\s*\S+)"
)

# Compatibility import name.  It intentionally names the same closed type as
# the registry rather than a ledger-only lookalike descriptor.
ProviderOperationDescriptor = OperationDescriptor


class AutonomyError(StateError):
    """Raised when an autonomous execution transition is not eligible."""


class InterventionConflictError(AutonomyError):
    """A bounded, retryable intervention compare-and-swap conflict."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


def _response_head_changed(head: Any) -> InterventionConflictError:
    return InterventionConflictError("response_head_changed", details={
        "current_response_id": None if head is None else head["current_response_id"],
        "current_response_sha256": None if head is None else head["current_response_sha256"],
    })


def _clock(value: str | datetime | None) -> tuple[str, datetime]:
    timestamp = _timestamp(value)
    return timestamp, datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_hash(value: Mapping[str, Any]) -> tuple[str, str]:
    def require_json(item: Any) -> None:
        if item is None or isinstance(item, (bool, int, float, str)):
            return
        if isinstance(item, list):
            for child in item:
                require_json(child)
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise AutonomyError("effect payload must use string JSON object keys")
                require_json(child)
            return
        raise AutonomyError("effect payload must use strict JSON containers")

    require_json(value)
    try:
        canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded = canonical.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise AutonomyError(f"effect request must be JSON-compatible: {error}") from error
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise AutonomyError(f"canonical JSON exceeds the {MAX_CANONICAL_JSON_BYTES}-byte limit")
    return canonical, sha256(encoded).hexdigest()


def _contains_supplied_lease_token(value: Any, lease_token: str) -> bool:
    """A lease secret authorizes the transition but is never durable evidence."""
    if not lease_token:
        return False
    if isinstance(value, str):
        return lease_token in value
    if isinstance(value, Mapping):
        return any(_contains_supplied_lease_token(item, lease_token) for item in value.values())
    if isinstance(value, list):
        return any(_contains_supplied_lease_token(item, lease_token) for item in value)
    return False


def _contains_persisted_lease_token(value: Any, lease_token_hash: str, token_length: int | None = None) -> bool:
    """Reject exact or embedded bounded token candidates without retaining plaintext."""
    if isinstance(value, str):
        candidates = [value]
        if token_length is None:
            for length in range(32, min(512, len(value)) + 1):
                candidates.extend(value[index:index + length] for index in range(len(value) - length + 1))
        elif len(value) >= token_length:
            candidates.extend(value[index:index + token_length] for index in range(len(value) - token_length + 1))
        for candidate in candidates:
            if hmac.compare_digest(sha256(candidate.encode("utf-8")).hexdigest(), lease_token_hash):
                return True
        return False
    if isinstance(value, Mapping):
        return any(_contains_persisted_lease_token(item, lease_token_hash, token_length) for item in value.values())
    if isinstance(value, list):
        return any(_contains_persisted_lease_token(item, lease_token_hash, token_length) for item in value)
    return False


def _scope_paths(scope: Mapping[str, Any], *, label: str, permit_empty: bool) -> tuple[list[str], list[str]]:
    """Read a portable scope.  Empty approval scope deliberately means envelope-wide."""
    if not isinstance(scope, Mapping):
        raise AutonomyError(f"{label} scope must be an object")
    if not scope and permit_empty:
        return ["."], []
    if set(scope) - {"paths", "exclusions"}:
        raise AutonomyError(f"{label} scope has unsupported fields")
    paths, exclusions = scope.get("paths"), scope.get("exclusions", [])
    if not isinstance(paths, list) or not paths or not isinstance(exclusions, list):
        raise AutonomyError(f"{label} scope requires non-empty paths and optional exclusions")
    result: list[list[str]] = []
    for values, name, allow_root in ((paths, "paths", True), (exclusions, "exclusions", False)):
        validated: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value or (value == "." and not allow_root) or "\\" in value or ":" in value:
                raise AutonomyError(f"{label} scope {name} must contain portable relative paths")
            path = PurePosixPath(value)
            if value != path.as_posix() or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                if value != "." or not allow_root:
                    raise AutonomyError(f"{label} scope {name} escapes the project root")
            validated.append(value)
        result.append(validated)
    return result[0], result[1]


def _scope_covers(approval_scope: Mapping[str, Any], requested_scope: Mapping[str, Any]) -> bool:
    """Return whether a transition approval's scope covers all requested paths.

    An empty approval scope is intentionally envelope-wide; a non-empty scope
    is fail-closed when a unit does not declare bounded paths.
    """
    if not approval_scope:
        return True
    allowed, excluded = _scope_paths(approval_scope, label="approval", permit_empty=True)
    requested, requested_exclusions = _scope_paths(requested_scope, label="work unit", permit_empty=False)
    return StateStore._scope_within_contract(
        {"paths": requested, "exclusions": requested_exclusions},
        {"paths": allowed, "exclusions": excluded},
    )


def _git_commit_request_scope(descriptor: OperationDescriptor, request: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """Return the target path scope for a Git commit without granting authority.

    Commit paths are untrusted request data.  They identify targets that must
    fit within already-authorized scopes; they can never expand those scopes.
    """
    if descriptor.provider != "git" or descriptor.capability != "git-commit":
        return None
    paths = request.get("paths")
    if not isinstance(paths, list) or not paths:
        raise AutonomyError("Git commit request requires non-empty paths")
    validated_paths, _ = _scope_paths(
        {"paths": paths, "exclusions": []},
        label="Git commit request",
        permit_empty=False,
    )
    return {"paths": validated_paths, "exclusions": []}


def _bound_intent_request(request: Mapping[str, Any], performer_id: str) -> tuple[str, str]:
    """Store request attribution in the canonical payload without changing its digest."""
    request_json, request_hash = _json_hash(request)
    bound_json, _ = _json_hash({"authorized_performer_id": performer_id, "request": json.loads(request_json)})
    return bound_json, request_hash


def _intent_performer(intent: Any) -> str:
    try:
        payload = json.loads(intent["request_json"])
        performer = payload["authorized_performer_id"]
        request = payload["request"]
        if not isinstance(performer, str) or not isinstance(request, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AutonomyError("effect intent lacks a valid authorized performer binding") from error
    return performer


def _intent_request(intent: Any) -> dict[str, Any]:
    """Restore the immutable, performer-bound request stored with an intent."""
    try:
        payload = json.loads(intent["request_json"])
        request = payload["request"]
        if not isinstance(payload["authorized_performer_id"], str) or not isinstance(request, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AutonomyError("effect intent lacks a valid bound request") from error
    return request


def _provider_descriptor(value: Mapping[str, Any] | OperationDescriptor) -> OperationDescriptor:
    """Accept only the registry's closed descriptor representation."""
    try:
        descriptor = value if isinstance(value, OperationDescriptor) else OperationDescriptor.from_mapping(value)
    except ProviderError as error:
        raise AutonomyError(f"provider operation descriptor is invalid: {error}") from error
    if not descriptor.is_trusted_shape:
        raise AutonomyError("provider operation descriptor must be a closed protocol-v2 descriptor")
    return descriptor


def _intent_descriptor(intent: Mapping[str, Any]) -> OperationDescriptor:
    """Restore the one descriptor identity persisted across intent columns."""
    try:
        return OperationDescriptor(
            intent["provider"], intent["effect_class"], capability=intent["capability"],
            action=intent["operation"], resource_scope=ResourceScope.from_mapping(_decode(intent["resource_scope"], {})),
            protocol_version=intent["protocol_version"],
        )
    except (KeyError, ProviderError, TypeError) as error:
        raise AutonomyError("provider effect intent lacks a valid descriptor identity") from error


def _sanitized_json(value: Mapping[str, Any], *, label: str) -> str:
    """Bound observations and ensure credential-shaped values never enter the ledger."""
    if not isinstance(value, Mapping):
        raise AutonomyError(f"{label} must be an object")

    def secret_text(text: str) -> bool:
        parsed = urlparse(text)
        credential_url = parsed.scheme in {"http", "https"} and (
            parsed.username is not None
            or parsed.password is not None
            or any(
                any(marker in query_key.casefold() for marker in _SECRET_FIELD_MARKERS)
                for query_key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        )
        return credential_url or bool(_SECRET_VALUE.search(text))

    def secret_key(key: str) -> bool:
        return any(marker in key.casefold() for marker in _SECRET_FIELD_MARKERS) or secret_text(key)

    def clean(item: Any, key: str = "") -> Any:
        if secret_key(key):
            return "[redacted]"
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for child_key, child in item.items():
                if not isinstance(child_key, str):
                    raise AutonomyError(f"{label} must use string JSON object keys")
                rendered_key = str(child_key)
                # A credential can be smuggled in a mapping key just as easily
                # as in its value.  Do not retain either in durable evidence.
                if secret_key(rendered_key):
                    rendered_key = "[redacted-key]"
                    result[rendered_key] = "[redacted]"
                else:
                    result[rendered_key] = clean(child, rendered_key)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, (tuple, set)):
            raise AutonomyError(f"{label} must use strict JSON containers")
        if isinstance(item, str):
            if secret_text(item):
                return "[redacted]"
        return item
    encoded, _ = _json_hash(clean(value))
    return encoded


def _reject_secret_fields(value: Mapping[str, Any], *, label: str) -> None:
    """Provider credentials belong to the host, never to a durable request."""
    def secret_text(text: str) -> bool:
        parsed = urlparse(text)
        if _SECRET_VALUE.search(text):
            return True
        return parsed.scheme in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
            or any(any(marker in query_key.casefold() for marker in _SECRET_FIELD_MARKERS)
                   for query_key, _ in parse_qsl(parsed.query, keep_blank_values=True))
        )

    def visit(item: Any, key: str = "") -> bool:
        if any(marker in key.casefold() for marker in _SECRET_FIELD_MARKERS) or secret_text(key):
            return True
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                if not isinstance(child_key, str):
                    raise AutonomyError(f"{label} must use string JSON object keys")
                if visit(child, child_key):
                    return True
            return False
        if isinstance(item, list):
            return any(visit(child) for child in item)
        if isinstance(item, (tuple, set)):
            raise AutonomyError(f"{label} must use strict JSON containers")
        if isinstance(item, str):
            return secret_text(item)
        return False
    if visit(value):
        raise AutonomyError(f"{label} must not contain credentials or secrets")


def _resource_scope_within(child: Mapping[str, Any], parent: Mapping[str, Any]) -> bool:
    """Use the v2 authority comparator when available; retain a strict v1 fallback."""
    try:
        from .authority import resource_scope_within
        return bool(resource_scope_within(dict(child), dict(parent)))
    except (ImportError, AttributeError):
        if set(child) != {"paths", "exclusions"} or set(parent) != {"paths", "exclusions"}:
            return False
        return StateStore._scope_within_contract(dict(child), dict(parent))


class AutonomyStore(StateStore):
    """Lease work, preserve evidence, and make idempotent effect intentions durable."""

    @staticmethod
    def _append(connection: Any, event_type: str, *, goal_id: str | None = None,
                work_unit_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        StateStore._append_event_in_transaction(
            connection, event_type, goal_id=goal_id, work_unit_id=work_unit_id, payload=payload
        )

    @staticmethod
    def _authorize(connection: Any, *, goal_id: str, work_unit_id: str | None,
                   action: str, envelope_sha256: str, performer_id: str,
                   effect: str, timestamp: str, require_human: bool = False,
                   require_completion_evidence: bool = False) -> dict[str, Any]:
        if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
            raise AutonomyError("runtime is emergency-stopped")
        contract = connection.execute(
            "SELECT version,envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if contract is None or contract["version"] not in {"v1", "v2"} or contract["envelope_sha256"] != envelope_sha256:
            raise AutonomyError("authorization requires the exact current envelope hash")
        try:
            envelope = load_authority_envelope(contract["contract"])
        except ValueError as error:
            raise AutonomyError(f"stored authority envelope is invalid: {error}") from error
        if action not in envelope["allowed_actions"]:
            raise AutonomyError("authority envelope does not allow this action")
        if effect not in envelope["allowed_effects"]:
            raise AutonomyError("authority envelope does not allow this effect")
        if work_unit_id is not None:
            unit = connection.execute("SELECT scope FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None:
                raise AutonomyError("unknown work unit")
            unit_scope = json.loads(unit["scope"])
            if not _scope_covers(envelope["scope"], unit_scope):
                raise AutonomyError("work unit scope is outside the authority envelope")
        rows = connection.execute(
            """SELECT * FROM transition_approvals
               WHERE goal_id=? AND action=? AND envelope_sha256=? AND performer_id=?
                 AND decision='approved' AND revoked_at IS NULL
                 AND (work_unit_id IS NULL OR work_unit_id=?)
               ORDER BY CASE WHEN work_unit_id IS NULL THEN 1 ELSE 0 END, created_at DESC, rowid DESC""",
            (goal_id, action, envelope_sha256, performer_id, work_unit_id),
        )
        for row in rows:
            approval = _row(row) or {}
            if approval["approver_id"] == performer_id:
                continue
            # An approval without an expiry is malformed legacy authority,
            # never an indefinite permission to claim work.
            if approval["valid_until"] is None:
                continue
            if require_human and approval["approver_kind"] != "human":
                continue
            if approval["valid_until"] is not None and approval["valid_until"] <= timestamp:
                continue
            if approval["effect"] != effect:
                continue
            if work_unit_id is None and approval["scope"] and approval["scope"] != envelope["scope"]:
                # A narrowed approval has no safe interpretation for a
                # goal-scoped operation with no requested work-unit paths.
                continue
            if work_unit_id is not None and not _scope_covers(approval["scope"], unit_scope):
                continue
            try:
                if int(approval.get("protocol_version") or 1) >= 3:
                    AutonomyStore._verify_approval_evidence(connection, goal_id, approval)
                if require_completion_evidence:
                    AutonomyStore._verify_completion_approval_evidence(connection, goal_id, approval)
            except AutonomyError:
                # A newer permission-to-record approval may intentionally
                # lack final evidence. Continue looking for the newest usable
                # final ceremony rather than making same-second records race.
                continue
            return approval
        raise AutonomyError("no current approval bound to the exact action, effect, envelope, performer, and work unit")

    @staticmethod
    def _verify_approval_evidence(connection: Any, goal_id: str, approval: Mapping[str, Any]) -> None:
        """Resolve every v3 evidence reference against its immutable ledger hash."""
        for reference in approval.get("evidence", []):
            kind, identifier, expected = reference["kind"], reference["id"], reference["sha256"]
            if kind == "acceptance-evidence":
                row = connection.execute(
                    "SELECT evidence_json FROM acceptance_evidence WHERE goal_id=? AND criterion_id=?",
                    (goal_id, identifier),
                ).fetchone()
                actual = None if row is None else sha256(row["evidence_json"].encode("utf-8")).hexdigest()
            elif kind == "workflow-evidence":
                row = connection.execute(
                    "SELECT workflow_sha256 FROM workflow_evidence WHERE work_unit_id=?", (identifier,)
                ).fetchone()
                actual = None if row is None else row["workflow_sha256"]
            elif kind == "checkpoint-evidence":
                row = connection.execute(
                    "SELECT evidence_json,status FROM goal_checkpoints WHERE goal_id=? AND checkpoint_id=?",
                    (goal_id, identifier),
                ).fetchone()
                actual = None if row is None or row["status"] != "reached" else sha256(row["evidence_json"].encode("utf-8")).hexdigest()
            else:  # The authority contract has already closed this enum.
                raise AutonomyError("approval evidence kind is not supported")
            if actual != expected:
                raise AutonomyError(
                    f"approval evidence is absent or its verified hash differs: {kind}/{identifier}"
                )

    @staticmethod
    def _verify_completion_approval_evidence(connection: Any, goal_id: str, approval: Mapping[str, Any]) -> None:
        """Require human-bound final approval to bind every recorded acceptance fact."""
        if int(approval.get("protocol_version") or 1) < 3:
            raise AutonomyError(
                "goal completion requires a v3+ human-bound approval with verified evidence; re-record approval"
            )
        AutonomyStore._verify_approval_evidence(connection, goal_id, approval)
        criteria = {
            row["criterion_id"]: sha256(row["evidence_json"].encode("utf-8")).hexdigest()
            for row in connection.execute(
                "SELECT criterion_id,evidence_json FROM acceptance_evidence WHERE goal_id=?", (goal_id,)
            )
        }
        bound = {
            reference["id"]: reference["sha256"]
            for reference in approval["evidence"]
            if reference["kind"] == "acceptance-evidence"
        }
        if not criteria or criteria != bound:
            raise AutonomyError(
                "goal completion approval lacks verified bindings for every acceptance evidence record"
            )

    @staticmethod
    def _active_goal(connection: Any, goal_id: str, *, allow_draining: bool = False) -> None:
        row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            raise AutonomyError(f"Unknown goal: {goal_id}")
        if row["status"] not in ({"active", "draining"} if allow_draining else {"active"}):
            raise AutonomyError("goal is not active")

    @staticmethod
    def _lease_expiry(now: datetime, seconds: int, available_ms: int | None) -> tuple[str, datetime]:
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= MAX_LEASE_SECONDS:
            raise AutonomyError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")
        expiry = now + timedelta(seconds=seconds)
        if available_ms is not None:
            if available_ms <= 0:
                raise AutonomyError("elapsed budget is exhausted")
            expiry = min(expiry, now + timedelta(milliseconds=available_ms))
        return _iso(expiry), expiry

    @staticmethod
    def _attempt_elapsed(attempt: Any, now: datetime) -> int:
        started = datetime.fromisoformat(attempt["acquired_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        return max(0, int((now - started).total_seconds() * 1000))

    @staticmethod
    def _lease_reservation_ms(attempt: Any) -> int:
        acquired = datetime.fromisoformat(attempt["acquired_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        expires = datetime.fromisoformat(attempt["expires_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        return max(0, int((expires - acquired).total_seconds() * 1000))

    @staticmethod
    def _codex_accounting_in_transaction(connection: Any, *, attempt_id: str,
                                         tokens_consumed: int | None, accounting_source: str | None) -> tuple[int, str]:
        if tokens_consumed is not None and (not isinstance(tokens_consumed, int) or isinstance(tokens_consumed, bool) or tokens_consumed < 0):
            raise AutonomyError("tokens_consumed must be a non-negative integer or null")
        source = accounting_source or ("unavailable" if tokens_consumed is None else "caller-declared")
        if source not in {"caller-declared", "host-measured", "unavailable"}:
            raise AutonomyError("accounting_source must be caller-declared, host-measured, or unavailable")
        if source == "unavailable":
            if tokens_consumed is not None:
                raise AutonomyError("unavailable accounting requires null tokens_consumed")
            return 0, source
        if tokens_consumed is None:
            raise AutonomyError("declared accounting requires an explicit tokens_consumed value")
        if source == "host-measured":
            totals = connection.execute(
                """SELECT count(p.id) AS prepared, count(f.run_id) AS finished,
                    COALESCE(sum(CASE WHEN f.usage_status='measured' THEN f.input_tokens+f.output_tokens END),0) AS measured
                   FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id WHERE p.attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            if int(totals["prepared"]) == 0 or int(totals["prepared"]) != int(totals["finished"]) or connection.execute(
                """SELECT 1 FROM codex_run_preparations p JOIN codex_run_finishes f ON f.run_id=p.id
                   WHERE p.attempt_id=? AND f.usage_status!='measured' LIMIT 1""", (attempt_id,)
            ).fetchone() is not None or int(totals["measured"]) != tokens_consumed:
                raise AutonomyError("host-measured accounting requires complete measured terminal Codex runs with the exact token sum")
        return tokens_consumed, source

    @staticmethod
    def _live_reservation_ms(connection: Any, goal_id: str, *, excluding_attempt_id: str | None = None) -> int:
        query = """SELECT a.acquired_at,a.expires_at FROM work_attempts a
                   JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'"""
        params: list[Any] = [goal_id]
        if excluding_attempt_id is not None:
            query += " AND a.id!=?"
            params.append(excluding_attempt_id)
        return sum(AutonomyStore._lease_reservation_ms(row) for row in connection.execute(query, params))

    @contextmanager
    def _readonly_connection(self) -> Any:
        """Open one immutable SQLite snapshot without creating or repairing state."""
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        except (OSError, sqlite3.Error) as error:
            raise StateError(f"runtime database cannot be opened read-only: {self.path}") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _prepare_readonly(connection: Any) -> None:
        """Verify a complete sealed ledger before a preview relies on it."""
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            StateStore._assert_audit_chain_in_transaction(connection)
            StateStore._assert_current_state_integrity_in_transaction(connection)
        except sqlite3.Error as error:
            raise StateError("runtime state cannot be read or its integrity verified") from error

    @staticmethod
    def _selection_reason(error: AutonomyError) -> str:
        """Expose a stable category without leaking approval or scope contents."""
        message = str(error)
        if "exact current envelope hash" in message:
            return "authorization.envelope_mismatch"
        if "does not allow this action" in message:
            return "authorization.action_not_allowed"
        if "does not allow this effect" in message:
            return "authorization.effect_not_allowed"
        if "scope is outside the authority envelope" in message:
            return "authorization.scope_outside_envelope"
        if "no current approval" in message:
            return "approval.unavailable"
        if "emergency-stopped" in message:
            return "runtime.emergency_stopped"
        return "authorization.unavailable"

    def _select_next_work(self, connection: Any, *, goal_id: str, performer_id: str,
                          envelope_sha256: str, lease_seconds: int,
                          token_reservation: int, timestamp: str, now: datetime,
                          mutate_exhausted: bool, explain: bool = False,
                          candidate_filter: str | None = None,
                          claim_filter: str | None = None,
                          candidate_limit: int = 0, candidate_offset: int = 0) -> dict[str, Any]:
        """Evaluate the queue once for both claim and explanation.

        ``mutate_exhausted`` preserves the historic claim-side transition for a
        unit which has consumed its per-unit attempt limit.  Preview leaves the
        same unit visible and reports why it cannot be chosen.
        """
        budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
        if budget is None:
            if explain:
                missing = ["goal.budgets_missing"]
                if connection.execute("SELECT 1 FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone() is None:
                    missing.append("goal.contract_missing")
                return self._empty_work_selection(connection, goal_id, missing, candidate_filter, candidate_limit, candidate_offset)
            raise AutonomyError(f"Unknown goal: {goal_id}")
        if budget["max_concurrency"] is None or budget["total_attempts"] is None:
            if explain:
                missing = ["goal.budgets_missing"]
                if connection.execute("SELECT 1 FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone() is None:
                    missing.append("goal.contract_missing")
                return self._empty_work_selection(connection, goal_id, missing, candidate_filter, candidate_limit, candidate_offset)
            raise AutonomyError("goal lacks Stage 3 execution budgets")
        contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        if contract_row is None:
            if explain:
                return self._empty_work_selection(connection, goal_id, "goal.contract_missing", candidate_filter, candidate_limit, candidate_offset)
            raise AutonomyError("goal lacks an authority envelope")
        try:
            contract = load_authority_envelope(contract_row["contract"])
        except ValueError as error:
            raise AutonomyError(f"stored authority envelope is invalid: {error}") from error

        goal_reasons: list[str] = []
        dependency_error: StateError | None = None
        try:
            self._dependencies_complete_in_transaction(connection, goal_id)
        except StateError as error:
            dependency_error = error
            goal_reasons.append("goal.dependencies_incomplete")
        if mutate_exhausted and dependency_error is not None:
            # Keep claim's established failure precedence: an incomplete
            # dependency is observed before checkpoint or budget gates.
            raise AutonomyError(str(dependency_error)) from dependency_error
        checkpoints = self._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
        next_checkpoint = next((row["checkpoint_id"] for row in checkpoints if row["status"] != "reached"), None)
        if contract["checkpoints"] and next_checkpoint is None:
            goal_reasons.append("goal.checkpoints_complete")

        active = connection.execute(
            """SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
               WHERE u.goal_id=? AND a.status='leased'""", (goal_id,)
        ).fetchone()[0]
        detached_runs = connection.execute(
            """SELECT count(*) FROM codex_run_preparations p JOIN work_attempts a ON a.id=p.attempt_id
               JOIN work_units u ON u.id=a.work_unit_id LEFT JOIN codex_run_finishes f ON f.run_id=p.id
               WHERE u.goal_id=? AND a.status!='leased' AND f.run_id IS NULL""", (goal_id,)
        ).fetchone()[0]
        effective_occupancy = int(active) + int(detached_runs)
        if effective_occupancy >= budget["max_concurrency"]:
            goal_reasons.append("budget.concurrency_exhausted")
            if mutate_exhausted:
                return {
                    "selected": None, "candidates": [], "candidate_total": 0,
                    "goal_reason_codes": goal_reasons, "active_leases": int(active), "effective_occupancy": effective_occupancy,
                    "max_concurrency": int(budget["max_concurrency"]), "lease_expires_at": None,
                }
        if budget["total_attempts"] is not None and budget["consumed_attempts"] >= budget["total_attempts"]:
            goal_reasons.append("budget.attempts_exhausted")
            if mutate_exhausted:
                raise AutonomyError("attempt budget is exhausted")
        if budget["total_tokens"] is not None and (
            budget["consumed_tokens"] + budget["reserved_tokens"] + token_reservation > budget["total_tokens"]
        ):
            goal_reasons.append("budget.tokens_exhausted")
            if mutate_exhausted:
                raise AutonomyError("token budget would be exceeded")
        live_reservation = self._live_reservation_ms(connection, goal_id)
        remaining_elapsed = None if budget["total_elapsed_ms"] is None else (
            int(budget["total_elapsed_ms"]) - int(budget["consumed_elapsed_ms"]) - live_reservation
        )
        try:
            expiry, _ = self._lease_expiry(now, lease_seconds, remaining_elapsed)
        except AutonomyError as error:
            if str(error) == "elapsed budget is exhausted":
                goal_reasons.append("budget.elapsed_exhausted")
                expiry = None
                if mutate_exhausted:
                    raise
            else:
                raise

        candidates: list[dict[str, Any]] = []
        candidate_total = 0
        selected: Any = None
        global_ready = not goal_reasons
        if mutate_exhausted and "goal.checkpoints_complete" in goal_reasons:
            return {
                    "selected": None, "candidates": [], "candidate_total": 0,
                    "goal_reason_codes": goal_reasons, "active_leases": int(active), "effective_occupancy": effective_occupancy,
                "max_concurrency": int(budget["max_concurrency"]), "lease_expires_at": expiry,
            }
        prerequisite_graph = self._work_dependency_graph_in_transaction(connection, goal_id)
        for candidate in connection.execute(
            "SELECT id,status,current_attempt_id,retry_at,checkpoint_id,attempt_count FROM work_units WHERE goal_id=? ORDER BY id",
            (goal_id,),
        ):
            if claim_filter is not None and candidate["id"] != claim_filter:
                continue
            reasons: list[str] = []
            claimable_status = candidate["status"] in {"eligible", "retry-wait", "planned"}
            if candidate["current_attempt_id"] is not None or candidate["status"] == "leased":
                reasons.append("candidate.lease_held")
            elif not claimable_status:
                reasons.append("candidate.status_not_claimable")
            if candidate["retry_at"] is not None and candidate["retry_at"] > timestamp:
                reasons.append("candidate.retry_wait")
            if contract["checkpoints"] and candidate["checkpoint_id"] != next_checkpoint:
                reasons.append("candidate.checkpoint_not_current")
            if not contract["checkpoints"] and candidate["checkpoint_id"] is not None:
                reasons.append("candidate.checkpoint_not_current")
            if candidate["attempt_count"] >= budget["total_attempts"]:
                reasons.append("candidate.attempts_exhausted")
                if mutate_exhausted and global_ready and selected is None and not reasons[:-1]:
                    connection.execute(
                        "UPDATE work_units SET status='exhausted',last_outcome_class='exhausted',updated_at=? WHERE id=?",
                        (timestamp, candidate["id"]),
                    )
            if not reasons and not prerequisite_graph[candidate["id"]]["ready"]:
                reasons.append("candidate.prerequisites_incomplete")
            locally_ready = not reasons
            if locally_ready:
                try:
                    self._authorize(connection, goal_id=goal_id, work_unit_id=candidate["id"], action=WORK_CLAIM_ACTION,
                                    envelope_sha256=envelope_sha256, performer_id=performer_id,
                                    effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp)
                except AutonomyError as error:
                    reasons.append(self._selection_reason(error))
            if selected is None and global_ready and not reasons:
                selected = candidate
                if mutate_exhausted:
                    # Claim only needs the first authorized candidate.  This
                    # also preserves its historic exhaustion-prefix behavior.
                    break
            if candidate_filter is None or candidate["id"] == candidate_filter:
                if candidate_total >= candidate_offset and len(candidates) < candidate_limit:
                    candidates.append({"row": candidate, "eligible": global_ready and not reasons, "reason_codes": reasons})
                candidate_total += 1

        if explain and selected is None and not goal_reasons:
            goal_reasons.append("queue.no_claimable_work")
        return {
            "selected": selected,
            "candidates": candidates,
            "candidate_total": candidate_total,
            "goal_reason_codes": goal_reasons,
            "active_leases": int(active),
            "effective_occupancy": effective_occupancy,
            "max_concurrency": int(budget["max_concurrency"]),
            "lease_expires_at": expiry,
        }

    @staticmethod
    def _empty_work_selection(connection: Any, goal_id: str, reasons: str | list[str],
                              candidate_filter: str | None, candidate_limit: int,
                              candidate_offset: int) -> dict[str, Any]:
        """Return a bounded queue view when state lacks selection prerequisites."""
        reason_codes = [reasons] if isinstance(reasons, str) else reasons
        candidates: list[dict[str, Any]] = []
        total = 0
        for row in connection.execute("SELECT id FROM work_units WHERE goal_id=? ORDER BY id", (goal_id,)):
            if candidate_filter is not None and row["id"] != candidate_filter:
                continue
            if total >= candidate_offset and len(candidates) < candidate_limit:
                candidates.append({"row": row, "eligible": False, "reason_codes": reason_codes})
            total += 1
        return {
            "selected": None, "candidates": candidates, "candidate_total": total,
            "goal_reason_codes": reason_codes, "active_leases": 0, "effective_occupancy": 0,
            "max_concurrency": 0, "lease_expires_at": None,
        }

    def claim_next_work(self, *, goal_id: str, performer_id: str, envelope_sha256: str,
                        lease_seconds: int = 300, token_reservation: int = 0,
                        repository: str, revision: str, branch: str, workspace: str,
                        work_unit_id: str | None = None,
                        lease_token: str | None = None,
                        at: str | datetime | None = None) -> dict[str, Any] | None:
        """Atomically claim work; caller-supplied tokens are never returned."""
        goal_id = _identifier(goal_id, label="goal_id")
        performer_id = _identifier(performer_id, label="performer_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        if not isinstance(token_reservation, int) or isinstance(token_reservation, bool) or token_reservation < 0:
            raise AutonomyError("token_reservation must be a non-negative integer")
        caller_supplied_token = lease_token is not None
        if caller_supplied_token and (
            not isinstance(lease_token, str) or not 32 <= len(lease_token) <= 512
        ):
            raise AutonomyError("caller-supplied lease_token must contain 32 to 512 characters")
        context = {"repository": repository, "revision": revision, "branch": branch, "workspace": workspace}
        if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_CONTEXT_CHARS for item in context.values()):
            raise AutonomyError("repository, revision, branch, and workspace must be non-empty bounded strings")
        timestamp, now = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            self._active_goal(connection, goal_id)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            try:
                selection = self._select_next_work(
                    connection, goal_id=goal_id, performer_id=performer_id,
                    envelope_sha256=envelope_sha256, lease_seconds=lease_seconds,
                    token_reservation=token_reservation, timestamp=timestamp, now=now,
                    mutate_exhausted=True, claim_filter=work_unit_id,
                )
            except StateError as error:
                raise AutonomyError(str(error)) from error
            selected = selection["selected"]
            if selected is None:
                return None
            selected = connection.execute("SELECT * FROM work_units WHERE id=?", (selected["id"],)).fetchone()
            assert selected is not None
            expiry = selection["lease_expires_at"]
            assert isinstance(expiry, str)
            attempt_id = _identifier(f"attempt-{uuid4().hex}", label="attempt_id")
            token = lease_token if caller_supplied_token else secrets.token_urlsafe(32)
            assert token is not None
            token_hash = sha256(token.encode("utf-8")).hexdigest()
            attempt_no = int(selected["attempt_count"]) + 1
            connection.execute(
                """INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,repository,revision,branch,workspace,
                    acquired_at,heartbeat_at,expires_at,status,tokens_reserved,token_accounting_source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'leased',?,'pending')""",
                (attempt_id, selected["id"], attempt_no, performer_id, attempt_no, token_hash,
                 repository, revision, branch, workspace, timestamp, timestamp, expiry, token_reservation),
            )
            changed = connection.execute(
                """UPDATE work_units SET status='leased',lease_holder=?,lease_expires_at=?,current_attempt_id=?,
                    attempt_count=?,retry_at=NULL,updated_at=? WHERE id=? AND current_attempt_id IS NULL""",
                (performer_id, expiry, attempt_id, attempt_no, timestamp, selected["id"]),
            ).rowcount
            if changed != 1:
                raise AutonomyError("work unit was claimed concurrently")
            connection.execute(
                """UPDATE budgets SET consumed_attempts=consumed_attempts+1,reserved_tokens=reserved_tokens+?,updated_at=?
                   WHERE goal_id=?""", (token_reservation, timestamp, goal_id)
            )
            self._append(connection, "work.claimed", goal_id=goal_id, work_unit_id=selected["id"],
                         payload={"attempt_id": attempt_id, "performer_id": performer_id, "attempt_no": attempt_no})
        result = {"attempt_id": attempt_id, "work_unit_id": selected["id"], "goal_id": goal_id,
                  "lease_expires_at": expiry, "attempt_no": attempt_no,
                  "work_unit": {"id": selected["id"], "title": selected["title"], "scope": json.loads(selected["scope"]), "checkpoint_id": selected["checkpoint_id"], "verification_policy": selected["verification_policy"]},
                  "context": context}
        if not caller_supplied_token:
            result["lease_token"] = token
        return result

    def explain_next_work(self, *, goal_id: str, performer_id: str, envelope_sha256: str,
                          lease_seconds: int = 300, token_reservation: int = 0,
                          limit: int = 20, offset: int = 0,
                          work_unit_id: str | None = None,
                          at: str | datetime | None = None) -> dict[str, Any]:
        """Explain the same deterministic queue decision as ``claim_next_work``.

        This is intentionally a read-only, single-snapshot operation.  It does
        not recover leases, mark exhausted units, mint a lease token, or expose
        titles, scopes, approval records, or other user-controlled payloads.
        """
        goal_id = _identifier(goal_id, label="goal_id")
        performer_id = _identifier(performer_id, label="performer_id")
        work_unit_id = _optional_identifier(work_unit_id, label="work_unit_id")
        if not isinstance(token_reservation, int) or isinstance(token_reservation, bool) or token_reservation < 0:
            raise AutonomyError("token_reservation must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AutonomyError("limit must be an integer between 1 and 100")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > 1_000_000:
            raise AutonomyError("offset must be a non-negative integer no greater than 1000000")
        # Match claim's lease validation even when the queue is currently empty.
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise AutonomyError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")
        timestamp, now = _clock(at)
        with self._readonly_connection() as connection:
            self._prepare_readonly(connection)
            goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise AutonomyError(f"Unknown goal: {goal_id}")
            if work_unit_id is not None:
                unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
                if unit is None or unit["goal_id"] != goal_id:
                    raise AutonomyError("unknown work unit for goal")

            goal_reasons: list[str] = []
            if goal["status"] == "draining":
                goal_reasons.append("goal.intake_draining")
            elif goal["status"] != "active":
                goal_reasons.append("goal.lifecycle_not_active")
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                goal_reasons.append("runtime.emergency_stopped")
            selection = self._select_next_work(
                connection, goal_id=goal_id, performer_id=performer_id,
                envelope_sha256=envelope_sha256, lease_seconds=lease_seconds,
                token_reservation=token_reservation, timestamp=timestamp, now=now,
                mutate_exhausted=False, explain=True,
                candidate_filter=work_unit_id, candidate_limit=limit,
                candidate_offset=offset,
            )
            all_goal_reasons = goal_reasons + selection["goal_reason_codes"]
            # Lifecycle and emergency gates are deliberate explanation results,
            # so neither may leave a selected unit in the preview.
            selected = selection["selected"] if not goal_reasons else None
            page = selection["candidates"]
            total = selection["candidate_total"]
            return {
                "goal_id": goal_id,
                "performer_id": performer_id,
                "evaluated_at": timestamp,
                "selected_work_unit_id": None if selected is None else selected["id"],
                "goal": {
                    "eligible": not all_goal_reasons,
                    "reason_codes": all_goal_reasons,
                    "active_leases": selection["active_leases"],
                    "effective_occupancy": selection["effective_occupancy"],
                    "max_concurrency": selection["max_concurrency"],
                },
                "candidates": [
                    {
                        "work_unit_id": entry["row"]["id"],
                        "eligible": bool(entry["eligible"] and not goal_reasons),
                        "reason_codes": entry["reason_codes"] + [
                            reason for reason in all_goal_reasons if reason not in entry["reason_codes"]
                        ],
                    }
                    for entry in page
                ],
                "total": total,
                "next_offset": offset + len(page) if offset + len(page) < total else None,
                "limit": limit,
                "offset": offset,
                "read_only": True,
                "notice": "Preview only; a claim rechecks state and requires current authorization.",
            }

    def claim_scheduled_work(self, *, invocation: Mapping[str, Any], lease_token: str,
                             at: str | datetime | None = None) -> dict[str, Any]:
        """Atomically consume one verified schedule invocation and claim its exact unit."""
        try:
            from .scheduling import _budget_reference, _canonical, _idempotency_key
            key = invocation["idempotency_key"]
            lease = invocation["lease"]
            goal_id = _identifier(invocation["goal_id"], label="goal_id")
            work_unit_id = _identifier(invocation["work_unit_id"], label="work_unit_id")
            performer_id = _identifier(lease["performer_id"], label="performer_id")
            envelope_sha256 = invocation["envelope_sha256"]
            checkpoint_id = invocation["checkpoint_id"]
            repository, revision, branch, workspace = (lease[name] for name in ("repository", "revision", "branch", "workspace"))
            lease_seconds, token_reservation = lease["requested_lease_seconds"], lease["requested_token_reservation"]
        except (KeyError, TypeError, ValueError) as error:
            raise AutonomyError("schedule invocation lacks complete claim context") from error
        if invocation.get("kind") != "tasktra.schedule-invocation" or invocation.get("authority") != "persisted-ledger-only" or key != _idempotency_key(invocation):
            raise AutonomyError("schedule invocation is not authentic")
        if not isinstance(lease_token, str) or not 32 <= len(lease_token) <= 512:
            raise AutonomyError("caller-supplied lease_token must contain 32 to 512 characters")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise AutonomyError("schedule invocation lease_seconds is invalid")
        if not isinstance(token_reservation, int) or isinstance(token_reservation, bool) or token_reservation < 0:
            raise AutonomyError("schedule invocation token_reservation is invalid")
        if any(not isinstance(value, str) or not value.strip() or len(value) > MAX_CONTEXT_CHARS for value in (repository, revision, branch, workspace)):
            raise AutonomyError("schedule invocation has invalid repository context")
        digest = sha256(_canonical(invocation).encode("utf-8")).hexdigest()
        timestamp, now = _clock(at)
        recovered: list[str] = []
        recovery_only = False
        with self._connection() as connection:
            self._prepare_write(connection)
            if connection.execute("SELECT 1 FROM schedule_resume_idempotency WHERE idempotency_key=?", (key,)).fetchone() is not None:
                raise AutonomyError("schedule invocation was already consumed")
            self._active_goal(connection, goal_id)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            unit = connection.execute("SELECT * FROM work_units WHERE id=? AND goal_id=?", (work_unit_id, goal_id)).fetchone()
            contract_row = connection.execute("SELECT contract,envelope_sha256 FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
            if unit is None or contract_row is None or budget is None or contract_row["envelope_sha256"] != envelope_sha256:
                raise AutonomyError("schedule invocation no longer binds current durable state")
            if checkpoint_id != unit["checkpoint_id"] or unit["status"] not in {"planned", "eligible", "retry-wait", "leased"}:
                raise AutonomyError("schedule invocation work unit is no longer claimable")
            reference = {"goal_id": goal_id, "total_tokens": budget["total_tokens"], "total_attempts": budget["total_attempts"], "total_elapsed_ms": budget["total_elapsed_ms"], "max_concurrency": budget["max_concurrency"], "consumed_tokens": budget["consumed_tokens"], "reserved_tokens": budget["reserved_tokens"], "remaining_tokens": None if budget["total_tokens"] is None else budget["total_tokens"] - budget["consumed_tokens"] - budget["reserved_tokens"], "consumed_attempts": budget["consumed_attempts"], "consumed_elapsed_ms": budget["consumed_elapsed_ms"], "updated_at": budget["updated_at"]}
            expected_budget = {"sha256": sha256(_canonical(reference).encode("utf-8")).hexdigest(), "snapshot": reference}
            if invocation.get("budget_reference") != expected_budget:
                raise AutonomyError("schedule invocation budget reference is stale")
            if not self._work_prerequisite_state_in_transaction(connection, work_unit_id)["ready"]:
                raise AutonomyError("work unit prerequisites are incomplete")
            expired_attempts = connection.execute(
                """SELECT a.*,u.goal_id FROM work_attempts a
                   JOIN work_units u ON u.id=a.work_unit_id
                   WHERE u.goal_id=? AND a.status='leased' AND a.expires_at<=?
                   ORDER BY a.id""",
                (goal_id, timestamp),
            ).fetchall()
            for attempt in expired_attempts:
                attempt_budget = connection.execute(
                    "SELECT * FROM budgets WHERE goal_id=?", (goal_id,),
                ).fetchone()
                elapsed = min(self._attempt_elapsed(attempt, now), self._lease_reservation_ms(attempt))
                terminal = "exhausted" if (
                    attempt_budget["consumed_attempts"] >= attempt_budget["total_attempts"]
                    or (
                        attempt_budget["total_elapsed_ms"] is not None
                        and attempt_budget["consumed_elapsed_ms"] + elapsed >= attempt_budget["total_elapsed_ms"]
                    )
                ) else "retry"
                if connection.execute("SELECT 1 FROM codex_run_preparations WHERE attempt_id=? LIMIT 1", (attempt["id"],)).fetchone() is not None:
                    terminal = "blocked"
                recovery_outcome = {"recovered": True}
                if terminal == "blocked":
                    recovery_outcome["reason"] = "host-execution-requires-review"
                    # Prepared host work needs a durable review block before any
                    # subsequent claim rejection can unwind this recovery.
                    recovery_only = True
                recovery_evidence: dict[str, Any] = recovery_outcome
                if int(attempt["tokens_reserved"]):
                    recovery_evidence["unmeasured_usage"] = unmeasured_usage_evidence(
                        charged_tokens=int(attempt["tokens_reserved"]), reason="lease-expired",
                    )
                connection.execute(
                    """UPDATE work_attempts SET status='expired',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=?,
                       tokens_consumed=tokens_consumed+?,token_accounting_source='unavailable' WHERE id=?""",
                    (terminal, _encode(recovery_evidence), timestamp, elapsed, attempt["tokens_reserved"], attempt["id"]),
                )
                connection.execute(
                    """UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_tokens=consumed_tokens+?,
                       consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?""",
                    (attempt["tokens_reserved"], attempt["tokens_reserved"], elapsed, timestamp, goal_id),
                )
                connection.execute(
                    "UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,retry_at=?,last_outcome_class=?,updated_at=? WHERE id=? AND current_attempt_id=?",
                    (
                        "retry-wait" if terminal == "retry" else terminal,
                        timestamp if terminal == "retry" else None,
                        terminal,
                        timestamp,
                        attempt["work_unit_id"],
                        attempt["id"],
                    ),
                )
                self._append(
                    connection,
                    "work.lease_recovered",
                    goal_id=goal_id,
                    work_unit_id=attempt["work_unit_id"],
                    payload={"attempt_id": attempt["id"], "outcome": terminal},
                )
                recovered.append(attempt["id"])
            if recovery_only:
                # Persist the host-execution review block before evaluating any
                # subsequent claim rejection. Ordinary lease recovery retains the
                # existing atomic recover-and-claim behavior below.
                pass
            else:
                unit = connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
                budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
                if unit["current_attempt_id"] is not None or unit["status"] not in {"planned", "eligible", "retry-wait"}:
                    if unit["current_attempt_id"] is not None:
                        raise AutonomyError("schedule invocation work unit has an active lease")
                    raise AutonomyError("schedule invocation work unit is no longer claimable")
                contract = load_authority_envelope(contract_row["contract"])
                self._dependencies_complete_in_transaction(connection, goal_id)
                checkpoints = self._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
                next_checkpoint = next((row["checkpoint_id"] for row in checkpoints if row["status"] != "reached"), None)
                if (contract["checkpoints"] and next_checkpoint != checkpoint_id) or (not contract["checkpoints"] and checkpoint_id is not None):
                    raise AutonomyError("schedule invocation checkpoint is not eligible")
                active = connection.execute("SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'", (goal_id,)).fetchone()[0]
                detached = connection.execute(
                    """SELECT count(*) FROM codex_run_preparations p JOIN work_attempts a ON a.id=p.attempt_id
                       JOIN work_units u ON u.id=a.work_unit_id LEFT JOIN codex_run_finishes f ON f.run_id=p.id
                       WHERE u.goal_id=? AND a.status!='leased' AND f.run_id IS NULL""", (goal_id,)
                ).fetchone()[0]
                if budget["max_concurrency"] is None or budget["total_attempts"] is None:
                    raise AutonomyError("execution budget requires max_concurrency and total_attempts")
                if int(active) + int(detached) >= budget["max_concurrency"] or budget["consumed_attempts"] >= budget["total_attempts"]:
                    raise AutonomyError("schedule invocation budget is exhausted")
                if int(unit["attempt_count"]) >= budget["total_attempts"]:
                    raise AutonomyError("schedule invocation work unit exhausted its attempt allowance")
                if budget["total_tokens"] is not None and budget["consumed_tokens"] + budget["reserved_tokens"] + token_reservation > budget["total_tokens"]:
                    raise AutonomyError("token budget would be exceeded")
                remaining = None if budget["total_elapsed_ms"] is None else budget["total_elapsed_ms"] - budget["consumed_elapsed_ms"] - self._live_reservation_ms(connection, goal_id)
                expiry, _ = self._lease_expiry(now, lease_seconds, remaining)
                self._authorize(connection, goal_id=goal_id, work_unit_id=work_unit_id, action=WORK_CLAIM_ACTION, envelope_sha256=envelope_sha256, performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp)
                attempt_id = _identifier(f"attempt-{uuid4().hex}", label="attempt_id")
                attempt_no = int(unit["attempt_count"]) + 1
                connection.execute("INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,repository,revision,branch,workspace,acquired_at,heartbeat_at,expires_at,status,tokens_reserved,token_accounting_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'leased',?,'pending')", (attempt_id, work_unit_id, attempt_no, performer_id, attempt_no, sha256(lease_token.encode("utf-8")).hexdigest(), repository, revision, branch, workspace, timestamp, timestamp, expiry, token_reservation))
                if connection.execute("UPDATE work_units SET status='leased',lease_holder=?,lease_expires_at=?,current_attempt_id=?,attempt_count=?,retry_at=NULL,updated_at=? WHERE id=? AND current_attempt_id IS NULL", (performer_id, expiry, attempt_id, attempt_no, timestamp, work_unit_id)).rowcount != 1:
                    raise AutonomyError("work unit was claimed concurrently")
                connection.execute("UPDATE budgets SET consumed_attempts=consumed_attempts+1,reserved_tokens=reserved_tokens+?,updated_at=? WHERE goal_id=?", (token_reservation, timestamp, goal_id))
                connection.execute("INSERT INTO schedule_resume_idempotency VALUES(?,?,?, ?,'consumed',?,?)", (key, digest, goal_id, work_unit_id, timestamp, attempt_id))
                self._append(connection, "schedule.resume_consumed", goal_id=goal_id, work_unit_id=work_unit_id, payload={"idempotency_key": key, "attempt_id": attempt_id})
        if recovery_only:
            return self.claim_scheduled_work(invocation=invocation, lease_token=lease_token, at=at)
        return {"attempt_id": attempt_id, "work_unit_id": work_unit_id, "goal_id": goal_id,
                "lease_expires_at": expiry, "attempt_no": attempt_no, "recovered_attempts": recovered,
                "context": {"repository": repository, "revision": revision, "branch": branch, "workspace": workspace}}
    def heartbeat(self, *, attempt_id: str, performer_id: str, lease_token: str,
                  lease_seconds: int = 300, at: str | datetime | None = None) -> dict[str, Any]:
        attempt_id = _identifier(attempt_id, label="attempt_id")
        performer_id = _identifier(performer_id, label="performer_id")
        timestamp, now = _clock(at)
        supplied_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute(
                """SELECT a.*,u.goal_id,u.current_attempt_id FROM work_attempts a
                   JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""", (attempt_id,)
            ).fetchone()
            if row is None or row["status"] != "leased" or row["current_attempt_id"] != attempt_id:
                raise AutonomyError("attempt is stale or no longer current")
            if row["owner_id"] != performer_id or not hmac.compare_digest(row["lease_token_hash"], supplied_hash):
                raise AutonomyError("attempt owner or lease token does not match")
            if timestamp >= row["expires_at"]:
                raise AutonomyError("lease is expired")
            self._active_goal(connection, row["goal_id"], allow_draining=True)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (row["goal_id"],)).fetchone()
            other_reservation = self._live_reservation_ms(connection, row["goal_id"], excluding_attempt_id=attempt_id)
            available_elapsed = None if budget["total_elapsed_ms"] is None else (
                int(budget["total_elapsed_ms"]) - int(budget["consumed_elapsed_ms"]) - other_reservation
            )
            expiry, deadline = self._lease_expiry(now, lease_seconds, available_elapsed)
            acquired = datetime.fromisoformat(row["acquired_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            if budget["total_elapsed_ms"] is not None:
                cap = acquired + timedelta(milliseconds=max(0, available_elapsed or 0))
                if cap <= now:
                    raise AutonomyError("elapsed budget is exhausted")
                deadline = min(deadline, cap)
                expiry = _iso(deadline)
            connection.execute("UPDATE work_attempts SET heartbeat_at=?,expires_at=? WHERE id=?", (timestamp, expiry, attempt_id))
            connection.execute("UPDATE work_units SET lease_expires_at=?,updated_at=? WHERE id=?", (expiry, timestamp, row["work_unit_id"]))
            self._append(connection, "work.heartbeat", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], payload={"attempt_id": attempt_id})
        return {"attempt_id": attempt_id, "lease_expires_at": expiry}

    def validate_attempt(self, *, attempt_id: str, performer_id: str, lease_token: str,
                         envelope_sha256: str | None = None,
                         at: str | datetime | None = None) -> dict[str, Any]:
        """Prove a host is still bound to its exact, live claim before a stage.

        The result contains no lease secret and includes the immutable work-unit
        selector and verification policy used by a host supervisor.
        """
        attempt_id = _identifier(attempt_id, label="attempt_id")
        performer_id = _identifier(performer_id, label="performer_id")
        timestamp, _ = _clock(at)
        supplied_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        with self._connection(write=False) as connection:
            self._prepare_readonly(connection)
            row = connection.execute(
                """SELECT a.*,u.goal_id,u.current_attempt_id,u.status AS work_unit_status,u.scope,u.checkpoint_id,u.verification_policy
                   FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""",
                (attempt_id,),
            ).fetchone()
            if row is None or row["status"] != "leased" or row["current_attempt_id"] != attempt_id:
                raise AutonomyError("attempt is stale or no longer current")
            if row["owner_id"] != performer_id or not hmac.compare_digest(row["lease_token_hash"], supplied_hash):
                raise AutonomyError("attempt owner or lease token does not match")
            if timestamp >= row["expires_at"]:
                raise AutonomyError("lease is expired; recover it instead")
            self._active_goal(connection, row["goal_id"], allow_draining=True)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            if envelope_sha256 is not None:
                # A host calls this between stages.  This rechecks both the
                # immutable current envelope and the approval's expiry and
                # revocation state, so a supervisor never relies on a claim
                # ceremony that was later withdrawn.
                self._authorize(
                    connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                    action=WORK_CLAIM_ACTION, envelope_sha256=envelope_sha256,
                    performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE,
                    timestamp=timestamp,
                )
            return {
                "attempt_id": attempt_id, "goal_id": row["goal_id"], "work_unit_id": row["work_unit_id"],
                "verification_policy": row["verification_policy"], "checkpoint_id": row["checkpoint_id"],
                "scope": json.loads(row["scope"]), "lease_expires_at": row["expires_at"],
            }

    def goal_run_health(self, goal_id: str) -> dict[str, Any]:
        """Stable host-facing alias for the derived, non-lifecycle health view."""
        return self.goal_execution_health(goal_id)

    @staticmethod
    def _validate_completion(goal_id: str, work_unit_id: str, verification_policy: str,
                             workflow: Mapping[str, Any], token: Mapping[str, Any] | None) -> tuple[str, str]:
        try:
            completion = workflow_completion_token(workflow) if token is None else validate_workflow_completion_token(workflow, token)
            if completion["source"] != {"goal_id": goal_id, "work_unit_id": work_unit_id}:
                raise AutonomyError("workflow completion belongs to a different goal or work unit")
            workflow_policy = "implementation-review" if workflow.get("version") == 1 else workflow.get("verification_policy")
            if workflow_policy != verification_policy:
                raise AutonomyError("workflow verification policy does not match the immutable work unit policy")
            serialized = serialize_workflow(workflow)
        except WorkflowError as error:
            raise AutonomyError(f"success requires a complete Stage 2 workflow: {error}") from error
        return serialized, _encode(completion)

    @staticmethod
    def _store_workflow(connection: Any, *, goal_id: str, work_unit_id: str,
                        workflow_json: str, completion_json: str,
                        completion_evidence_json: str | None, timestamp: str) -> None:
        digest = sha256(workflow_json.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT workflow_sha256,completion_evidence_json FROM workflow_evidence WHERE work_unit_id=?",
            (work_unit_id,),
        ).fetchone()
        if existing is not None and existing["workflow_sha256"] != digest:
            raise AutonomyError("work unit already has different workflow evidence")
        if existing is not None and existing["completion_evidence_json"] != completion_evidence_json:
            raise AutonomyError("work unit already has different completion evidence")
        connection.execute(
            """INSERT INTO workflow_evidence(work_unit_id,workflow_json,workflow_sha256,completion_token_json,completion_evidence_json,recorded_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(work_unit_id) DO NOTHING""",
            (work_unit_id, workflow_json, digest, completion_json, completion_evidence_json, timestamp),
        )

    def record_workflow_completion(self, *, goal_id: str, work_unit_id: str, workflow: Mapping[str, Any],
                                   completion_token: Mapping[str, Any] | None = None,
                                   completion_evidence: Mapping[str, Any] | None = None,
                                   at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        unit_policy = self.get_work_unit(work_unit_id)
        if unit_policy is None:
            raise AutonomyError("unknown work unit for goal")
        workflow_json, token_json = self._validate_completion(goal_id, work_unit_id, unit_policy["verification_policy"], workflow, completion_token)
        evidence_json: str | None = None
        if unit_policy["verification_policy"] == "implementation-deterministic-review":
            try:
                proof = validate_deterministic_review_completion_evidence(
                    workflow, unit_policy["acceptance_checks"], completion_evidence,
                )
            except StateError as error:
                raise AutonomyError(str(error)) from error
            evidence_json = _encode(proof)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None or unit["goal_id"] != goal_id:
                raise AutonomyError("unknown work unit for goal")
            self._store_workflow(connection, goal_id=goal_id, work_unit_id=work_unit_id,
                                 workflow_json=workflow_json, completion_json=token_json,
                                 completion_evidence_json=evidence_json, timestamp=timestamp)
            self._append(connection, "workflow.completed", goal_id=goal_id, work_unit_id=work_unit_id, payload={"workflow_sha256": sha256(workflow_json.encode()).hexdigest()})
        return {"work_unit_id": work_unit_id, "workflow_sha256": sha256(workflow_json.encode()).hexdigest()}

    @staticmethod
    def _reach_checkpoint_if_ready(
        connection: Any, *, goal_id: str, work_unit_id: str, checkpoint_id: str | None,
        workflow_json: str, outcome_json: str, timestamp: str,
    ) -> str | None:
        """Atomically reach one evidence checkpoint after all of its units finish.

        The compact evidence binds the gate to the complete ordered set of
        units and the triggering workflow without placing an unbounded work
        list in the ledger row.
        """
        if checkpoint_id is None:
            return None
        contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        if contract_row is None:
            raise AutonomyError("goal lacks an authority envelope")
        try:
            contract = load_authority_envelope(contract_row["contract"])
            checkpoints = StateStore._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
        except StateError as error:
            raise AutonomyError(str(error)) from error
        checkpoint = next((row for row in checkpoints if row["checkpoint_id"] == checkpoint_id), None)
        if checkpoint is None:
            raise AutonomyError("work unit checkpoint is not present in the authority envelope")
        if checkpoint["status"] == "reached":
            raise AutonomyError("work completed after its checkpoint was already reached")
        incomplete = connection.execute(
            "SELECT count(*) FROM work_units WHERE goal_id=? AND checkpoint_id=? AND status!='complete'",
            (goal_id, checkpoint_id),
        ).fetchone()[0]
        if incomplete:
            return None
        unit_digest = sha256()
        unit_count = 0
        for unit in connection.execute(
            "SELECT id FROM work_units WHERE goal_id=? AND checkpoint_id=? ORDER BY id",
            (goal_id, checkpoint_id),
        ):
            unit_digest.update(unit["id"].encode("utf-8"))
            unit_digest.update(b"\0")
            unit_count += 1
        evidence_json, _ = _json_hash({
            "checkpoint_id": checkpoint_id,
            "trigger_work_unit_id": work_unit_id,
            "work_unit_count": unit_count,
            "work_units_sha256": unit_digest.hexdigest(),
            "workflow_sha256": sha256(workflow_json.encode("utf-8")).hexdigest(),
            "outcome_evidence_sha256": sha256(outcome_json.encode("utf-8")).hexdigest(),
        })
        changed = connection.execute(
            "UPDATE goal_checkpoints SET status='reached',evidence_json=?,reached_at=? WHERE goal_id=? AND checkpoint_id=? AND status='pending'",
            (evidence_json, timestamp, goal_id, checkpoint_id),
        ).rowcount
        if changed != 1:
            raise AutonomyError("checkpoint was concurrently changed")
        updated = connection.execute(
            "SELECT * FROM goal_checkpoints WHERE goal_id=? AND checkpoint_id=?", (goal_id, checkpoint_id)
        ).fetchone()
        StateStore._seal_authority_row_in_transaction(
            connection, "goal_checkpoints", StateStore._checkpoint_seal_id(goal_id, checkpoint_id), updated, timestamp
        )
        AutonomyStore._append(
            connection, "goal.checkpoint_reached", goal_id=goal_id, work_unit_id=work_unit_id,
            payload={"checkpoint_id": checkpoint_id, "work_unit_count": unit_count},
        )
        return checkpoint_id

    def finish_attempt(self, *, attempt_id: str, performer_id: str, lease_token: str, outcome: str,
                       tokens_consumed: int | None = None, accounting_source: str | None = None, elapsed_ms: int | None = None,
                       outcome_evidence: Mapping[str, Any] | None = None,
                       observed_token_overrun: bool = False,
                       observed_usage_evidence: Mapping[str, Any] | None = None,
                       workflow: Mapping[str, Any] | None = None, completion_token: Mapping[str, Any] | None = None,
                       at: str | datetime | None = None) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise AutonomyError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")
        attempt_id = _identifier(attempt_id, label="attempt_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(observed_token_overrun, bool):
            raise AutonomyError("observed_token_overrun must be a boolean")
        timestamp, now = _clock(at)
        supplied_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("""SELECT a.*,u.goal_id,u.current_attempt_id,u.attempt_count,u.checkpoint_id,u.verification_policy,u.acceptance_checks FROM work_attempts a
                                      JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""", (attempt_id,)).fetchone()
            if row is None or row["status"] != "leased" or row["current_attempt_id"] != attempt_id:
                raise AutonomyError("attempt is stale or no longer current")
            if row["owner_id"] != performer_id or not hmac.compare_digest(row["lease_token_hash"], supplied_hash):
                raise AutonomyError("attempt owner or lease token does not match")
            if timestamp >= row["expires_at"]:
                raise AutonomyError("lease is expired; recover it instead")
            self._active_goal(connection, row["goal_id"], allow_draining=True)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (row["goal_id"],)).fetchone()
            measured = self._attempt_elapsed(row, now)
            measured_elapsed = measured if elapsed_ms is None else elapsed_ms
            if not isinstance(measured_elapsed, int) or isinstance(measured_elapsed, bool) or measured_elapsed < 0:
                raise AutonomyError("elapsed_ms must be a non-negative integer")
            if measured_elapsed < measured:
                raise AutonomyError("elapsed_ms cannot underreport elapsed execution time")
            if measured_elapsed > self._lease_reservation_ms(row):
                raise AutonomyError("elapsed_ms exceeds this attempt's reserved lease budget")
            if outcome_evidence is None:
                outcome_evidence = {}
            if not isinstance(outcome_evidence, Mapping):
                raise AutonomyError("outcome_evidence must be an object")
            outcome_json, _ = _json_hash(outcome_evidence)
            tokens_consumed, resolved_accounting_source = self._codex_accounting_in_transaction(
                connection, attempt_id=attempt_id, tokens_consumed=tokens_consumed, accounting_source=accounting_source
            )
            if outcome in {"success", "transient"} and connection.execute(
                """SELECT 1 FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id
                   WHERE p.attempt_id=? AND f.run_id IS NULL LIMIT 1""", (attempt_id,)
            ).fetchone() is not None:
                raise AutonomyError("work completion or automatic retry requires all Codex runs to be resolved")
            if tokens_consumed > row["tokens_reserved"] and not observed_token_overrun:
                raise AutonomyError("tokens_consumed exceeds the reservation")
            if observed_token_overrun:
                # This is an exceptional accounting settlement for a
                # coordinator-attested usage total.  It cannot complete work
                # or grant new budget; it releases the lease as exhausted.
                if tokens_consumed <= row["tokens_reserved"]:
                    raise AutonomyError("observed token overrun must exceed the reservation")
                if outcome != "exhausted" or workflow is not None or completion_token is not None:
                    raise AutonomyError("observed token overrun must settle an exhausted attempt without workflow")
                if "observed_usage" in outcome_evidence:
                    raise AutonomyError("outcome_evidence may not supply observed_usage")
                try:
                    observed_usage = validate_observed_usage_evidence(
                        observed_usage_evidence, tokens_consumed=tokens_consumed,
                    )
                except StateError as error:
                    raise AutonomyError(str(error)) from error
                outcome_evidence = {**dict(outcome_evidence), "observed_usage": observed_usage}
            elif observed_usage_evidence is not None:
                raise AutonomyError("observed_usage_evidence requires observed_token_overrun")
            outcome_json, _ = _json_hash(outcome_evidence)
            terminal = outcome
            workflow_json = token_json = None
            if outcome == "success":
                if workflow is None:
                    raise AutonomyError("success requires a complete Stage 2 workflow")
                self._authorize(connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], action=WORK_COMPLETE_ACTION,
                                envelope_sha256=connection.execute("SELECT envelope_sha256 FROM goal_contracts WHERE goal_id=?", (row["goal_id"],)).fetchone()[0],
                                performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp)
                workflow_json, token_json = self._validate_completion(row["goal_id"], row["work_unit_id"], row["verification_policy"], workflow, completion_token)
                completion_evidence_json = None
                if row["verification_policy"] == "implementation-deterministic-review":
                    try:
                        proof = validate_deterministic_review_completion_evidence(
                            workflow, _decode(row["acceptance_checks"], None), outcome_evidence,
                        )
                    except StateError as error:
                        raise AutonomyError(str(error)) from error
                    completion_evidence_json = _encode(proof)
            else:
                completion_evidence_json = None
            other_reservation = self._live_reservation_ms(connection, row["goal_id"], excluding_attempt_id=attempt_id)
            if (budget["total_elapsed_ms"] is not None and budget["consumed_elapsed_ms"] + other_reservation + measured_elapsed > budget["total_elapsed_ms"]):
                terminal = "exhausted"
            if outcome == "transient" and terminal != "exhausted":
                terminal = "exhausted" if budget["consumed_attempts"] >= budget["total_attempts"] else "retry"
            elif outcome == "permanent": terminal = "failed"
            connection.execute("UPDATE work_attempts SET status='finished',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=?,tokens_consumed=?,token_accounting_source=? WHERE id=?", (terminal, outcome_json, timestamp, measured_elapsed, tokens_consumed, resolved_accounting_source, attempt_id))
            connection.execute("""UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_tokens=consumed_tokens+?,
                                consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?""",
                               (row["tokens_reserved"], tokens_consumed, measured_elapsed, timestamp, row["goal_id"]))
            connection.execute("""UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,
                                retry_at=?,last_outcome_class=?,updated_at=? WHERE id=?""",
                               ("complete" if terminal == "success" else "retry-wait" if terminal == "retry" else terminal,
                                timestamp if terminal == "retry" else None, terminal, timestamp, row["work_unit_id"]))
            if terminal == "success":
                self._store_workflow(
                    connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                    workflow_json=workflow_json or "", completion_json=token_json or "",
                    completion_evidence_json=completion_evidence_json, timestamp=timestamp,
                )
                self._reach_checkpoint_if_ready(
                    connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                    checkpoint_id=row["checkpoint_id"], workflow_json=workflow_json or "",
                    outcome_json=outcome_json, timestamp=timestamp,
                )
            self._append(connection, "work.finished", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], payload={"attempt_id": attempt_id, "outcome": terminal})
            StateStore._finalize_drain_if_empty_in_transaction(
                connection, row["goal_id"], timestamp=timestamp, actor_id=performer_id,
            )
        return {"attempt_id": attempt_id, "work_unit_id": row["work_unit_id"], "outcome": terminal, "token_accounting_source": resolved_accounting_source, "tokens_consumed": tokens_consumed}

    def yield_for_intervention(
        self, *, attempt_id: str, performer_id: str, lease_token: str, request: Mapping[str, Any],
        tokens_consumed: int | None = None, accounting_source: str | None = None, elapsed_ms: int | None = None, at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically persist one bounded intervention and relinquish its lease."""
        attempt_id = _identifier(attempt_id, label="attempt_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if elapsed_ms is not None and (not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool) or elapsed_ms < 0):
            raise AutonomyError("elapsed_ms must be a non-negative integer")
        try:
            accepted = validate_intervention_request(request)
            request_json = canonical_intervention_request(accepted)
            request_sha256 = intervention_request_sha256(accepted)
        except InterventionError as error:
            raise AutonomyError(f"intervention request is invalid: {error}") from error
        if accepted["source"]["attempt_id"] != attempt_id or accepted["producer"]["actor_id"] != performer_id:
            raise AutonomyError("intervention request source or producer does not match yield")
        if isinstance(lease_token, str) and _contains_supplied_lease_token(accepted, lease_token):
            raise AutonomyError("intervention request must not contain the supplied lease token")
        timestamp, now = _clock(at)
        supplied_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        input_mode = "explicit" if elapsed_ms is not None else "measured"
        with self._connection() as connection:
            self._prepare_write(connection)
            tokens_consumed, resolved_accounting_source = self._codex_accounting_in_transaction(
                connection, attempt_id=attempt_id, tokens_consumed=tokens_consumed, accounting_source=accounting_source
            )
            prior = connection.execute(
                """SELECT r.*,a.owner_id,a.lease_token_hash,a.token_accounting_source,u.current_attempt_id,u.current_intervention_id
                   FROM intervention_requests r JOIN work_attempts a ON a.id=r.attempt_id
                   JOIN work_units u ON u.id=r.work_unit_id WHERE r.attempt_id=?""", (attempt_id,)
            ).fetchone()
            if prior is not None:
                exact = (
                    prior["owner_id"] == performer_id and hmac.compare_digest(prior["lease_token_hash"], supplied_hash)
                    and prior["request_sha256"] == request_sha256 and prior["request_json"] == request_json
                    and prior["yield_tokens_consumed"] == tokens_consumed and prior["yield_elapsed_input_mode"] == input_mode
                    and prior["yield_elapsed_input_ms"] == elapsed_ms and prior["token_accounting_source"] == resolved_accounting_source
                )
                if not exact:
                    raise AutonomyError("idempotency_conflict")
                return {
                    "request_id": prior["id"], "request_sha256": prior["request_sha256"],
                    "goal_id": prior["goal_id"], "work_unit_id": prior["work_unit_id"], "attempt_id": attempt_id,
                    "status": prior["outcome_class"], "tokens_consumed": prior["yield_tokens_consumed"],
                    "elapsed_input_mode": prior["yield_elapsed_input_mode"],
                    "elapsed_input_ms": prior["yield_elapsed_input_ms"],
                    "accounted_elapsed_ms": prior["yield_accounted_elapsed_ms"], "mutation": "none", "idempotent": True,
                    "token_accounting_source": prior["token_accounting_source"],
                    "current": prior["current_intervention_id"] == prior["id"], "current_attempt_id": prior["current_attempt_id"],
                    "current_intervention_id": prior["current_intervention_id"],
                    "stale_reason": None if prior["current_intervention_id"] == prior["id"] else "unit_advanced",
                }
            identity_conflict = connection.execute(
                "SELECT id FROM intervention_requests WHERE id=? OR request_sha256=?", (accepted["request_id"], request_sha256)
            ).fetchone()
            if identity_conflict is not None:
                raise AutonomyError("idempotency_conflict")
            row = connection.execute(
                """SELECT a.*,u.goal_id,u.current_attempt_id,u.current_intervention_id,u.attempt_count,u.checkpoint_id FROM work_attempts a
                   JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""", (attempt_id,)
            ).fetchone()
            if row is None or row["status"] != "leased" or row["current_attempt_id"] != attempt_id:
                raise AutonomyError("attempt is stale or no longer current")
            if row["owner_id"] != performer_id or not hmac.compare_digest(row["lease_token_hash"], supplied_hash):
                raise AutonomyError("attempt owner or lease token does not match")
            if timestamp >= row["expires_at"]:
                raise AutonomyError("lease is expired; recover it instead")
            if row["current_intervention_id"] is not None:
                raise AutonomyError("work unit already has a current intervention")
            if accepted["source"] != {"goal_id": row["goal_id"], "work_unit_id": row["work_unit_id"], "attempt_id": attempt_id}:
                raise AutonomyError("intervention request source does not match the leased attempt")
            self._active_goal(connection, row["goal_id"], allow_draining=True)
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            measured = self._attempt_elapsed(row, now)
            accounted_elapsed = measured if elapsed_ms is None else elapsed_ms
            if accounted_elapsed < measured:
                raise AutonomyError("elapsed_ms cannot underreport elapsed execution time")
            if accounted_elapsed > self._lease_reservation_ms(row):
                raise AutonomyError("elapsed_ms exceeds this attempt's reserved lease budget")
            if tokens_consumed > row["tokens_reserved"]:
                raise AutonomyError("tokens_consumed exceeds the reservation")
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (row["goal_id"],)).fetchone()
            other_reservation = self._live_reservation_ms(connection, row["goal_id"], excluding_attempt_id=attempt_id)
            if budget["total_elapsed_ms"] is not None and budget["consumed_elapsed_ms"] + other_reservation + accounted_elapsed > budget["total_elapsed_ms"]:
                raise AutonomyError("yield accounting would exhaust the work unit; finish it as exhausted")
            outcome_json = _encode({
                "intervention_request_id": accepted["request_id"], "request_sha256": request_sha256,
                "tokens_consumed": tokens_consumed, "elapsed_input_mode": input_mode,
                "elapsed_input_ms": elapsed_ms, "accounted_elapsed_ms": accounted_elapsed,
            })
            connection.execute(
                """INSERT INTO intervention_requests(
                    id,version,goal_id,work_unit_id,attempt_id,producer_id,outcome_class,request_json,request_sha256,
                    yield_tokens_consumed,yield_elapsed_input_mode,yield_elapsed_input_ms,yield_accounted_elapsed_ms,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (accepted["request_id"], accepted["version"], row["goal_id"], row["work_unit_id"], attempt_id,
                 performer_id, accepted["outcome_class"], request_json, request_sha256, tokens_consumed,
                 input_mode, elapsed_ms, accounted_elapsed, timestamp),
            )
            connection.execute(
                "UPDATE work_attempts SET status='finished',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=?,tokens_consumed=?,token_accounting_source=? WHERE id=?",
                (accepted["outcome_class"], outcome_json, timestamp, accounted_elapsed, tokens_consumed, resolved_accounting_source, attempt_id),
            )
            connection.execute(
                """UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_tokens=consumed_tokens+?,
                   consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?""",
                (row["tokens_reserved"], tokens_consumed, accounted_elapsed, timestamp, row["goal_id"]),
            )
            connection.execute(
                """UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,
                   current_intervention_id=?,retry_at=NULL,last_outcome_class=?,updated_at=? WHERE id=?""",
                (accepted["outcome_class"], accepted["request_id"], accepted["outcome_class"], timestamp, row["work_unit_id"]),
            )
            self._append(
                connection, "intervention.requested", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                payload={"request_id": accepted["request_id"], "request_sha256": request_sha256, "attempt_id": attempt_id,
                         "producer_id": performer_id, "outcome_class": accepted["outcome_class"],
                         "requires_human_approval": accepted["requires_human_approval"], "timestamp": timestamp},
            )
            self._append(
                connection, "work.finished", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                payload={"attempt_id": attempt_id, "outcome": accepted["outcome_class"], "request_id": accepted["request_id"], "request_sha256": request_sha256, "timestamp": timestamp},
            )
            drain_finalized = StateStore._finalize_drain_if_empty_in_transaction(
                connection, row["goal_id"], timestamp=timestamp, actor_id=performer_id,
            )
        return {
            "request_id": accepted["request_id"], "request_sha256": request_sha256, "goal_id": row["goal_id"],
            "work_unit_id": row["work_unit_id"], "attempt_id": attempt_id, "status": accepted["outcome_class"],
            "tokens_consumed": tokens_consumed, "elapsed_input_mode": input_mode, "elapsed_input_ms": elapsed_ms,
            "accounted_elapsed_ms": accounted_elapsed, "token_accounting_source": resolved_accounting_source, "drain_finalized": drain_finalized,
            "mutation": "applied", "idempotent": False, "current": True,
        }

    def record_intervention_response(
        self, *, response: Mapping[str, Any], responder_id: str, responder_kind: str,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        responder_id = _identifier(responder_id, label="responder_id")
        if responder_kind not in {"human", "steward"}:
            raise AutonomyError("responder_kind must be human or steward")
        try:
            accepted = validate_intervention_response(response)
            response_json = canonical_intervention_response(accepted)
            response_sha256 = intervention_response_sha256(accepted)
        except InterventionError as error:
            raise AutonomyError(f"intervention response is invalid: {error}") from error
        if accepted["responder"] != {"kind": responder_kind, "actor_id": responder_id}:
            raise AutonomyError("intervention response responder does not match caller")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            source_attempt = connection.execute(
                """SELECT a.lease_token_hash FROM intervention_requests r
                   JOIN work_attempts a ON a.id=r.attempt_id WHERE r.id=?""",
                (accepted["request"]["request_id"],),
            ).fetchone()
            if source_attempt is not None and _contains_persisted_lease_token(accepted, source_attempt["lease_token_hash"]):
                raise AutonomyError("intervention response must not contain a lease token")
            existing = connection.execute("SELECT * FROM intervention_responses WHERE id=?", (accepted["response_id"],)).fetchone()
            if existing is not None:
                if existing["response_json"] != response_json or existing["response_sha256"] != response_sha256:
                    raise AutonomyError("idempotency_conflict")
                request = connection.execute(
                    """SELECT r.*,u.current_intervention_id,u.current_attempt_id FROM intervention_requests r
                       JOIN work_units u ON u.id=r.work_unit_id WHERE r.id=?""", (existing["request_id"],)
                ).fetchone()
                head = connection.execute("SELECT * FROM intervention_response_heads WHERE request_id=?", (existing["request_id"],)).fetchone()
                return {
                    "request_id": existing["request_id"], "request_sha256": existing["request_sha256"],
                    "response_id": existing["id"], "response_sha256": existing["response_sha256"],
                    "revision_no": existing["revision_no"], "mutation": "none", "idempotent": True,
                    "current": (
                        request is not None and request["current_intervention_id"] == existing["request_id"]
                        and head is not None and head["current_response_id"] == existing["id"]
                    ),
                    "current_response_id": None if head is None else head["current_response_id"],
                    "current_response_sha256": None if head is None else head["current_response_sha256"],
                    "current_attempt_id": None if request is None else request["current_attempt_id"],
                    "current_intervention_id": None if request is None else request["current_intervention_id"],
                    "stale_reason": None if (
                        request is not None and request["current_intervention_id"] == existing["request_id"]
                        and head is not None and head["current_response_id"] == existing["id"]
                    ) else "unit_advanced",
                }
            request = connection.execute(
                """SELECT r.*,u.status,u.current_intervention_id FROM intervention_requests r
                   JOIN work_units u ON u.id=r.work_unit_id WHERE r.id=?""", (accepted["request"]["request_id"],)
            ).fetchone()
            if request is None or request["request_sha256"] != accepted["request"]["request_sha256"]:
                raise AutonomyError("intervention request is missing or digest-mismatched")
            if request["current_intervention_id"] != request["id"] or request["status"] != request["outcome_class"]:
                raise AutonomyError("intervention request is no longer current")
            if connection.execute("SELECT 1 FROM intervention_closures WHERE request_id=?", (request["id"],)).fetchone() is not None:
                raise AutonomyError("intervention request is closed")
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            if request["outcome_class"] == "approval-required" and responder_kind != "human":
                raise AutonomyError("approval-required intervention responses require a human responder")
            head = connection.execute("SELECT * FROM intervention_response_heads WHERE request_id=?", (request["id"],)).fetchone()
            expected = accepted["expected_current_response"]
            if head is None:
                if expected is not None:
                    raise _response_head_changed(None)
                revision_no, previous_id, previous_sha256 = 1, None, None
            else:
                if expected is None or expected["response_id"] != head["current_response_id"] or expected["response_sha256"] != head["current_response_sha256"]:
                    raise _response_head_changed(head)
                revision_no = int(head["revision_no"]) + 1
                previous_id, previous_sha256 = head["current_response_id"], head["current_response_sha256"]
            connection.execute(
                """INSERT INTO intervention_responses(
                    id,version,request_id,request_sha256,revision_no,previous_response_id,expected_previous_sha256,
                    responder_kind,responder_id,disposition,response_json,response_sha256,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (accepted["response_id"], accepted["version"], request["id"], request["request_sha256"], revision_no,
                 previous_id, previous_sha256, responder_kind, responder_id, accepted["disposition"], response_json,
                 response_sha256, timestamp),
            )
            if head is None:
                connection.execute(
                    "INSERT INTO intervention_response_heads(request_id,current_response_id,current_response_sha256,revision_no,updated_at) VALUES(?,?,?,?,?)",
                    (request["id"], accepted["response_id"], response_sha256, revision_no, timestamp),
                )
            elif connection.execute(
                """UPDATE intervention_response_heads SET current_response_id=?,current_response_sha256=?,revision_no=?,updated_at=?
                   WHERE request_id=? AND current_response_id=? AND current_response_sha256=?""",
                (accepted["response_id"], response_sha256, revision_no, timestamp, request["id"], previous_id, previous_sha256),
            ).rowcount != 1:
                raise _response_head_changed(head)
            self._append(
                connection, "intervention.responded", goal_id=request["goal_id"], work_unit_id=request["work_unit_id"],
                payload={"request_id": request["id"], "request_sha256": request["request_sha256"],
                         "response_id": accepted["response_id"], "response_sha256": response_sha256, "revision_no": revision_no,
                         "previous_response_id": previous_id, "previous_response_sha256": previous_sha256,
                         "responder_id": responder_id, "responder_kind": responder_kind, "disposition": accepted["disposition"], "timestamp": timestamp},
            )
        return {
            "request_id": request["id"], "request_sha256": request["request_sha256"], "response_id": accepted["response_id"],
            "response_sha256": response_sha256, "revision_no": revision_no, "previous_response_id": previous_id,
            "previous_response_sha256": previous_sha256, "current_response_id": accepted["response_id"],
            "current_response_sha256": response_sha256, "mutation": "applied", "idempotent": False, "current": True,
        }

    def recover_expired_leases(self, *, goal_id: str | None = None, at: str | datetime | None = None) -> list[str]:
        timestamp, now = _clock(at)
        if goal_id is not None: goal_id = _identifier(goal_id, label="goal_id")
        recovered: list[str] = []
        affected_goals: set[str] = set()
        with self._connection() as connection:
            self._prepare_write(connection)
            query = """SELECT a.*,u.goal_id,u.attempt_count FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
                       WHERE a.status='leased' AND a.expires_at<=?"""
            params: tuple[Any, ...] = (timestamp,)
            if goal_id is not None:
                query += " AND u.goal_id=?"; params += (goal_id,)
            for row in connection.execute(query, params).fetchall():
                budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (row["goal_id"],)).fetchone()
                elapsed = min(self._attempt_elapsed(row, now), self._lease_reservation_ms(row))
                terminal = "exhausted" if budget["consumed_attempts"] >= budget["total_attempts"] or (budget["total_elapsed_ms"] is not None and budget["consumed_elapsed_ms"] + elapsed >= budget["total_elapsed_ms"]) else "retry"
                has_codex_preparation = connection.execute(
                    "SELECT 1 FROM codex_run_preparations WHERE attempt_id=? LIMIT 1", (row["id"],)
                ).fetchone() is not None
                if has_codex_preparation:
                    terminal = "blocked"
                recovery_outcome = {"recovered": True}
                if has_codex_preparation:
                    recovery_outcome["reason"] = "host-execution-requires-review"
                recovery_evidence: dict[str, Any] = recovery_outcome
                if int(row["tokens_reserved"]):
                    recovery_evidence["unmeasured_usage"] = unmeasured_usage_evidence(
                        charged_tokens=int(row["tokens_reserved"]), reason="lease-expired",
                    )
                connection.execute("""UPDATE work_attempts SET status='expired',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=?,
                                   tokens_consumed=tokens_consumed+?,token_accounting_source='unavailable' WHERE id=?""", (terminal, _encode(recovery_evidence), timestamp, elapsed, row["tokens_reserved"], row["id"]))
                connection.execute("""UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_tokens=consumed_tokens+?,
                                   consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?""", (row["tokens_reserved"], row["tokens_reserved"], elapsed, timestamp, row["goal_id"]))
                connection.execute("UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,retry_at=?,last_outcome_class=?,updated_at=? WHERE id=? AND current_attempt_id=?", ("retry-wait" if terminal == "retry" else terminal, timestamp if terminal == "retry" else None, terminal, timestamp, row["work_unit_id"], row["id"]))
                recovery_event = {"attempt_id": row["id"], "outcome": terminal}
                if has_codex_preparation:
                    recovery_event["reason"] = "host-execution-requires-review"
                self._append(connection, "work.lease_recovered", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], payload=recovery_event)
                recovered.append(row["id"])
                affected_goals.add(str(row["goal_id"]))
            for affected_goal in affected_goals:
                StateStore._finalize_drain_if_empty_in_transaction(
                    connection, affected_goal, timestamp=timestamp, actor_id="lease-recovery",
                )
        return recovered

    def requeue_work(
        self, *, work_unit_id: str, performer_id: str, envelope_sha256: str,
        evidence: Mapping[str, Any], intervention_request_id: str | None = None,
        expected_intervention_response_id: str | None = None,
        expected_intervention_response_sha256: str | None = None,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Resume blocked work only through an explicit, evidenced approval."""
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(evidence, Mapping) or not evidence:
            raise AutonomyError("requeue evidence must be a nonempty object")
        evidence_json, evidence_sha256 = _json_hash(evidence)
        structured_values = (intervention_request_id, expected_intervention_response_id, expected_intervention_response_sha256)
        if any(value is not None for value in structured_values) and any(value is None for value in structured_values):
            raise AutonomyError("structured requeue requires request id and expected response id and digest")
        structured = intervention_request_id is not None
        if structured:
            intervention_request_id = _identifier(intervention_request_id, label="intervention_request_id")
            expected_intervention_response_id = _identifier(expected_intervention_response_id, label="expected_intervention_response_id")
            if (not isinstance(expected_intervention_response_sha256, str) or len(expected_intervention_response_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in expected_intervention_response_sha256)):
                raise AutonomyError("expected_intervention_response_sha256 must be a lowercase SHA-256")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            unit = connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None:
                raise AutonomyError("unknown work unit")
            if connection.execute(
                """SELECT 1 FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id
                   JOIN work_attempts a ON a.id=p.attempt_id WHERE a.work_unit_id=? AND f.run_id IS NULL LIMIT 1""",
                (work_unit_id,),
            ).fetchone() is not None:
                raise AutonomyError("requeue requires all prior Codex runs to be resolved")
            if structured:
                historical = connection.execute(
                    """SELECT c.*,r.work_unit_id FROM intervention_closures c
                       JOIN intervention_requests r ON r.id=c.request_id WHERE c.request_id=?""",
                    (intervention_request_id,),
                ).fetchone()
                if historical is not None and historical["work_unit_id"] == work_unit_id:
                    if (
                        historical["response_id"] != expected_intervention_response_id
                        or historical["response_sha256"] != expected_intervention_response_sha256
                        or historical["closed_by"] != performer_id
                        or historical["envelope_sha256"] != envelope_sha256
                        or historical["requeue_evidence_sha256"] != evidence_sha256
                    ):
                        raise AutonomyError("idempotency_conflict")
                    return {
                        "request_id": intervention_request_id, "response_id": historical["response_id"],
                        "response_sha256": historical["response_sha256"], "closure_id": historical["id"],
                        "status": unit["status"], "mutation": "none", "idempotent": True, "current": False,
                        "current_attempt_id": unit["current_attempt_id"], "current_intervention_id": unit["current_intervention_id"],
                        "stale_reason": "unit_advanced",
                    }
            if unit["current_intervention_id"] is not None:
                if not structured:
                    raise AutonomyError("structured intervention requeue requires request and response head identities")
                if intervention_request_id != unit["current_intervention_id"]:
                    raise AutonomyError("intervention request is stale or does not belong to this work unit")
                request = connection.execute("SELECT * FROM intervention_requests WHERE id=?", (intervention_request_id,)).fetchone()
                head = connection.execute("SELECT * FROM intervention_response_heads WHERE request_id=?", (intervention_request_id,)).fetchone()
                if (
                    request is None or request["work_unit_id"] != work_unit_id or request["goal_id"] != unit["goal_id"]
                    or head is None or head["current_response_id"] != expected_intervention_response_id
                    or head["current_response_sha256"] != expected_intervention_response_sha256
                ):
                    raise _response_head_changed(head)
                response = connection.execute("SELECT * FROM intervention_responses WHERE id=?", (expected_intervention_response_id,)).fetchone()
                if response is None or response["request_id"] != intervention_request_id or response["disposition"] != "answered":
                    raise AutonomyError("intervention response is not answered")
                if connection.execute("SELECT 1 FROM intervention_closures WHERE request_id=?", (intervention_request_id,)).fetchone() is not None:
                    raise AutonomyError("intervention request is already closed")
                if unit["status"] not in {"blocked", "approval-required"}:
                    raise AutonomyError("only blocked or approval-required work may be requeued")
                self._active_goal(connection, unit["goal_id"])
                self._authorize(
                    connection, goal_id=unit["goal_id"], work_unit_id=work_unit_id,
                    action=WORK_REQUEUE_ACTION, envelope_sha256=envelope_sha256,
                    performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp,
                )
                closure_id = uuid4().hex
                connection.execute(
                    """INSERT INTO intervention_closures(
                        id,request_id,response_id,response_sha256,closure_kind,requeue_evidence_sha256,
                        envelope_sha256,closed_by,closed_at
                    ) VALUES(?,?,?,?,'requeued',?,?,?,?)""",
                    (closure_id, intervention_request_id, expected_intervention_response_id,
                     expected_intervention_response_sha256, evidence_sha256, envelope_sha256, performer_id, timestamp),
                )
                connection.execute(
                    "UPDATE work_units SET status='eligible',retry_at=NULL,current_intervention_id=NULL,updated_at=? WHERE id=?",
                    (timestamp, work_unit_id),
                )
                self._append(
                    connection, "work.requeued", goal_id=unit["goal_id"], work_unit_id=work_unit_id,
                    payload={"performer_id": performer_id, "previous_status": unit["status"], "request_id": intervention_request_id,
                             "request_sha256": request["request_sha256"], "response_id": expected_intervention_response_id,
                             "response_sha256": expected_intervention_response_sha256, "response_revision_no": response["revision_no"],
                             "closure_id": closure_id, "envelope_sha256": envelope_sha256, "evidence_sha256": evidence_sha256,
                             "timestamp": timestamp},
                )
                return {
                    "request_id": intervention_request_id, "response_id": expected_intervention_response_id,
                    "response_sha256": expected_intervention_response_sha256, "closure_id": closure_id,
                    "status": "eligible", "requeue_evidence_sha256": evidence_sha256,
                    "mutation": "applied", "idempotent": False, "current": True,
                }
            if structured:
                raise AutonomyError("intervention request is stale or closed")
            if unit["status"] not in {"blocked", "approval-required", "failed", "exhausted"}:
                raise AutonomyError("only blocked, approval-required, failed, or exhausted work may be requeued")
            self._active_goal(connection, unit["goal_id"])
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (unit["goal_id"],)).fetchone()
            if budget is None or budget["total_attempts"] is None:
                raise AutonomyError("goal lacks execution budgets")
            if int(budget["consumed_attempts"]) >= int(budget["total_attempts"]):
                raise AutonomyError("requeue cannot grant an exhausted attempt budget")
            if int(unit["attempt_count"]) >= int(budget["total_attempts"]):
                raise AutonomyError("requeue cannot grant an exhausted work-unit attempt allowance")
            if budget["total_elapsed_ms"] is not None and int(budget["consumed_elapsed_ms"]) >= int(budget["total_elapsed_ms"]):
                raise AutonomyError("requeue cannot grant an exhausted elapsed budget")
            self._authorize(
                connection, goal_id=unit["goal_id"], work_unit_id=work_unit_id,
                action=WORK_REQUEUE_ACTION, envelope_sha256=envelope_sha256,
                performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE,
                timestamp=timestamp,
            )
            previous_status = unit["status"]
            connection.execute(
                "UPDATE work_units SET status='eligible',retry_at=NULL,updated_at=? WHERE id=?",
                (timestamp, work_unit_id),
            )
            self._append(
                connection, "work.requeued", goal_id=unit["goal_id"], work_unit_id=work_unit_id,
                payload={"performer_id": performer_id, "previous_status": previous_status,
                         "evidence_sha256": evidence_sha256, "evidence": json.loads(evidence_json)},
            )
        result = self.get_work_unit(work_unit_id) or {}
        result["requeue_evidence_sha256"] = evidence_sha256
        return result

    @staticmethod
    def _codex_projection(connection: Any, row: Any) -> dict[str, Any]:
        start = connection.execute("SELECT * FROM codex_run_starts WHERE run_id=?", (row["id"],)).fetchone()
        finish = connection.execute("SELECT * FROM codex_run_finishes WHERE run_id=?", (row["id"],)).fetchone()
        attempt = connection.execute(
            "SELECT a.*,u.current_attempt_id,u.status AS unit_status FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?",
            (row["attempt_id"],),
        ).fetchone()
        accounting = connection.execute(
            """SELECT count(p.id) AS prepared,count(f.run_id) AS finished,
                count(CASE WHEN f.usage_status='measured' THEN 1 END) AS measured
               FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id WHERE p.attempt_id=?""",
            (row["attempt_id"],),
        ).fetchone()
        attempt_usage_complete = bool(accounting["prepared"]) and accounting["prepared"] == accounting["finished"] == accounting["measured"]
        run_usage_complete = finish is not None and finish["usage_status"] == "measured"
        state = "finished" if finish is not None else "started" if start is not None else "prepared"
        return {
            "run_id": row["id"], "attempt_id": row["attempt_id"], "run_no": row["run_no"], "state": state,
            "idempotency_key": row["idempotency_key"], "requested_task_name": row["requested_task_name"],
            "requested_profile": {"role": row["role"], "model": row["requested_model"],
                                  "reasoning_effort": row["requested_reasoning_effort"], "sandbox_mode": row["sandbox_mode"]},
            "request_sha256": row["request_sha256"], "handoff_sha256": row["handoff_sha256"],
            "plan_sha256": row["plan_sha256"], "brief_sha256": row["brief_sha256"], "prepared_at": row["prepared_at"],
            "actual": {"canonical_name": None if start is None else start["host_canonical_name"],
                       "agent_id": None if start is None else start["host_agent_id"], "model": None, "reasoning_effort": None},
            "result": {"status": None if finish is None else finish["result_status"],
                       "sha256": None if finish is None else finish["result_sha256"], "outcome": None if finish is None else finish["outcome"],
                       "observed_by": None if finish is None else finish["observed_by"], "recorded_at": None if finish is None else finish["recorded_at"]},
            "usage": {"status": None if finish is None else finish["usage_status"],
                      "input_tokens": None if finish is None else finish["input_tokens"],
                      "output_tokens": None if finish is None else finish["output_tokens"],
                      "complete": run_usage_complete},
            "host_token_cap_enforced": False,
            "attempt": {"is_current": attempt is not None and attempt["current_attempt_id"] == row["attempt_id"],
                        "is_live": attempt is not None and attempt["status"] == "leased" and attempt["expires_at"] > _timestamp(None),
                        "status": None if attempt is None else attempt["status"],
                        "lease_generation": row["lease_generation"],
                        "token_accounting_source": None if attempt is None else attempt["token_accounting_source"],
                        "arithmetic_tokens_consumed": None if attempt is None else attempt["tokens_consumed"],
                        "accounting_complete": attempt_usage_complete},
        }

    def prepare_codex_run(
        self, *, attempt_id: str, performer_id: str, lease_token: str, catalog: Any, config: Any,
        routing_request: Mapping[str, Any], idempotency_key: str, handoff: Mapping[str, Any] | None = None,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        attempt_id, performer_id, idempotency_key = (
            _identifier(attempt_id, label="attempt_id"), _identifier(performer_id, label="performer_id"),
            _identifier(idempotency_key, label="idempotency_key"),
        )
        if not isinstance(lease_token, str) or not lease_token:
            raise AutonomyError("attempt owner or lease token does not match")
        if not isinstance(routing_request, Mapping) or (handoff is not None and not isinstance(handoff, Mapping)):
            raise AutonomyError("Codex run request and handoff must be objects")
        if contains_secret(idempotency_key, lease_token) or contains_secret(routing_request, lease_token) or contains_secret(handoff or {}, lease_token):
            raise AutonomyError("Codex run request must not contain the supplied lease token")
        try:
            plan = delegation_plan(catalog, config, routing_request, handoff=handoff)
        except DelegationError as error:
            raise AutonomyError(f"Codex run plan is invalid: {error}") from error
        request_sha256, handoff_sha256 = sha256_json(routing_request), (None if handoff is None else sha256_json(handoff))
        plan_sha256, brief_sha256 = sha256_json(plan), sha256_json(plan["brief"])
        timestamp, _ = _clock(at)
        token_hash = sha256(lease_token.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            self._prepare_write(connection)
            existing = connection.execute("SELECT * FROM codex_run_preparations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                exact = (existing["attempt_id"] == attempt_id and existing["prepared_by"] == performer_id
                         and existing["request_sha256"] == request_sha256 and existing["handoff_sha256"] == handoff_sha256
                         and existing["plan_sha256"] == plan_sha256 and existing["brief_sha256"] == brief_sha256)
                if not exact:
                    raise AutonomyError("idempotency_conflict")
                return {"run_id": existing["id"], "attempt_id": attempt_id,
                        "requested_task_name": existing["requested_task_name"], "launch_directive": "reconcile-only",
                        "idempotent": True, "agent": plan["agent"], "plan_sha256": existing["plan_sha256"],
                        "brief_sha256": existing["brief_sha256"]}
            attempt = connection.execute(
                "SELECT a.*,u.goal_id,u.current_attempt_id FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?",
                (attempt_id,),
            ).fetchone()
            source = routing_request.get("source") if isinstance(routing_request, Mapping) else None
            if not isinstance(source, Mapping) or source.get("goal_id") != (None if attempt is None else attempt["goal_id"]) or source.get("work_unit_id") != (None if attempt is None else attempt["work_unit_id"]):
                raise AutonomyError("Codex routing request source does not match the leased attempt")
            if (attempt is None or attempt["status"] != "leased" or attempt["current_attempt_id"] != attempt_id
                    or attempt["owner_id"] != performer_id or not hmac.compare_digest(attempt["lease_token_hash"], token_hash)
                    or timestamp >= attempt["expires_at"]):
                raise AutonomyError("Codex run preparation requires the live bound work-attempt lease token")
            self._active_goal(connection, attempt["goal_id"], allow_draining=True)
            profile_values = (plan["agent"]["role"], plan["agent"]["model"], plan["agent"]["reasoning_effort"], plan["agent"]["sandbox_mode"])
            if any(value is not None and (not isinstance(value, str) or not value or len(value) > 200 or _SECRET_VALUE.search(value)) for value in profile_values):
                raise AutonomyError("Codex requested profile is invalid")
            persisted_metadata = {"prepared_by": performer_id, "idempotency_key": idempotency_key, "profile": profile_values,
                                  "repository": attempt["repository"], "revision": attempt["revision"], "branch": attempt["branch"], "workspace": attempt["workspace"]}
            if contains_secret(persisted_metadata, lease_token) or _contains_persisted_lease_token(persisted_metadata, attempt["lease_token_hash"]):
                raise AutonomyError("Codex attempt context must not contain the supplied lease token")
            if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
                raise AutonomyError("runtime is emergency-stopped")
            open_run = connection.execute(
                "SELECT id FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id WHERE p.attempt_id=? AND f.run_id IS NULL",
                (attempt_id,),
            ).fetchone()
            if open_run is not None:
                raise AutonomyError("attempt already has an unresolved Codex run")
            unfinished_runs = connection.execute(
                "SELECT count(*) FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id WHERE f.run_id IS NULL"
            ).fetchone()[0]
            if unfinished_runs >= config.concurrency_limit:
                raise AutonomyError("configured Codex host execution capacity is exhausted")
            run_no = int(connection.execute("SELECT COALESCE(MAX(run_no),0)+1 FROM codex_run_preparations WHERE attempt_id=?", (attempt_id,)).fetchone()[0])
            role = str(plan["agent"]["role"])
            requested_name = task_name(attempt_id=attempt_id, run_no=run_no, requested_role=role)
            run_id = _identifier(f"codex-run-{uuid4().hex}", label="run_id")
            connection.execute(
                """INSERT INTO codex_run_preparations(id,attempt_id,run_no,idempotency_key,prepared_by,goal_id,work_unit_id,lease_generation,envelope_sha256,repository,revision,branch,workspace,role,requested_model,requested_reasoning_effort,sandbox_mode,request_sha256,handoff_sha256,plan_sha256,brief_sha256,requested_task_name,prepared_at,lease_token_char_length)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, attempt_id, run_no, idempotency_key, performer_id, attempt["goal_id"], attempt["work_unit_id"], attempt["lease_generation"],
                 connection.execute("SELECT envelope_sha256 FROM goal_contracts WHERE goal_id=?", (attempt["goal_id"],)).fetchone()[0],
                 attempt["repository"], attempt["revision"], attempt["branch"], attempt["workspace"], role,
                 plan["agent"]["model"], plan["agent"]["reasoning_effort"], plan["agent"]["sandbox_mode"], request_sha256,
                 handoff_sha256, plan_sha256, brief_sha256, requested_name, timestamp, len(lease_token)),
            )
            self._append(connection, "codex_run.prepared", goal_id=attempt["goal_id"], work_unit_id=attempt["work_unit_id"], payload={
                "run_id": run_id, "attempt_id": attempt_id, "run_no": run_no, "idempotency_key": idempotency_key,
                "requested_task_name": requested_name, "request_sha256": request_sha256, "handoff_sha256": handoff_sha256,
                "plan_sha256": plan_sha256, "brief_sha256": brief_sha256, "prepared_at": timestamp,
                "preparation_row_sha256": _authority_row_hash("codex_run_preparations", connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()),
                "attempt_binding_sha256": _attempt_binding_sha256(attempt),
            })
        return {"run_id": run_id, "attempt_id": attempt_id, "requested_task_name": requested_name,
                "launch_directive": "invoke-once-now", "idempotent": False, "agent": plan["agent"],
                "plan_sha256": plan_sha256, "brief_sha256": brief_sha256}

    def record_codex_start(self, *, run_id: str, observer_id: str, host_canonical_name: str,
                           host_agent_id: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        run_id, observer_id = _identifier(run_id, label="run_id"), _identifier(observer_id, label="observer_id")
        if (not isinstance(host_canonical_name, str) or not re.fullmatch(r"(?:/[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*)*|[a-z][a-z0-9_]*)", host_canonical_name)
                or len(host_canonical_name) > 256 or _SECRET_VALUE.search(host_canonical_name)):
            raise AutonomyError("host canonical name is required")
        if host_agent_id is not None: host_agent_id = _identifier(host_agent_id, label="host_agent_id")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            run = connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()
            if run is None: raise AutonomyError("unknown Codex run")
            attempt = connection.execute("SELECT lease_token_hash FROM work_attempts WHERE id=?", (run["attempt_id"],)).fetchone()
            if attempt is not None and (_contains_persisted_lease_token(host_canonical_name, attempt["lease_token_hash"], run["lease_token_char_length"])
                                        or (host_agent_id is not None and _contains_persisted_lease_token(host_agent_id, attempt["lease_token_hash"], run["lease_token_char_length"]))
                                        or _contains_persisted_lease_token(observer_id, attempt["lease_token_hash"], run["lease_token_char_length"])):
                raise AutonomyError("host identity must not contain a lease token")
            if host_canonical_name.rsplit("/", 1)[-1] != run["requested_task_name"]:
                raise AutonomyError("host canonical name does not match requested task name")
            prior = connection.execute("SELECT * FROM codex_run_starts WHERE run_id=?", (run_id,)).fetchone()
            if prior is not None:
                if (prior["host_canonical_name"], prior["host_agent_id"], prior["observed_by"]) != (host_canonical_name, host_agent_id, observer_id):
                    raise AutonomyError("idempotency_conflict")
                return self._codex_projection(connection, run)
            if connection.execute("SELECT 1 FROM codex_run_starts WHERE host_canonical_name=? OR (? IS NOT NULL AND host_agent_id=?) LIMIT 1", (host_canonical_name, host_agent_id, host_agent_id)).fetchone() is not None:
                raise AutonomyError("host identity is already bound to another Codex run")
            try:
                connection.execute("INSERT INTO codex_run_starts(run_id,host_canonical_name,host_agent_id,observed_by,recorded_at) VALUES(?,?,?,?,?)", (run_id, host_canonical_name, host_agent_id, observer_id, timestamp))
            except sqlite3.IntegrityError as error:
                raise AutonomyError(f"cannot record Codex start receipt: {error}") from error
            self._append(connection, "codex_run.started", goal_id=run["goal_id"], work_unit_id=run["work_unit_id"], payload={"run_id":run_id,"host_canonical_name":host_canonical_name,"host_agent_id":host_agent_id,"observed_by":observer_id,"recorded_at":timestamp})
            return self._codex_projection(connection, run)

    def record_codex_finish(self, *, run_id: str, observer_id: str, outcome: str, result_status: str,
                            result_sha256: str | None, usage_status: str, input_tokens: int | None = None,
                            output_tokens: int | None = None, at: str | datetime | None = None) -> dict[str, Any]:
        run_id, observer_id = _identifier(run_id, label="run_id"), _identifier(observer_id, label="observer_id")
        try:
            validate_finish(outcome=outcome, result_status=result_status, result_sha256=result_sha256, usage_status=usage_status, input_tokens=input_tokens, output_tokens=output_tokens)
        except CodexRunError as error: raise AutonomyError(str(error)) from error
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            run = connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()
            if run is None: raise AutonomyError("unknown Codex run")
            attempt = connection.execute("SELECT lease_token_hash FROM work_attempts WHERE id=?", (run["attempt_id"],)).fetchone()
            if attempt is not None and _contains_persisted_lease_token(observer_id, attempt["lease_token_hash"], run["lease_token_char_length"]):
                raise AutonomyError("receipt observer must not contain a lease token")
            if connection.execute("SELECT 1 FROM codex_run_starts WHERE run_id=?", (run_id,)).fetchone() is None:
                raise AutonomyError("Codex run finish requires a recorded start")
            prior = connection.execute("SELECT * FROM codex_run_finishes WHERE run_id=?", (run_id,)).fetchone()
            supplied = (outcome,result_status,result_sha256,usage_status,input_tokens,output_tokens,observer_id)
            if prior is not None:
                if tuple(prior[key] for key in ("outcome","result_status","result_sha256","usage_status","input_tokens","output_tokens","observed_by")) != supplied: raise AutonomyError("idempotency_conflict")
                return self._codex_projection(connection, run)
            connection.execute("INSERT INTO codex_run_finishes(run_id,outcome,result_status,result_sha256,usage_status,input_tokens,output_tokens,observed_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id,*supplied,timestamp))
            self._append(connection, "codex_run.finished", goal_id=run["goal_id"], work_unit_id=run["work_unit_id"], payload={"run_id":run_id,"outcome":outcome,"result_status":result_status,"result_sha256":result_sha256,"usage_status":usage_status,"input_tokens":input_tokens,"output_tokens":output_tokens,"observed_by":observer_id,"recorded_at":timestamp})
            return self._codex_projection(connection, run)

    def get_codex_run(self, run_id: str) -> dict[str, Any] | None:
        run_id = _identifier(run_id, label="run_id")
        with self._readonly_connection() as connection:
            self._prepare_readonly(connection)
            row = connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()
            return None if row is None else self._codex_projection(connection, row)

    def list_codex_runs(self, *, attempt_id: str | None = None, limit: int = 50, after_run_id: str | None = None) -> dict[str, Any]:
        if attempt_id is not None: attempt_id = _identifier(attempt_id, label="attempt_id")
        if after_run_id is not None: after_run_id = _identifier(after_run_id, label="after_run_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100: raise AutonomyError("limit must be between 1 and 100")
        with self._readonly_connection() as connection:
            self._prepare_readonly(connection)
            query, params = "SELECT * FROM codex_run_preparations WHERE 1=1", []
            if attempt_id is not None: query += " AND attempt_id=?"; params.append(attempt_id)
            if after_run_id is not None: query += " AND id>?"; params.append(after_run_id)
            rows = connection.execute(query + " ORDER BY id LIMIT ?", (*params, limit + 1)).fetchall()
            page = rows[:limit]
            return {"items":[self._codex_projection(connection, row) for row in page], "next_after_run_id": None if len(rows)<=limit else page[-1]["id"]}

    def prepare_effect(self, *, idempotency_key: str, goal_id: str, work_unit_id: str | None,
                       effect_class: str, operation: str, request: Mapping[str, Any], envelope_sha256: str,
                       performer_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _identifier(work_unit_id, label="work_unit_id") if work_unit_id is not None else None
        performer_id = _identifier(performer_id, label="performer_id")
        operation = _identifier(operation, label="operation")
        request_json, request_hash = _bound_intent_request(request, performer_id)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            runtime_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            compatibility_write = runtime_schema in {8, 9, 10, 11}
            if compatibility_write:
                if (
                    effect_class != LOCAL_REVERSIBLE_WRITE
                    or operation != "local-effect"
                    or dict(request).get("action") != "upgrade-apply"
                    or set(request) != {"action", "plan_sha256"}
                    or not isinstance(request.get("plan_sha256"), str)
                    or len(str(request["plan_sha256"])) != 64
                ):
                    raise AutonomyError(
                        "pre-migration effect bridge only permits an exact local reversible upgrade plan"
                    )
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(
                    connection, existing_only=True,
                )
            else:
                self._prepare_write(connection)
            existing = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                if (existing["goal_id"], existing["work_unit_id"], existing["effect_class"], existing["operation"], existing["request_sha256"]) != (goal_id, work_unit_id, effect_class, operation, request_hash):
                    raise AutonomyError("idempotency key conflicts with a different effect request")
                if _intent_performer(existing) != performer_id:
                    raise AutonomyError("effect intent belongs to a different authorized performer")
                replay = dict(existing)
                receipt = connection.execute("SELECT * FROM effect_receipts WHERE intent_key=?", (idempotency_key,)).fetchone()
                if receipt is None and replay["status"] == "pending":
                    connection.execute("UPDATE effect_intents SET status='reconciliation-required' WHERE idempotency_key=?", (idempotency_key,))
                    replay["status"] = "reconciliation-required"
                    self._append(connection, "effect.reconciliation_required", goal_id=goal_id, work_unit_id=work_unit_id, payload={"idempotency_key": idempotency_key})
                replay["receipt"] = None if receipt is None else dict(receipt)
                if compatibility_write:
                    self._seal_current_state_in_transaction(
                        connection, timestamp, existing_only=True,
                    )
                return replay
            self._active_goal(connection, goal_id)
            self._authorize(connection, goal_id=goal_id, work_unit_id=work_unit_id, action=operation,
                            envelope_sha256=envelope_sha256, performer_id=performer_id, effect=effect_class, timestamp=timestamp)
            connection.execute("INSERT INTO effect_intents(idempotency_key,goal_id,work_unit_id,effect_class,operation,request_sha256,request_json,status,created_at,protocol_version,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,1,?)", (idempotency_key, goal_id, work_unit_id, effect_class, operation, request_hash, request_json, timestamp, timestamp))
            self._append(connection, "effect.prepared", goal_id=goal_id, work_unit_id=work_unit_id, payload={"idempotency_key": idempotency_key, "request_sha256": request_hash})
            result = dict(connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone())
            if compatibility_write:
                self._seal_current_state_in_transaction(
                    connection, timestamp, existing_only=True,
                )
            return result

    @staticmethod
    def _provider_authorize(connection: Any, *, goal_id: str, work_unit_id: str,
                            descriptor: Mapping[str, Any], envelope_sha256: str,
                            performer_id: str, work_attempt_id: str, lease_token: str,
                            timestamp: str, request: Mapping[str, Any],
                            expected_approval_id: str | None = None) -> dict[str, Any]:
        """Re-check every mutable authorization fact immediately before dispatch."""
        if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
            raise AutonomyError("runtime is emergency-stopped")
        goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None or goal["status"] not in {"active", "draining"}:
            raise AutonomyError("goal is not active")
        contract = connection.execute("SELECT version,envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        if contract is None or contract["version"] != "v2" or contract["envelope_sha256"] != envelope_sha256:
            raise AutonomyError("provider dispatch requires the exact current v2 envelope hash")
        try:
            envelope = load_authority_envelope(contract["contract"])
        except ValueError as error:
            raise AutonomyError(f"stored authority envelope is invalid: {error}") from error
        if envelope.get("version") != 2 or descriptor.action not in envelope["allowed_actions"] or descriptor.action in envelope["prohibited_actions"]:
            raise AutonomyError("provider action is not allowed by the authority envelope")
        if descriptor.effect_class not in envelope["allowed_effects"]:
            raise AutonomyError("provider effect is not allowed by the authority envelope")
        scopes = envelope.get("resource_scopes")
        assert descriptor.resource_scope is not None
        if not isinstance(scopes, list) or not any(_resource_scope_within(descriptor.resource_scope.to_dict(), scope) for scope in scopes if isinstance(scope, Mapping)):
            raise AutonomyError("provider resource scope is outside the authority envelope")
        unit = connection.execute(
            "SELECT goal_id,scope FROM work_units WHERE id=?", (work_unit_id,)
        ).fetchone()
        try:
            unit_scope = json.loads(unit["scope"]) if unit is not None else None
        except (TypeError, ValueError, json.JSONDecodeError):
            unit_scope = None
        if unit is None or unit["goal_id"] != goal_id or not isinstance(unit_scope, Mapping):
            raise AutonomyError("provider dispatch requires a valid work-unit scope")
        if not _scope_covers(envelope["scope"], unit_scope):
            raise AutonomyError("work unit scope is outside the authority envelope")
        request_path_scope = _git_commit_request_scope(descriptor, request)
        if request_path_scope is not None:
            if not _scope_covers(envelope["scope"], request_path_scope):
                raise AutonomyError("Git commit paths are outside the authority envelope scope")
            if not _scope_covers(unit_scope, request_path_scope):
                raise AutonomyError("Git commit paths are outside the work-unit scope")
        attempt = connection.execute(
            """SELECT a.*,u.goal_id FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
               WHERE a.id=?""", (work_attempt_id,)
        ).fetchone()
        token_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        if (attempt is None or attempt["goal_id"] != goal_id or attempt["work_unit_id"] != work_unit_id
                or attempt["owner_id"] != performer_id or attempt["status"] != "leased"
                or attempt["lease_token_hash"] != token_hash or attempt["expires_at"] <= timestamp):
            raise AutonomyError("provider dispatch requires the live bound work-attempt lease token")
        rows = connection.execute(
            """SELECT * FROM transition_approvals WHERE goal_id=? AND action=? AND envelope_sha256=?
               AND performer_id=? AND decision='approved' AND revoked_at IS NULL
               AND (work_unit_id IS NULL OR work_unit_id=?)""",
            (goal_id, descriptor.action, envelope_sha256, performer_id, work_unit_id),
        ).fetchall()
        for row in rows:
            if expected_approval_id is not None and row["id"] != expected_approval_id:
                continue
            approval = _row(row) or {}
            if (
                int(approval.get("protocol_version") or 1) < 3
                or approval["approver_kind"] != "human"
                or approval["approver_id"] == performer_id
                or approval["effect"] != descriptor.effect_class
            ):
                continue
            if approval["valid_until"] is None or approval["valid_until"] <= timestamp:
                continue
            try:
                AutonomyStore._verify_approval_evidence(connection, goal_id, approval)
                approval_scope = approval["resource_scope"]
                approval_path_scope = approval["scope"]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                _scope_covers(approval_path_scope, unit_scope)
                and _resource_scope_within(descriptor.resource_scope.to_dict(), approval_scope)
                and (request_path_scope is None or _scope_covers(approval_path_scope, request_path_scope))
            ):
                return dict(row)
        raise AutonomyError("no current human-bound approval with verified evidence binds this provider effect")

    def prepare_provider_effect(self, *, idempotency_key: str, goal_id: str, work_unit_id: str,
                                operation_descriptor: Mapping[str, Any] | ProviderOperationDescriptor,
                                request: Mapping[str, Any], envelope_sha256: str, performer_id: str,
                                work_attempt_id: str, lease_token: str,
                                at: str | datetime | None = None) -> dict[str, Any]:
        """Durably bind a v2 provider intent to one descriptor, approval, and live lease."""
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        goal_id, work_unit_id = _identifier(goal_id, label="goal_id"), _identifier(work_unit_id, label="work_unit_id")
        performer_id, work_attempt_id = _identifier(performer_id, label="performer_id"), _identifier(work_attempt_id, label="work_attempt_id")
        descriptor = _provider_descriptor(operation_descriptor)
        _reject_secret_fields(request, label="provider effect request")
        request_json, request_hash = _bound_intent_request(request, performer_id)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            existing = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                fields = ("goal_id", "work_unit_id", "provider", "capability", "operation", "effect_class", "request_sha256", "work_attempt_id", "resource_scope")
                expected = (goal_id, work_unit_id, descriptor.provider, descriptor.capability, descriptor.action, descriptor.effect_class, request_hash, work_attempt_id, _encode(descriptor.resource_scope.to_dict()))
                if existing["protocol_version"] != 2 or tuple(existing[name] for name in fields) != expected:
                    raise AutonomyError("idempotency key conflicts with a different provider effect request")
                return dict(existing)
            approval = self._provider_authorize(connection, goal_id=goal_id, work_unit_id=work_unit_id,
                descriptor=descriptor, envelope_sha256=envelope_sha256, performer_id=performer_id,
                work_attempt_id=work_attempt_id, lease_token=lease_token, timestamp=timestamp, request=request)
            connection.execute(
                """INSERT INTO effect_intents(idempotency_key,goal_id,work_unit_id,effect_class,operation,request_sha256,request_json,status,created_at,
                   protocol_version,provider,capability,envelope_sha256,approval_id,resource_scope,work_attempt_id,updated_at)
                   VALUES(?,?,?,?,?,?,?,'pending',?,2,?,?,?,?,?,?,?)""",
                (idempotency_key, goal_id, work_unit_id, descriptor.effect_class, descriptor.action, request_hash, request_json, timestamp,
                 descriptor.provider, descriptor.capability, envelope_sha256, approval["id"], _encode(descriptor.resource_scope.to_dict()), work_attempt_id, timestamp),
            )
            self._append(connection, "provider_effect.prepared", goal_id=goal_id, work_unit_id=work_unit_id,
                payload={"idempotency_key": idempotency_key, "provider": descriptor.provider, "capability": descriptor.capability, "request_sha256": request_hash})
            return dict(connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone())

    def begin_provider_effect_dispatch(self, *, idempotency_key: str, operation_descriptor: Mapping[str, Any] | OperationDescriptor,
                                       performer_id: str, lease_token: str,
                                       at: str | datetime | None = None) -> dict[str, Any]:
        """Atomically claim the sole dispatch slot; callers must reconcile stale claims."""
        idempotency_key, performer_id = _identifier(idempotency_key, label="idempotency_key"), _identifier(performer_id, label="performer_id")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None or intent["protocol_version"] != 2:
                raise AutonomyError("unknown provider effect intent")
            if _provider_descriptor(operation_descriptor) != _intent_descriptor(intent):
                raise AutonomyError("provider dispatch descriptor differs from the prepared intent")
            if intent["status"] == "executing":
                bound = connection.execute("SELECT status,expires_at FROM work_attempts WHERE id=?", (intent["work_attempt_id"],)).fetchone()
                if bound is None or bound["status"] != "leased" or bound["expires_at"] <= timestamp:
                    connection.execute("UPDATE effect_intents SET status='indeterminate',updated_at=? WHERE idempotency_key=?", (timestamp, idempotency_key))
                    self._append(connection, "provider_effect.indeterminate", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "reason": "bound_work_lease_not_live"})
                    return dict(connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone())
                else:
                    raise AutonomyError("provider effect is already executing; reconciliation is required")
            if intent["status"] != "pending":
                raise AutonomyError("provider effect is not pending; reconciliation is required")
            descriptor = _intent_descriptor(intent)
            self._provider_authorize(connection, goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], descriptor=descriptor,
                envelope_sha256=intent["envelope_sha256"], performer_id=performer_id, work_attempt_id=intent["work_attempt_id"],
                lease_token=lease_token, timestamp=timestamp, request=_intent_request(intent), expected_approval_id=intent["approval_id"])
            next_no = connection.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM effect_attempts WHERE intent_key=?", (idempotency_key,)).fetchone()[0]
            attempt_id = _identifier(f"effect-attempt-{uuid4().hex}", label="effect_attempt_id")
            lease_generation = connection.execute("SELECT lease_generation FROM work_attempts WHERE id=?", (intent["work_attempt_id"],)).fetchone()[0]
            connection.execute("INSERT INTO effect_attempts(id,intent_key,attempt_no,work_attempt_id,lease_generation,dispatched_by,dispatched_at,status) VALUES(?,?,?,?,?,?,?,'executing')", (attempt_id, idempotency_key, next_no, intent["work_attempt_id"], lease_generation, performer_id, timestamp))
            connection.execute("UPDATE effect_intents SET status='executing',updated_at=? WHERE idempotency_key=?", (timestamp, idempotency_key))
            self._append(connection, "provider_effect.dispatch_started", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "effect_attempt_id": attempt_id})
            return dict(connection.execute("SELECT * FROM effect_attempts WHERE id=?", (attempt_id,)).fetchone())

    def append_provider_effect_receipt_event(self, *, idempotency_key: str, event_type: str,
                                             observation: Mapping[str, Any], performer_id: str,
                                             effect_attempt_id: str | None = None,
                                             at: str | datetime | None = None) -> dict[str, Any]:
        """Append non-authoritative provider evidence.

        Receipt and reconciliation events change execution authority, so they
        are only created by their atomic state-transition methods below.
        """
        idempotency_key, performer_id = _identifier(idempotency_key, label="idempotency_key"), _identifier(performer_id, label="performer_id")
        effect_attempt_id = _optional_identifier(effect_attempt_id, label="effect_attempt_id")
        if event_type != "observation":
            raise AutonomyError("only provider observation events may be appended directly")
        observation_json = _sanitized_json(observation, label="provider receipt observation")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=? AND protocol_version=2", (idempotency_key,)).fetchone()
            if intent is None or _intent_performer(intent) != performer_id:
                raise AutonomyError("provider receipt event is not bound to the authorized performer")
            if effect_attempt_id is not None and connection.execute("SELECT 1 FROM effect_attempts WHERE id=? AND intent_key=?", (effect_attempt_id, idempotency_key)).fetchone() is None:
                raise AutonomyError("provider receipt event references another effect intent")
            event_id = _identifier(f"effect-event-{uuid4().hex}", label="effect_receipt_event_id")
            connection.execute("INSERT INTO effect_receipt_events(id,intent_key,effect_attempt_id,event_type,observation,observed_by,recorded_at) VALUES(?,?,?,?,?,?,?)", (event_id, idempotency_key, effect_attempt_id, event_type, observation_json, performer_id, timestamp))
            return dict(connection.execute("SELECT * FROM effect_receipt_events WHERE id=?", (event_id,)).fetchone())

    def record_provider_effect_receipt(self, *, idempotency_key: str, outcome: str, receipt: Mapping[str, Any],
                                       performer_id: str, effect_attempt_id: str | None = None,
                                       at: str | datetime | None = None) -> dict[str, Any]:
        if outcome not in _PROVIDER_TERMINAL_STATUSES:
            raise AutonomyError("provider receipt outcome must be succeeded, failed, or indeterminate")
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        if effect_attempt_id is None:
            raise AutonomyError("provider receipt requires the current effect attempt")
        effect_attempt_id = _identifier(effect_attempt_id, label="effect_attempt_id")
        observation_json = _sanitized_json(
            {"outcome": outcome, "receipt": dict(receipt)},
            label="provider receipt observation",
        )
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None or intent["protocol_version"] != 2 or _intent_performer(intent) != performer_id:
                raise AutonomyError("provider receipt is not bound to the authorized performer")
            if intent["status"] not in {"executing", "indeterminate"}:
                raise AutonomyError("provider receipt requires an executing or indeterminate effect")
            latest_attempt = connection.execute(
                "SELECT * FROM effect_attempts WHERE intent_key=? ORDER BY attempt_no DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if (
                latest_attempt is None
                or latest_attempt["id"] != effect_attempt_id
                or latest_attempt["dispatched_by"] != performer_id
                or latest_attempt["work_attempt_id"] != intent["work_attempt_id"]
            ):
                raise AutonomyError("provider receipt must reference the current dispatch attempt")
            if connection.execute(
                "SELECT 1 FROM effect_receipt_events WHERE intent_key=? AND effect_attempt_id=? AND event_type='receipt'",
                (idempotency_key, effect_attempt_id),
            ).fetchone() is not None:
                raise AutonomyError("provider dispatch attempt already has a terminal receipt")
            event_id = _identifier(f"effect-event-{uuid4().hex}", label="effect_receipt_event_id")
            connection.execute(
                "INSERT INTO effect_receipt_events(id,intent_key,effect_attempt_id,event_type,observation,observed_by,recorded_at) VALUES(?,?,?,'receipt',?,?,?)",
                (event_id, idempotency_key, effect_attempt_id, observation_json, performer_id, timestamp),
            )
            connection.execute("UPDATE effect_intents SET status=?,updated_at=? WHERE idempotency_key=?", (outcome, timestamp, idempotency_key))
            self._append(connection, "provider_effect.receipt_recorded", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "event_id": event_id, "outcome": outcome})
            return dict(connection.execute("SELECT * FROM effect_receipt_events WHERE id=?", (event_id,)).fetchone())

    def reconcile_provider_effect(self, *, idempotency_key: str, resolution: str, observation: Mapping[str, Any],
                                  performer_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        """Record a human/manual reconciliation; it can never assert absence."""
        if resolution not in {"applied", "conflict"}:
            raise AutonomyError("manual provider reconciliation resolution must be applied or conflict")
        return self._record_provider_reconciliation(
            idempotency_key=idempotency_key, resolution=resolution, observation=observation,
            performer_id=performer_id, source="manual", at=at,
        )

    def _record_adapter_provider_reconciliation(self, *, idempotency_key: str, resolution: str,
                                                observation: Mapping[str, Any], performer_id: str,
                                                at: str | datetime | None = None) -> dict[str, Any]:
        """Executor-internal adapter reconciliation; adapter absence is retry evidence."""
        if resolution not in {"applied", "absent", "conflict"}:
            raise AutonomyError("adapter provider reconciliation resolution is invalid")
        return self._record_provider_reconciliation(
            idempotency_key=idempotency_key, resolution=resolution, observation=observation,
            performer_id=performer_id, source="adapter", at=at,
        )

    def _record_provider_reconciliation(self, *, idempotency_key: str, resolution: str,
                                        observation: Mapping[str, Any], performer_id: str,
                                        source: str, at: str | datetime | None = None) -> dict[str, Any]:
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        if source not in {"manual", "adapter"}:
            raise AutonomyError("provider reconciliation source is invalid")
        observation_json = _sanitized_json(
            {"source": source, "resolution": resolution, "observation": dict(observation)},
            label="provider reconciliation observation",
        )
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None or intent["protocol_version"] != 2 or _intent_performer(intent) != performer_id:
                raise AutonomyError("provider reconciliation is not bound to the authorized performer")
            if intent["status"] != "indeterminate":
                raise AutonomyError("provider reconciliation requires an indeterminate effect")
            effect_attempt = connection.execute(
                "SELECT * FROM effect_attempts WHERE intent_key=? ORDER BY attempt_no DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if (
                effect_attempt is None
                or effect_attempt["dispatched_by"] != performer_id
                or effect_attempt["work_attempt_id"] != intent["work_attempt_id"]
            ):
                raise AutonomyError("provider reconciliation requires the current dispatch attempt")
            if connection.execute(
                "SELECT 1 FROM effect_receipt_events WHERE intent_key=? AND effect_attempt_id=? AND event_type='reconciliation'",
                (idempotency_key, effect_attempt["id"]),
            ).fetchone() is not None:
                raise AutonomyError("provider dispatch attempt is already reconciled")
            status = "failed" if resolution == "conflict" else "reconciled"
            event_id = _identifier(f"effect-event-{uuid4().hex}", label="effect_receipt_event_id")
            connection.execute(
                "INSERT INTO effect_receipt_events(id,intent_key,effect_attempt_id,event_type,observation,observed_by,recorded_at) VALUES(?,?,?,'reconciliation',?,?,?)",
                (event_id, idempotency_key, effect_attempt["id"], observation_json, performer_id, timestamp),
            )
            connection.execute(
                "UPDATE effect_intents SET status=?,last_reconciliation_event_id=?,updated_at=? WHERE idempotency_key=?",
                (status, event_id, timestamp, idempotency_key),
            )
            self._append(connection, "provider_effect.reconciled", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "event_id": event_id, "resolution": resolution, "source": source})
        return self.inspect_effect(idempotency_key) or {}

    def retry_provider_effect(self, *, idempotency_key: str, performer_id: str, work_attempt_id: str,
                              lease_token: str, at: str | datetime | None = None) -> dict[str, Any]:
        """Permit another dispatch only after durable reconciliation proved absence.

        The new attempt is still bound to a live lease and a current approval;
        this method never converts timeout or failure into an automatic retry.
        """
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        work_attempt_id = _identifier(work_attempt_id, label="work_attempt_id")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None or intent["protocol_version"] != 2 or intent["status"] != "reconciled":
                raise AutonomyError("provider retry requires an absent reconciled effect")
            if _intent_performer(intent) != performer_id:
                raise AutonomyError("provider retry performer does not match the bound intent")
            event_id = intent["last_reconciliation_event_id"]
            event = connection.execute(
                "SELECT observation FROM effect_receipt_events WHERE id=? AND intent_key=? AND event_type='reconciliation'",
                (event_id, idempotency_key),
            ).fetchone()
            try:
                payload = json.loads(event["observation"]) if event is not None else {}
                absent = payload.get("resolution") == "absent" and payload.get("source") == "adapter"
            except (TypeError, ValueError, json.JSONDecodeError):
                absent = False
            if not absent:
                raise AutonomyError("provider retry requires a durable absent reconciliation")
            descriptor = _intent_descriptor(intent)
            approval = self._provider_authorize(connection, goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], descriptor=descriptor,
                envelope_sha256=intent["envelope_sha256"], performer_id=performer_id, work_attempt_id=work_attempt_id,
                lease_token=lease_token, timestamp=timestamp, request=_intent_request(intent))
            connection.execute("UPDATE effect_intents SET status='pending',approval_id=?,work_attempt_id=?,updated_at=? WHERE idempotency_key=?", (approval["id"], work_attempt_id, timestamp, idempotency_key))
            self._append(connection, "provider_effect.retry_authorized", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "approval_id": approval["id"], "work_attempt_id": work_attempt_id})
        return self.inspect_effect(idempotency_key) or {}

    def record_effect_receipt(self, *, idempotency_key: str, outcome: str, evidence: Mapping[str, Any], performer_id: str,
                              before_sha256: str | None = None, after_sha256: str | None = None,
                              at: str | datetime | None = None) -> dict[str, Any]:
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        if outcome not in LOCAL_EFFECT_RECEIPT_OUTCOMES and outcome != "failed":
            raise AutonomyError("receipt outcome must be one of applied, success, failed-before-effect, indeterminate, or recovery-required")
        for name, digest in (("before_sha256", before_sha256), ("after_sha256", after_sha256)):
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise AutonomyError(f"{name} must be a lowercase SHA-256 digest or null")
        evidence_json, _ = _json_hash(evidence)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            runtime_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            compatibility_write = runtime_schema in {8, 9, 10, 11, 12, 13, 14}
            if outcome == "failed" and not compatibility_write:
                raise AutonomyError("legacy failed receipt only permits a pre-migration exact local upgrade")
            if compatibility_write:
                # A failed explicit upgrade must be receipted before its
                # runtime can migrate.  This deliberately admits only the
                # exact v1 local-effect bridge intent, never ordinary work or
                # provider effects from an older schema.
                self._assert_audit_chain_in_transaction(connection)
                self._assert_current_state_integrity_in_transaction(
                    connection, existing_only=True,
                )
            else:
                self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None: raise AutonomyError("unknown effect intent")
            if _intent_performer(intent) != performer_id:
                raise AutonomyError("effect receipt performer does not match the authorized intent performer")
            existing = connection.execute("SELECT * FROM effect_receipts WHERE intent_key=?", (idempotency_key,)).fetchone()
            if compatibility_write:
                try:
                    request_payload = json.loads(intent["request_json"])
                    request = request_payload["request"]
                    plan_sha256 = request["plan_sha256"]
                    exact_upgrade_intent = (
                        int(intent["protocol_version"]) == 1
                        and intent["effect_class"] == LOCAL_REVERSIBLE_WRITE
                        and intent["operation"] == "local-effect"
                        and set(request_payload) == {"authorized_performer_id", "request"}
                        and request_payload["authorized_performer_id"] == performer_id
                        and isinstance(request, dict)
                        and set(request) == {"action", "plan_sha256"}
                        and request["action"] == "upgrade-apply"
                        and isinstance(plan_sha256, str)
                        and len(plan_sha256) == 64
                        and all(character in "0123456789abcdef" for character in plan_sha256)
                        and intent["request_sha256"] == _json_hash(request)[1]
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    exact_upgrade_intent = False
                if (
                    not exact_upgrade_intent
                    or outcome not in {"failed", "failed-before-effect", "indeterminate", "recovery-required"}
                    or before_sha256 is not None
                    or after_sha256 is not None
                    or (existing is None and intent["status"] != "pending")
                    or (existing is not None and intent["status"] not in {"received", "recovery-required"})
                ):
                    raise AutonomyError(
                        "pre-migration receipt bridge only permits a failed exact local upgrade effect"
                    )
                if outcome != "failed" and (
                    evidence.get("action") != "upgrade-apply"
                    or evidence.get("plan_sha256") != plan_sha256
                    or not isinstance(evidence.get("error"), str)
                    or not evidence["error"].strip()
                ):
                    raise AutonomyError("pre-migration receipt requires bound upgrade failure evidence")
            if existing is not None:
                if (existing["outcome"], existing["before_sha256"], existing["after_sha256"], existing["evidence_json"], existing["performed_by"]) != (outcome, before_sha256, after_sha256, evidence_json, performer_id):
                    raise AutonomyError("effect receipt conflicts with the existing receipt")
                return dict(existing)
            receipt_id = _identifier(f"receipt-{uuid4().hex}", label="receipt_id")
            if outcome in {"indeterminate", "recovery-required"}:
                intent_status = "recovery-required"
            else:
                intent_status = "received"
            connection.execute("INSERT INTO effect_receipts VALUES(?,?,?,?,?,?,?,?)", (receipt_id, idempotency_key, outcome, before_sha256, after_sha256, evidence_json, performer_id, timestamp))
            connection.execute("UPDATE effect_intents SET status=? WHERE idempotency_key=?", (intent_status, idempotency_key))
            self._append(connection, "effect.receipt_recorded", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "receipt_id": receipt_id, "outcome": outcome, "recovery_required": intent_status == "recovery-required"})
            if compatibility_write:
                self._seal_current_state_in_transaction(
                    connection, timestamp, existing_only=True,
                )
            return dict(connection.execute("SELECT * FROM effect_receipts WHERE id=?", (receipt_id,)).fetchone())

    def resolve_effect_recovery(
        self, *, idempotency_key: str, resolution: str, evidence: Mapping[str, Any],
        performer_id: str, envelope_sha256: str, at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Close an indeterminate local effect only through a fresh human approval.

        This never rewrites the original receipt.  The resolution is an
        additional audit event and only `applied` or `failed-before-effect`
        can clear the outstanding recovery marker.
        """
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        if resolution not in {"applied", "failed-before-effect"}:
            raise AutonomyError("effect recovery resolution must be applied or failed-before-effect")
        if not isinstance(evidence, Mapping) or not evidence:
            raise AutonomyError("effect recovery evidence must be a nonempty object")
        evidence_json, evidence_sha256 = _json_hash(evidence)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None or int(intent["protocol_version"] or 1) != 1:
                raise AutonomyError("local effect recovery requires a local effect intent")
            if intent["status"] != "recovery-required":
                raise AutonomyError("effect does not have outstanding recovery")
            self._authorize(
                connection, goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"],
                action="effect-recovery-resolve", envelope_sha256=envelope_sha256,
                performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE,
                timestamp=timestamp, require_human=True,
            )
            connection.execute("UPDATE effect_intents SET status='received' WHERE idempotency_key=?", (idempotency_key,))
            self._append(connection, "effect.recovery_resolved", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={
                "idempotency_key": idempotency_key, "resolution": resolution,
                "performer_id": performer_id, "evidence_sha256": evidence_sha256,
                "evidence": json.loads(evidence_json),
            })
        return self.inspect_effect(idempotency_key) or {}

    def inspect_effect(self, idempotency_key: str) -> dict[str, Any] | None:
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        self._ensure()
        with self._connection(write=False) as connection:
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None: return None
            result = dict(intent)
            receipt = connection.execute("SELECT * FROM effect_receipts WHERE intent_key=?", (idempotency_key,)).fetchone()
            result["receipt"] = None if receipt is None else dict(receipt)
            return result

    def record_acceptance_evidence(self, *, goal_id: str, criterion_id: str, evidence: Mapping[str, Any],
                                   performer_id: str, envelope_sha256: str,
                                   at: str | datetime | None = None) -> dict[str, Any]:
        """Record one idempotent evidence object for an explicit envelope criterion."""
        goal_id = _identifier(goal_id, label="goal_id")
        criterion_id = _identifier(criterion_id, label="criterion_id")
        performer_id = _identifier(performer_id, label="performer_id")
        evidence_json, _ = _json_hash(evidence)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            self._active_goal(connection, goal_id)
            self._authorize(connection, goal_id=goal_id, work_unit_id=None, action="goal-complete",
                            envelope_sha256=envelope_sha256, performer_id=performer_id,
                            effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp, require_human=True)
            contract = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
            try:
                if contract is None:
                    raise AutonomyError("goal lacks an authority envelope")
                envelope = load_authority_envelope(contract["contract"])
                self._dependencies_complete_in_transaction(connection, goal_id)
                checkpoints = self._verify_goal_checkpoints_in_transaction(connection, goal_id, envelope)
                criteria = {item["id"] for item in envelope["acceptance_criteria"]}
            except ValueError as error:
                raise AutonomyError(f"stored authority envelope is invalid: {error}") from error
            if criterion_id not in criteria:
                raise AutonomyError("criterion is not in the authority envelope")
            existing = connection.execute("SELECT * FROM acceptance_evidence WHERE goal_id=? AND criterion_id=?", (goal_id, criterion_id)).fetchone()
            if existing is not None:
                if existing["evidence_json"] != evidence_json:
                    raise AutonomyError("acceptance evidence conflicts with the recorded criterion evidence")
                return dict(existing)
            connection.execute("INSERT INTO acceptance_evidence VALUES(?,?,?,?)", (goal_id, criterion_id, evidence_json, timestamp))
            self._append(connection, "acceptance.evidence_recorded", goal_id=goal_id, payload={"criterion_id": criterion_id, "performer_id": performer_id})
            return dict(connection.execute("SELECT * FROM acceptance_evidence WHERE goal_id=? AND criterion_id=?", (goal_id, criterion_id)).fetchone())

    def complete_goal(self, *, goal_id: str, performer_id: str, envelope_sha256: str,
                      at: str | datetime | None = None) -> dict[str, Any]:
        """Complete a goal only after every durable execution obligation is closed."""
        goal_id = _identifier(goal_id, label="goal_id")
        performer_id = _identifier(performer_id, label="performer_id")
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            self._active_goal(connection, goal_id)
            self._authorize(connection, goal_id=goal_id, work_unit_id=None, action="goal-complete",
                            envelope_sha256=envelope_sha256, performer_id=performer_id,
                            effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp, require_human=True,
                            require_completion_evidence=True)
            incomplete = connection.execute(
                "SELECT count(*) FROM work_units WHERE goal_id=? AND status!='complete'", (goal_id,)
            ).fetchone()[0]
            missing_evidence = connection.execute(
                """SELECT count(*) FROM work_units u LEFT JOIN workflow_evidence w ON w.work_unit_id=u.id
                   WHERE u.goal_id=? AND w.work_unit_id IS NULL""", (goal_id,)
            ).fetchone()[0]
            active = connection.execute(
                """SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
                   WHERE u.goal_id=? AND a.status='leased'""", (goal_id,)
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT count(*) FROM effect_intents WHERE goal_id=? AND ((protocol_version=1 AND status!='received') OR (protocol_version=2 AND status NOT IN ('succeeded','reconciled')))", (goal_id,)
            ).fetchone()[0]
            contract = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
            try:
                if contract is None:
                    raise AutonomyError("goal lacks an authority envelope")
                envelope = load_authority_envelope(contract["contract"])
                self._dependencies_complete_in_transaction(connection, goal_id)
                checkpoints = self._verify_goal_checkpoints_in_transaction(connection, goal_id, envelope)
                criteria = {item["id"] for item in envelope["acceptance_criteria"]}
            except ValueError as error:
                raise AutonomyError(f"stored authority envelope is invalid: {error}") from error
            recorded = {row[0] for row in connection.execute("SELECT criterion_id FROM acceptance_evidence WHERE goal_id=?", (goal_id,))}
            missing_criteria = criteria - recorded
            from .workflow import load_workflow
            for evidence in connection.execute("SELECT workflow_json,completion_token_json,u.verification_policy FROM workflow_evidence w JOIN work_units u ON u.id=w.work_unit_id WHERE u.goal_id=?", (goal_id,)):
                try:
                    if evidence["completion_token_json"] is None:
                        raise WorkflowError("missing completion token")
                    stored_workflow = load_workflow(evidence["workflow_json"])
                    policy = "implementation-review" if stored_workflow.get("version") == 1 else stored_workflow.get("verification_policy")
                    if policy != evidence["verification_policy"]:
                        raise WorkflowError("workflow verification policy does not match its work unit")
                    validate_workflow_completion_token(stored_workflow, json.loads(evidence["completion_token_json"]))
                except (WorkflowError, ValueError, json.JSONDecodeError) as error:
                    raise AutonomyError(f"stored workflow evidence is invalid: {error}") from error
            unreached_checkpoints = sum(1 for row in checkpoints if row["status"] != "reached")
            if incomplete or missing_evidence or active or pending or missing_criteria or unreached_checkpoints:
                raise AutonomyError("goal has incomplete work, missing workflow evidence, active attempts, or pending effects")
            connection.execute("UPDATE goals SET status='complete',updated_at=? WHERE id=?", (timestamp, goal_id))
            self._append(connection, "goal.completed", goal_id=goal_id, payload={"performer_id": performer_id})
        return self.get_goal(goal_id) or {}
