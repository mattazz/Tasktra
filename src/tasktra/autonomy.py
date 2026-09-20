"""Atomic, resumable execution on top of the Stage 3 SQLite ledger."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
from pathlib import PurePosixPath
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

from .state import StateError, StateStore, _decode, _encode, _identifier, _optional_identifier, _row, _timestamp
from .authority import load_authority_envelope
from .providers import OperationDescriptor, ProviderError, ResourceScope
from .workflow import (
    WorkflowError,
    serialize_workflow,
    validate_workflow_completion_token,
    workflow_completion_token,
)


WORK_CLAIM_ACTION = "work-claim"
WORK_COMPLETE_ACTION = "work-complete"
WORK_REQUEUE_ACTION = "work-requeue"
LOCAL_REVERSIBLE_WRITE = "local-reversible-write"
OUTCOMES = frozenset({"success", "transient", "permanent", "blocked", "approval-required", "exhausted"})
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
                if int(approval.get("protocol_version") or 1) == 3:
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
        """Require a v3 final approval to bind every recorded acceptance fact."""
        if int(approval.get("protocol_version") or 1) != 3:
            raise AutonomyError(
                "goal completion requires a v3 local-human-ceremony approval with verified evidence; re-record approval"
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
    def _active_goal(connection: Any, goal_id: str) -> None:
        row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            raise AutonomyError(f"Unknown goal: {goal_id}")
        if row["status"] != "active":
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
    def _live_reservation_ms(connection: Any, goal_id: str, *, excluding_attempt_id: str | None = None) -> int:
        query = """SELECT a.acquired_at,a.expires_at FROM work_attempts a
                   JOIN work_units u ON u.id=a.work_unit_id WHERE u.goal_id=? AND a.status='leased'"""
        params: list[Any] = [goal_id]
        if excluding_attempt_id is not None:
            query += " AND a.id!=?"
            params.append(excluding_attempt_id)
        return sum(AutonomyStore._lease_reservation_ms(row) for row in connection.execute(query, params))

    def claim_next_work(self, *, goal_id: str, performer_id: str, envelope_sha256: str,
                        lease_seconds: int = 300, token_reservation: int = 0,
                        repository: str, revision: str, branch: str, workspace: str,
                        lease_token: str | None = None,
                        at: str | datetime | None = None) -> dict[str, Any] | None:
        """Atomically claim work; caller-supplied tokens are never returned."""
        goal_id = _identifier(goal_id, label="goal_id")
        performer_id = _identifier(performer_id, label="performer_id")
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
            budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
            if budget is None:
                raise AutonomyError(f"Unknown goal: {goal_id}")
            if budget["max_concurrency"] is None or budget["total_attempts"] is None:
                raise AutonomyError("goal lacks Stage 3 execution budgets")
            try:
                contract_row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
                if contract_row is None:
                    raise AutonomyError("goal lacks an authority envelope")
                contract = load_authority_envelope(contract_row["contract"])
                self._dependencies_complete_in_transaction(connection, goal_id)
                checkpoints = self._verify_goal_checkpoints_in_transaction(connection, goal_id, contract)
            except StateError as error:
                raise AutonomyError(str(error)) from error
            active = connection.execute(
                """SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
                   WHERE u.goal_id=? AND a.status='leased'""", (goal_id,)
            ).fetchone()[0]
            if active >= budget["max_concurrency"]:
                return None
            if budget["total_attempts"] is not None and budget["consumed_attempts"] >= budget["total_attempts"]:
                raise AutonomyError("attempt budget is exhausted")
            if budget["total_tokens"] is not None and (
                budget["consumed_tokens"] + budget["reserved_tokens"] + token_reservation > budget["total_tokens"]
            ):
                raise AutonomyError("token budget would be exceeded")
            live_reservation = self._live_reservation_ms(connection, goal_id)
            remaining_elapsed = None if budget["total_elapsed_ms"] is None else (
                int(budget["total_elapsed_ms"]) - int(budget["consumed_elapsed_ms"]) - live_reservation
            )
            expiry, _ = self._lease_expiry(now, lease_seconds, remaining_elapsed)
            next_checkpoint = next((row["checkpoint_id"] for row in checkpoints if row["status"] != "reached"), None)
            if contract["checkpoints"] and next_checkpoint is None:
                return None
            query = """SELECT * FROM work_units WHERE goal_id=? AND current_attempt_id IS NULL
                   AND status IN ('eligible','retry-wait','planned') AND (retry_at IS NULL OR retry_at<=?)"""
            params: list[Any] = [goal_id, timestamp]
            if contract["checkpoints"]:
                query += " AND checkpoint_id=?"
                params.append(next_checkpoint)
            else:
                query += " AND checkpoint_id IS NULL"
            candidates = connection.execute(query + " ORDER BY id", params).fetchall()
            selected = None
            for candidate in candidates:
                if candidate["attempt_count"] >= budget["total_attempts"]:
                    connection.execute("UPDATE work_units SET status='exhausted',last_outcome_class='exhausted',updated_at=? WHERE id=?", (timestamp, candidate["id"]))
                    continue
                try:
                    self._authorize(connection, goal_id=goal_id, work_unit_id=candidate["id"], action=WORK_CLAIM_ACTION,
                                    envelope_sha256=envelope_sha256, performer_id=performer_id,
                                    effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp)
                except AutonomyError:
                    continue
                selected = candidate
                break
            if selected is None:
                return None
            attempt_id = _identifier(f"attempt-{uuid4().hex}", label="attempt_id")
            token = lease_token if caller_supplied_token else secrets.token_urlsafe(32)
            assert token is not None
            token_hash = sha256(token.encode("utf-8")).hexdigest()
            attempt_no = int(selected["attempt_count"]) + 1
            connection.execute(
                """INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,repository,revision,branch,workspace,
                    acquired_at,heartbeat_at,expires_at,status,tokens_reserved)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'leased',?)""",
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
                  "work_unit": {"id": selected["id"], "title": selected["title"], "scope": json.loads(selected["scope"]), "checkpoint_id": selected["checkpoint_id"]},
                  "context": context}
        if not caller_supplied_token:
            result["lease_token"] = token
        return result

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
                connection.execute(
                    "UPDATE work_attempts SET status='expired',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=? WHERE id=?",
                    (terminal, _encode({"recovered": True}), timestamp, elapsed, attempt["id"]),
                )
                connection.execute(
                    "UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?",
                    (attempt["tokens_reserved"], elapsed, timestamp, goal_id),
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
            if budget["max_concurrency"] is None or budget["total_attempts"] is None:
                raise AutonomyError("execution budget requires max_concurrency and total_attempts")
            if active >= budget["max_concurrency"] or budget["consumed_attempts"] >= budget["total_attempts"]:
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
            connection.execute("INSERT INTO work_attempts(id,work_unit_id,attempt_no,owner_id,lease_generation,lease_token_hash,repository,revision,branch,workspace,acquired_at,heartbeat_at,expires_at,status,tokens_reserved) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'leased',?)", (attempt_id, work_unit_id, attempt_no, performer_id, attempt_no, sha256(lease_token.encode("utf-8")).hexdigest(), repository, revision, branch, workspace, timestamp, timestamp, expiry, token_reservation))
            if connection.execute("UPDATE work_units SET status='leased',lease_holder=?,lease_expires_at=?,current_attempt_id=?,attempt_count=?,retry_at=NULL,updated_at=? WHERE id=? AND current_attempt_id IS NULL", (performer_id, expiry, attempt_id, attempt_no, timestamp, work_unit_id)).rowcount != 1:
                raise AutonomyError("work unit was claimed concurrently")
            connection.execute("UPDATE budgets SET consumed_attempts=consumed_attempts+1,reserved_tokens=reserved_tokens+?,updated_at=? WHERE goal_id=?", (token_reservation, timestamp, goal_id))
            connection.execute("INSERT INTO schedule_resume_idempotency VALUES(?,?,?, ?,'consumed',?,?)", (key, digest, goal_id, work_unit_id, timestamp, attempt_id))
            self._append(connection, "schedule.resume_consumed", goal_id=goal_id, work_unit_id=work_unit_id, payload={"idempotency_key": key, "attempt_id": attempt_id})
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
            self._active_goal(connection, row["goal_id"])
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

    @staticmethod
    def _validate_completion(goal_id: str, work_unit_id: str, workflow: Mapping[str, Any], token: Mapping[str, Any] | None) -> tuple[str, str]:
        try:
            completion = workflow_completion_token(workflow) if token is None else validate_workflow_completion_token(workflow, token)
            if completion["source"] != {"goal_id": goal_id, "work_unit_id": work_unit_id}:
                raise AutonomyError("workflow completion belongs to a different goal or work unit")
            serialized = serialize_workflow(workflow)
        except WorkflowError as error:
            raise AutonomyError(f"success requires a complete Stage 2 workflow: {error}") from error
        return serialized, _encode(completion)

    @staticmethod
    def _store_workflow(connection: Any, *, goal_id: str, work_unit_id: str,
                        workflow_json: str, completion_json: str, timestamp: str) -> None:
        digest = sha256(workflow_json.encode("utf-8")).hexdigest()
        existing = connection.execute("SELECT workflow_sha256 FROM workflow_evidence WHERE work_unit_id=?", (work_unit_id,)).fetchone()
        if existing is not None and existing["workflow_sha256"] != digest:
            raise AutonomyError("work unit already has different workflow evidence")
        connection.execute(
            """INSERT INTO workflow_evidence(work_unit_id,workflow_json,workflow_sha256,completion_token_json,recorded_at)
               VALUES(?,?,?,?,?) ON CONFLICT(work_unit_id) DO NOTHING""",
            (work_unit_id, workflow_json, digest, completion_json, timestamp),
        )

    def record_workflow_completion(self, *, goal_id: str, work_unit_id: str, workflow: Mapping[str, Any],
                                   completion_token: Mapping[str, Any] | None = None,
                                   at: str | datetime | None = None) -> dict[str, Any]:
        goal_id = _identifier(goal_id, label="goal_id")
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        workflow_json, token_json = self._validate_completion(goal_id, work_unit_id, workflow, completion_token)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            unit = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None or unit["goal_id"] != goal_id:
                raise AutonomyError("unknown work unit for goal")
            self._store_workflow(connection, goal_id=goal_id, work_unit_id=work_unit_id,
                                 workflow_json=workflow_json, completion_json=token_json, timestamp=timestamp)
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
                       tokens_consumed: int = 0, elapsed_ms: int | None = None,
                       outcome_evidence: Mapping[str, Any] | None = None,
                       workflow: Mapping[str, Any] | None = None, completion_token: Mapping[str, Any] | None = None,
                       at: str | datetime | None = None) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise AutonomyError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")
        attempt_id = _identifier(attempt_id, label="attempt_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(tokens_consumed, int) or isinstance(tokens_consumed, bool) or tokens_consumed < 0:
            raise AutonomyError("tokens_consumed must be a non-negative integer")
        timestamp, now = _clock(at)
        supplied_hash = sha256(lease_token.encode("utf-8")).hexdigest() if isinstance(lease_token, str) else ""
        with self._connection() as connection:
            self._prepare_write(connection)
            row = connection.execute("""SELECT a.*,u.goal_id,u.current_attempt_id,u.attempt_count,u.checkpoint_id FROM work_attempts a
                                      JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?""", (attempt_id,)).fetchone()
            if row is None or row["status"] != "leased" or row["current_attempt_id"] != attempt_id:
                raise AutonomyError("attempt is stale or no longer current")
            if row["owner_id"] != performer_id or not hmac.compare_digest(row["lease_token_hash"], supplied_hash):
                raise AutonomyError("attempt owner or lease token does not match")
            if timestamp >= row["expires_at"]:
                raise AutonomyError("lease is expired; recover it instead")
            self._active_goal(connection, row["goal_id"])
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
            if tokens_consumed > row["tokens_reserved"]:
                raise AutonomyError("tokens_consumed exceeds the reservation")
            terminal = outcome
            workflow_json = token_json = None
            if outcome == "success":
                if workflow is None:
                    raise AutonomyError("success requires a complete Stage 2 workflow")
                self._authorize(connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], action=WORK_COMPLETE_ACTION,
                                envelope_sha256=connection.execute("SELECT envelope_sha256 FROM goal_contracts WHERE goal_id=?", (row["goal_id"],)).fetchone()[0],
                                performer_id=performer_id, effect=LOCAL_REVERSIBLE_WRITE, timestamp=timestamp)
                workflow_json, token_json = self._validate_completion(row["goal_id"], row["work_unit_id"], workflow, completion_token)
            other_reservation = self._live_reservation_ms(connection, row["goal_id"], excluding_attempt_id=attempt_id)
            if (budget["total_elapsed_ms"] is not None and budget["consumed_elapsed_ms"] + other_reservation + measured_elapsed > budget["total_elapsed_ms"]):
                terminal = "exhausted"
            if outcome == "transient" and terminal != "exhausted":
                terminal = "exhausted" if budget["consumed_attempts"] >= budget["total_attempts"] else "retry"
            elif outcome == "permanent": terminal = "failed"
            connection.execute("UPDATE work_attempts SET status='finished',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=?,tokens_consumed=? WHERE id=?", (terminal, outcome_json, timestamp, measured_elapsed, tokens_consumed, attempt_id))
            connection.execute("""UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_tokens=consumed_tokens+?,
                                consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?""",
                               (row["tokens_reserved"], tokens_consumed, measured_elapsed, timestamp, row["goal_id"]))
            connection.execute("""UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,
                                retry_at=?,last_outcome_class=?,updated_at=? WHERE id=?""",
                               ("complete" if terminal == "success" else "retry-wait" if terminal == "retry" else terminal,
                                timestamp if terminal == "retry" else None, terminal, timestamp, row["work_unit_id"]))
            if terminal == "success":
                self._store_workflow(connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], workflow_json=workflow_json or "", completion_json=token_json or "", timestamp=timestamp)
                self._reach_checkpoint_if_ready(
                    connection, goal_id=row["goal_id"], work_unit_id=row["work_unit_id"],
                    checkpoint_id=row["checkpoint_id"], workflow_json=workflow_json or "",
                    outcome_json=outcome_json, timestamp=timestamp,
                )
            self._append(connection, "work.finished", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], payload={"attempt_id": attempt_id, "outcome": terminal})
        return {"attempt_id": attempt_id, "work_unit_id": row["work_unit_id"], "outcome": terminal}

    def recover_expired_leases(self, *, goal_id: str | None = None, at: str | datetime | None = None) -> list[str]:
        timestamp, now = _clock(at)
        if goal_id is not None: goal_id = _identifier(goal_id, label="goal_id")
        recovered: list[str] = []
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
                connection.execute("UPDATE work_attempts SET status='expired',outcome_class=?,outcome_json=?,ended_at=?,elapsed_ms=? WHERE id=?", (terminal, _encode({"recovered": True}), timestamp, elapsed, row["id"]))
                connection.execute("UPDATE budgets SET reserved_tokens=reserved_tokens-?,consumed_elapsed_ms=consumed_elapsed_ms+?,updated_at=? WHERE goal_id=?", (row["tokens_reserved"], elapsed, timestamp, row["goal_id"]))
                connection.execute("UPDATE work_units SET status=?,lease_holder=NULL,lease_expires_at=NULL,current_attempt_id=NULL,retry_at=?,last_outcome_class=?,updated_at=? WHERE id=? AND current_attempt_id=?", ("retry-wait" if terminal == "retry" else terminal, timestamp if terminal == "retry" else None, terminal, timestamp, row["work_unit_id"], row["id"]))
                self._append(connection, "work.lease_recovered", goal_id=row["goal_id"], work_unit_id=row["work_unit_id"], payload={"attempt_id": row["id"], "outcome": terminal})
                recovered.append(row["id"])
        return recovered

    def requeue_work(
        self, *, work_unit_id: str, performer_id: str, envelope_sha256: str,
        evidence: Mapping[str, Any], at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Resume blocked work only through an explicit, evidenced approval."""
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(evidence, Mapping) or not evidence:
            raise AutonomyError("requeue evidence must be a nonempty object")
        evidence_json, evidence_sha256 = _json_hash(evidence)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            unit = connection.execute("SELECT * FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
            if unit is None:
                raise AutonomyError("unknown work unit")
            if unit["status"] not in {"blocked", "approval-required"}:
                raise AutonomyError("only blocked or approval-required work may be requeued")
            self._active_goal(connection, unit["goal_id"])
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
            compatibility_write = runtime_schema in {8, 9}
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
                            timestamp: str, expected_approval_id: str | None = None) -> dict[str, Any]:
        """Re-check every mutable authorization fact immediately before dispatch."""
        if connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()[0]:
            raise AutonomyError("runtime is emergency-stopped")
        goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None or goal["status"] != "active":
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
                int(approval.get("protocol_version") or 1) != 3
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
            ):
                return dict(row)
        raise AutonomyError("no current v3 human-ceremony approval with verified evidence binds this provider effect")

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
                work_attempt_id=work_attempt_id, lease_token=lease_token, timestamp=timestamp)
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
                lease_token=lease_token, timestamp=timestamp, expected_approval_id=intent["approval_id"])
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
                lease_token=lease_token, timestamp=timestamp)
            connection.execute("UPDATE effect_intents SET status='pending',approval_id=?,work_attempt_id=?,updated_at=? WHERE idempotency_key=?", (approval["id"], work_attempt_id, timestamp, idempotency_key))
            self._append(connection, "provider_effect.retry_authorized", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "approval_id": approval["id"], "work_attempt_id": work_attempt_id})
        return self.inspect_effect(idempotency_key) or {}

    def record_effect_receipt(self, *, idempotency_key: str, outcome: str, evidence: Mapping[str, Any], performer_id: str,
                              before_sha256: str | None = None, after_sha256: str | None = None,
                              at: str | datetime | None = None) -> dict[str, Any]:
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        performer_id = _identifier(performer_id, label="performer_id")
        if not isinstance(outcome, str) or not outcome: raise AutonomyError("receipt outcome must be non-empty")
        for name, digest in (("before_sha256", before_sha256), ("after_sha256", after_sha256)):
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise AutonomyError(f"{name} must be a lowercase SHA-256 digest or null")
        evidence_json, _ = _json_hash(evidence)
        timestamp, _ = _clock(at)
        with self._connection() as connection:
            self._prepare_write(connection)
            intent = connection.execute("SELECT * FROM effect_intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if intent is None: raise AutonomyError("unknown effect intent")
            if _intent_performer(intent) != performer_id:
                raise AutonomyError("effect receipt performer does not match the authorized intent performer")
            existing = connection.execute("SELECT * FROM effect_receipts WHERE intent_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                if (existing["outcome"], existing["before_sha256"], existing["after_sha256"], existing["evidence_json"], existing["performed_by"]) != (outcome, before_sha256, after_sha256, evidence_json, performer_id):
                    raise AutonomyError("effect receipt conflicts with the existing receipt")
                return dict(existing)
            receipt_id = _identifier(f"receipt-{uuid4().hex}", label="receipt_id")
            connection.execute("INSERT INTO effect_receipts VALUES(?,?,?,?,?,?,?,?)", (receipt_id, idempotency_key, outcome, before_sha256, after_sha256, evidence_json, performer_id, timestamp))
            connection.execute("UPDATE effect_intents SET status='received' WHERE idempotency_key=?", (idempotency_key,))
            self._append(connection, "effect.receipt_recorded", goal_id=intent["goal_id"], work_unit_id=intent["work_unit_id"], payload={"idempotency_key": idempotency_key, "receipt_id": receipt_id})
            return dict(connection.execute("SELECT * FROM effect_receipts WHERE id=?", (receipt_id,)).fetchone())

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
            for evidence in connection.execute("SELECT workflow_json,completion_token_json FROM workflow_evidence w JOIN work_units u ON u.id=w.work_unit_id WHERE u.goal_id=?", (goal_id,)):
                try:
                    if evidence["completion_token_json"] is None:
                        raise WorkflowError("missing completion token")
                    validate_workflow_completion_token(load_workflow(evidence["workflow_json"]), json.loads(evidence["completion_token_json"]))
                except (WorkflowError, ValueError, json.JSONDecodeError) as error:
                    raise AutonomyError(f"stored workflow evidence is invalid: {error}") from error
            unreached_checkpoints = sum(1 for row in checkpoints if row["status"] != "reached")
            if incomplete or missing_evidence or active or pending or missing_criteria or unreached_checkpoints:
                raise AutonomyError("goal has incomplete work, missing workflow evidence, active attempts, or pending effects")
            connection.execute("UPDATE goals SET status='complete',updated_at=? WHERE id=?", (timestamp, goal_id))
            self._append(connection, "goal.completed", goal_id=goal_id, payload={"performer_id": performer_id})
        return self.get_goal(goal_id) or {}
