"""Closed, read-only case file for one durable work unit.

This projection intentionally reads the ledger directly.  The richer public
views include prose and opaque evidence that do not belong in this dossier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from .autonomy import AutonomyError, _contains_persisted_lease_token, _intent_descriptor
from .goal_readiness import _operational_gates, _synthesize
from .identifiers import is_identifier
from .overview import _lease_elapsed
from .state import SCHEMA_VERSION, StateError, StateStore, _identifier, _timestamp


_NOTICE = ("This verified snapshot is observational. Structural readiness is not claimability, "
           "priority, authority, or proof that a command remains current. Existing commands re-read "
           "state and apply their own guards.")
_EVENTS = frozenset({
    "work.claimed", "work.heartbeat", "work.finished", "work.lease_recovered", "work.requeued",
    "workflow.completed", "intervention.requested", "intervention.responded", "codex_run.prepared",
    "codex_run.started", "codex_run.finished", "provider_effect.prepared", "provider_effect.indeterminate",
    "provider_effect.dispatch_started", "provider_effect.receipt_recorded", "provider_effect.reconciled",
    "provider_effect.retry_authorized",
})
_STATUSES = frozenset({"approval-required", "blocked", "complete", "eligible", "exhausted", "failed",
                       "leased", "paused", "planned", "retry-wait", "stopped"})
_ATTEMPT_STATUSES = _STATUSES | frozenset({"active", "finished", "yielded", "retry", "transient", "permanent", "recovered", "expired"})
_ATTEMPT_OUTCOMES = _STATUSES | frozenset({"retry", "success", "transient", "permanent", "paused"})


def _bounds(limit: int, sequence: int | None, attempt: int | None) -> tuple[int, int | None, int | None]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise StateError("limit must be an integer from 1 to 50")
    for name, value in (("before_sequence", sequence), ("before_attempt_no", attempt)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9223372036854775807):
            raise StateError(f"{name} must be a positive integer")
    return limit, sequence, attempt


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise StateError("stored inspection timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateError("stored inspection timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise StateError("stored inspection timestamp is invalid")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if normalized != value:
        raise StateError("stored inspection timestamp is invalid")
    return value


def _id(value: Any, label: str) -> str:
    try:
        return _identifier(value, label=label)
    except StateError as error:
        raise StateError("stored inspection identity is invalid") from error


def _hash(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise StateError("stored inspection digest is invalid")
    return value


def _closure_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise StateError("stored inspection identity is invalid")
    return value


def _safe_identity(value: str | None, attempt: sqlite3.Row, token_length: int | None = None) -> bool:
    if value is None:
        return True
    return not (attempt["lease_token_hash"] in value or _contains_persisted_lease_token(value, attempt["lease_token_hash"], token_length))


def _context(value: str | None, attempt: sqlite3.Row) -> dict[str, Any]:
    if value is None:
        return {"present": False, "sha256": None, "redacted": False}
    digest = _digest(value)
    # Context is never retained as a raw identity.  Its only disclosure is the
    # digest, so the contract suppresses only the credential-surrogate digest.
    redacted = digest == attempt["lease_token_hash"]
    return {"present": True, "sha256": None if redacted else digest, "redacted": redacted}


def _attempt_item(row: sqlite3.Row, *, current: bool, capture: str) -> dict[str, Any]:
    attempt_id = _id(row["id"], "attempt_id")
    if not isinstance(row["attempt_no"], int) or row["attempt_no"] < 1:
        raise StateError("stored inspection attempt is invalid")
    status = row["status"]
    if status not in _ATTEMPT_STATUSES:
        raise StateError("stored inspection attempt is invalid")
    for field in ("acquired_at", "heartbeat_at", "expires_at"):
        _canonical_timestamp(row[field])
    if row["ended_at"] is not None:
        _canonical_timestamp(row["ended_at"])
    if row["outcome_class"] is not None and row["outcome_class"] not in _ATTEMPT_OUTCOMES:
        raise StateError("stored inspection attempt is invalid")
    for field in ("tokens_reserved", "tokens_consumed", "elapsed_ms"):
        if not isinstance(row[field], int) or row[field] < 0:
            raise StateError("stored inspection attempt is invalid")
    if row["token_accounting_source"] not in {"legacy-unspecified", "pending", "host-measured", "caller-declared", "unavailable"}:
        raise StateError("stored inspection attempt is invalid")
    live = status == "leased" and row["expires_at"] > capture
    expired = status == "leased" and row["expires_at"] <= capture
    return {
        "attempt_id": attempt_id, "attempt_no": row["attempt_no"], "acquired_at": row["acquired_at"],
        "heartbeat_at": row["heartbeat_at"], "expires_at": row["expires_at"], "ended_at": row["ended_at"],
        "status": status, "outcome_class": row["outcome_class"], "tokens_reserved": row["tokens_reserved"],
        "tokens_consumed": row["tokens_consumed"], "token_accounting_source": row["token_accounting_source"],
        "elapsed_ms": row["elapsed_ms"], "is_current": current, "lease_live_at_capture": live,
        "lease_expired_at_capture": expired,
        "repository_context": {name: _context(row[name], row) for name in ("repository", "revision", "branch", "workspace")},
    }


def _page(rows: list[sqlite3.Row], *, limit: int, cursor: int | None, key: str, convert: Any) -> dict[str, Any]:
    shown = rows[:limit]
    more = len(rows) > limit
    return {"items": [convert(row) for row in shown], "limit": limit, key: cursor, "returned": len(shown),
            "has_more": more, f"next_{key}": shown[-1][key.removeprefix("before_")] if more and shown else None}


def _rooted(root: Path, family: str, *rest: str) -> list[str]:
    return ["tasktra", family, "--root", str(root), *rest]


def _effect_source(connection: sqlite3.Connection, intent: sqlite3.Row, *, unit_id: str,
                   attempt_for: Any) -> sqlite3.Row | None:
    """Keep the privacy origin unambiguous after mutable intent rebinding."""
    if intent["protocol_version"] != 2 or intent["work_attempt_id"] is None:
        return None
    source = attempt_for(intent["work_attempt_id"])
    if source is None:
        return None
    crossed = connection.execute(
        """SELECT 1 FROM effect_attempts e JOIN work_attempts a ON a.id=e.work_attempt_id
           JOIN work_units u ON u.id=a.work_unit_id
           WHERE e.intent_key=? AND (e.work_attempt_id<>? OR u.id<>?) LIMIT 1""",
        (intent["idempotency_key"], intent["work_attempt_id"], unit_id),
    ).fetchone()
    if crossed is not None or any(not _safe_identity(intent[key], source)
                                  for key in ("idempotency_key", "request_sha256")):
        return None
    return source


def _provider_activity(connection: sqlite3.Connection, *, event: str, payload: dict[str, Any],
                       goal_id: str, unit_id: str, attempt_for: Any) -> dict[str, Any] | None:
    intent = connection.execute(
        "SELECT * FROM effect_intents WHERE idempotency_key=? AND goal_id=? AND work_unit_id=?",
        (payload.get("idempotency_key"), goal_id, unit_id),
    ).fetchone()
    if intent is None:
        return None
    source = _effect_source(connection, intent, unit_id=unit_id, attempt_for=attempt_for)
    if source is None:
        return None
    metadata = {"idempotency_key": _id(intent["idempotency_key"], "idempotency_key"),
                "attempt_id": _id(source["id"], "attempt_id")}
    if event == "provider_effect.prepared":
        if (payload.get("request_sha256") != intent["request_sha256"]
                or payload.get("provider") != intent["provider"]
                or payload.get("capability") != intent["capability"]):
            return None
        return dict(metadata, request_sha256=_hash(intent["request_sha256"]))
    if event == "provider_effect.indeterminate":
        return metadata if payload.get("reason") == "bound_work_lease_not_live" else None
    if event == "provider_effect.retry_authorized":
        approval = connection.execute(
            "SELECT goal_id,work_unit_id FROM transition_approvals WHERE id=?", (payload.get("approval_id"),),
        ).fetchone()
        if (payload.get("work_attempt_id") != source["id"] or approval is None
                or approval["goal_id"] != goal_id or approval["work_unit_id"] not in (None, unit_id)):
            return None
        return metadata
    if event == "provider_effect.dispatch_started":
        dispatch = connection.execute(
            "SELECT id,work_attempt_id FROM effect_attempts WHERE id=? AND intent_key=?",
            (payload.get("effect_attempt_id"), intent["idempotency_key"]),
        ).fetchone()
        if dispatch is None or dispatch["work_attempt_id"] != source["id"]:
            return None
        return dict(metadata, effect_attempt_id=_id(dispatch["id"], "effect_attempt_id"))
    receipt = connection.execute(
        "SELECT * FROM effect_receipt_events WHERE id=? AND intent_key=?",
        (payload.get("event_id"), intent["idempotency_key"]),
    ).fetchone()
    if receipt is None:
        return None
    dispatch = connection.execute(
        "SELECT id,work_attempt_id FROM effect_attempts WHERE id=? AND intent_key=?",
        (receipt["effect_attempt_id"], intent["idempotency_key"]),
    ).fetchone()
    if dispatch is None or dispatch["work_attempt_id"] != source["id"]:
        return None
    observation = json.loads(receipt["observation"])
    if not isinstance(observation, dict):
        return None
    metadata["effect_attempt_id"] = _id(dispatch["id"], "effect_attempt_id")
    if event == "provider_effect.receipt_recorded":
        outcome = observation.get("outcome")
        if (receipt["event_type"] != "receipt" or not isinstance(outcome, str)
                or outcome not in {"succeeded", "failed", "indeterminate"} or payload.get("outcome") != outcome):
            return None
        return dict(metadata, outcome=outcome)
    if event == "provider_effect.reconciled":
        resolution = observation.get("resolution")
        if (receipt["event_type"] != "reconciliation" or not isinstance(resolution, str)
                or resolution not in {"applied", "conflict", "absent"}
                or payload.get("resolution") != resolution or payload.get("source") != observation.get("source")):
            return None
        return dict(metadata, resolution=resolution,
                    reconciliation_event_id=_id(receipt["id"], "reconciliation_event_id"))
    return None


def _activity(connection: sqlite3.Connection, *, goal_id: str, unit_id: str, limit: int, cursor: int | None,
              attempt_for: Any) -> dict[str, Any]:
    query = "SELECT * FROM audit_events WHERE goal_id=? AND work_unit_id=?"
    args: list[Any] = [goal_id, unit_id]
    if cursor is not None:
        query += " AND sequence<?"; args.append(cursor)
    rows = connection.execute(query + " ORDER BY sequence DESC LIMIT ?", (*args, limit + 1)).fetchall()
    def one(row: sqlite3.Row) -> dict[str, Any]:
        event = str(row["event_type"])
        base = {"sequence": int(row["sequence"]), "created_at": _canonical_timestamp(row["created_at"]),
                "event_hash": _hash(row["event_hash"]), "metadata": {}}
        if event not in _EVENTS:
            return dict(base, event_type="unrecognized", event_type_sha256=_digest(event), recognized=False,
                        payload_status="unrecognized_type")
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="unsupported_shape")
        identity_keys = ("attempt_id", "request_id", "response_id", "run_id", "idempotency_key",
                         "effect_attempt_id", "event_id", "work_attempt_id", "approval_id")
        if any(key in payload and not is_identifier(payload[key]) for key in identity_keys):
            return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="unsupported_shape")
        metadata: dict[str, Any] = {}
        attempt_id = payload.get("attempt_id")
        candidate = attempt_for(attempt_id) if isinstance(attempt_id, str) else None
        if candidate is not None and _safe_identity(attempt_id, candidate):
            metadata["attempt_id"] = _id(attempt_id, "attempt_id")
            if "attempt_no" in payload and payload["attempt_no"] == candidate["attempt_no"]:
                metadata["attempt_no"] = candidate["attempt_no"]
        supported_work = False
        if candidate is not None and metadata:
            if event == "work.claimed":
                supported_work = (type(payload.get("attempt_no")) is int
                                  and payload["attempt_no"] == candidate["attempt_no"]
                                  and payload.get("performer_id") == candidate["owner_id"])
            elif event == "work.heartbeat":
                supported_work = True  # Native payload identifies the attempt only.
            elif event in {"work.finished", "work.lease_recovered"}:
                required_status = "expired" if event == "work.lease_recovered" else "finished"
                supported_work = (candidate["ended_at"] is not None and candidate["status"] == required_status
                                  and payload.get("outcome") == candidate["outcome_class"]
                                  and candidate["outcome_class"] in _ATTEMPT_OUTCOMES)
                if supported_work:
                    metadata["outcome"] = candidate["outcome_class"]
        if supported_work:
            return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata=metadata)
        if event == "work.requeued":
            closure = connection.execute(
                """SELECT c.*,r.goal_id,r.work_unit_id,r.attempt_id,r.request_sha256
                   FROM intervention_closures c JOIN intervention_requests r ON r.id=c.request_id
                   WHERE c.request_id=?""", (payload.get("request_id"),),
            ).fetchone()
            source = None if closure is None else attempt_for(closure["attempt_id"])
            if (source is not None and closure["goal_id"] == goal_id and closure["work_unit_id"] == unit_id
                    and payload.get("closure_id") == closure["id"]
                    and payload.get("request_sha256") == closure["request_sha256"]
                    and payload.get("response_id") == closure["response_id"]
                    and payload.get("response_sha256") == closure["response_sha256"]
                    and all(_safe_identity(closure[key], source) for key in
                            ("request_id", "request_sha256", "response_id", "response_sha256"))):
                return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={
                    "request_id": _id(closure["request_id"], "request_id"), "request_sha256": _hash(closure["request_sha256"]),
                    "response_id": _id(closure["response_id"], "response_id"), "response_sha256": _hash(closure["response_sha256"]),
                })
        if event == "intervention.requested":
            request = connection.execute("SELECT * FROM intervention_requests WHERE id=? AND goal_id=? AND work_unit_id=?", (payload.get("request_id"), goal_id, unit_id)).fetchone()
            source = None if request is None else attempt_for(request["attempt_id"])
            if (request is not None and source is not None and payload.get("request_sha256") == request["request_sha256"]
                    and payload.get("attempt_id") == request["attempt_id"] and _safe_identity(request["id"], source)
                    and _safe_identity(request["request_sha256"], source)):
                return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={
                    "request_id": _id(request["id"], "request_id"), "request_sha256": _hash(request["request_sha256"]),
                    "attempt_id": _id(request["attempt_id"], "attempt_id"), "outcome_class": request["outcome_class"],
                })
        if event == "intervention.responded":
            response = connection.execute("""SELECT q.*,r.attempt_id,r.goal_id,r.work_unit_id FROM intervention_responses q
                JOIN intervention_requests r ON r.id=q.request_id WHERE q.id=?""", (payload.get("response_id"),)).fetchone()
            source = None if response is None else attempt_for(response["attempt_id"])
            if (response is not None and source is not None and response["goal_id"] == goal_id and response["work_unit_id"] == unit_id
                    and payload.get("request_id") == response["request_id"] and payload.get("request_sha256") == response["request_sha256"]
                    and payload.get("response_sha256") == response["response_sha256"] and payload.get("revision_no") == response["revision_no"]
                    and payload.get("disposition") == response["disposition"] and _safe_identity(response["id"], source)
                    and _safe_identity(response["response_sha256"], source) and _safe_identity(response["request_id"], source)
                    and _safe_identity(response["request_sha256"], source)):
                return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={
                    "request_id": _id(response["request_id"], "request_id"), "request_sha256": _hash(response["request_sha256"]),
                    "response_id": _id(response["id"], "response_id"), "response_sha256": _hash(response["response_sha256"]),
                    "response_revision": int(response["revision_no"]), "disposition": response["disposition"],
                })
        if event.startswith("codex_run."):
            run = connection.execute("SELECT * FROM codex_run_preparations WHERE id=? AND goal_id=? AND work_unit_id=?", (payload.get("run_id"), goal_id, unit_id)).fetchone()
            source = None if run is None else attempt_for(run["attempt_id"])
            if (run is not None and source is not None and payload.get("attempt_id", run["attempt_id"]) == run["attempt_id"]
                    and all(_safe_identity(value, source, run["lease_token_char_length"]) for value in (run["id"], run["idempotency_key"], run["request_sha256"], run["handoff_sha256"], run["plan_sha256"], run["brief_sha256"]))):
                if event == "codex_run.prepared" and payload.get("request_sha256") == run["request_sha256"] and payload.get("run_no") == run["run_no"]:
                    return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={"run_id": _id(run["id"], "run_id"), "attempt_id": _id(run["attempt_id"], "attempt_id"), "attempt_no": int(source["attempt_no"]), "request_sha256": _hash(run["request_sha256"])})
                if event == "codex_run.started":
                    start = connection.execute("SELECT 1 FROM codex_run_starts WHERE run_id=?", (run["id"],)).fetchone()
                    if start is not None:
                        return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={"run_id": _id(run["id"], "run_id")})
                if event == "codex_run.finished":
                    finish = connection.execute("SELECT * FROM codex_run_finishes WHERE run_id=?", (run["id"],)).fetchone()
                    if (finish is not None and all(payload.get(key) == finish[key] for key in
                            ("outcome", "result_status", "result_sha256", "usage_status", "input_tokens", "output_tokens"))
                            and _safe_identity(finish["result_sha256"], source, run["lease_token_char_length"])):
                        result={"run_id": _id(run["id"], "run_id"), "outcome": finish["outcome"], "result_status": finish["result_status"], "usage_status": finish["usage_status"]}
                        return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata=result)
        if event.startswith("provider_effect."):
            projected = _provider_activity(connection, event=event, payload=payload, goal_id=goal_id,
                                           unit_id=unit_id, attempt_for=attempt_for)
            if projected is not None:
                return dict(base, event_type=event, event_type_sha256=None, recognized=True,
                            payload_status="recognized", metadata=projected)
        if event == "workflow.completed":
            evidence = connection.execute("SELECT workflow_sha256 FROM workflow_evidence WHERE work_unit_id=?", (unit_id,)).fetchone()
            value = payload.get("workflow_sha256")
            if evidence is not None and value == evidence["workflow_sha256"]:
                return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="recognized", metadata={"workflow_sha256": _hash(value)})
        # A known name is not enough: unsupported payload bindings remain
        # deliberately opaque rather than falling back to ledger strings.
        return dict(base, event_type=event, event_type_sha256=None, recognized=True, payload_status="unsupported_shape")
    return _page(rows, limit=limit, cursor=cursor, key="before_sequence", convert=one)


def _workflow(connection: sqlite3.Connection, unit_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT workflow_sha256,recorded_at FROM workflow_evidence WHERE work_unit_id=?", (unit_id,)).fetchone()
    if row is None:
        return {"present": False, "workflow_sha256": None, "recorded_at": None}
    return {"present": True, "workflow_sha256": _hash(row["workflow_sha256"]), "recorded_at": _canonical_timestamp(row["recorded_at"])}


def _interventions(connection: sqlite3.Connection, *, goal_id: str, unit_id: str, root: Path, limit: int,
                   attempt_for: Any, current_id: str | None) -> dict[str, Any]:
    rows = connection.execute("""SELECT r.*,h.current_response_id,h.current_response_sha256,h.revision_no,
             h.updated_at,c.id closure_id,c.closure_kind,c.response_id closure_response_id,c.response_sha256 closure_response_sha256,c.closed_at
             FROM intervention_requests r LEFT JOIN intervention_response_heads h ON h.request_id=r.id
             LEFT JOIN intervention_closures c ON c.request_id=r.id WHERE r.goal_id=? AND r.work_unit_id=?
             ORDER BY r.created_at DESC,r.id DESC LIMIT ?""", (goal_id, unit_id, limit + 1)).fetchall()
    shown: list[dict[str, Any]] = []; redacted = 0
    for row in rows[:limit]:
        attempt = attempt_for(row["attempt_id"])
        unsafe = attempt is None or not _safe_identity(row["id"], attempt) or not _safe_identity(row["request_sha256"], attempt)
        if unsafe:
            redacted += 1; continue
        head = None
        if row["current_response_id"] is not None:
            response = connection.execute("SELECT disposition,created_at FROM intervention_responses WHERE id=? AND request_id=?", (row["current_response_id"], row["id"])).fetchone()
            if response is None or not _safe_identity(row["current_response_id"], attempt) or not _safe_identity(row["current_response_sha256"], attempt):
                redacted += 1; continue
            head = {"response_id": _id(row["current_response_id"], "response_id"), "response_sha256": _hash(row["current_response_sha256"]),
                    "revision": int(row["revision_no"]), "disposition": response["disposition"], "created_at": _canonical_timestamp(response["created_at"])}
        closure = None
        if row["closure_id"] is not None:
            closure = {"closure_id": _closure_id(row["closure_id"]), "closure_kind": row["closure_kind"],
                       "response_id": _id(row["closure_response_id"], "response_id"), "response_sha256": _hash(row["closure_response_sha256"]),
                       "closed_at": _canonical_timestamp(row["closed_at"])}
        request_id = _id(row["id"], "request_id")
        shown.append({"request_id": request_id, "request_sha256": _hash(row["request_sha256"]), "attempt_id": _id(row["attempt_id"], "attempt_id"),
                      "outcome_class": row["outcome_class"], "created_at": _canonical_timestamp(row["created_at"]), "is_current": request_id == current_id,
                      "response_head": head, "closure": closure,
                      "show_argv": _rooted(root, "intervention", "show", request_id),
                      "responses_argv": _rooted(root, "intervention", "responses", request_id, "--after-revision", "0", "--limit", "20")})
    return {"items": shown, "limit": limit, "returned": len(shown), "truncated": len(rows) > limit,
            "redacted_items": redacted,
            "list_argv": _rooted(root, "intervention", "list", "--goal-id", goal_id, "--work-unit-id", unit_id, "--include-closed", "--limit", "20", "--offset", "0")}


def _codex_runs(connection: sqlite3.Connection, *, unit_id: str, root: Path, limit: int,
                attempt_for: Any) -> dict[str, Any]:
    rows = connection.execute("""SELECT p.*,s.recorded_at started_at,s.host_agent_id,f.outcome,f.result_status,f.result_sha256,f.usage_status,f.input_tokens,f.output_tokens,f.recorded_at finished_at
             FROM codex_run_preparations p LEFT JOIN codex_run_starts s ON s.run_id=p.id LEFT JOIN codex_run_finishes f ON f.run_id=p.id
             WHERE p.work_unit_id=? ORDER BY p.prepared_at DESC,p.id DESC LIMIT ?""", (unit_id, limit + 1)).fetchall()
    shown: list[dict[str, Any]]=[]; redacted=0
    for row in rows[:limit]:
        attempt=attempt_for(row["attempt_id"])
        identities=(row["id"],row["idempotency_key"],row["request_sha256"],row["handoff_sha256"],row["plan_sha256"],row["brief_sha256"],row["result_sha256"])
        if attempt is None or any(value is not None and not _safe_identity(value, attempt, row["lease_token_char_length"]) for value in identities):
            redacted+=1; continue
        state = "finished" if row["finished_at"] is not None else "started" if row["started_at"] is not None else "prepared"
        shown.append({"run_id": _id(row["id"],"run_id"), "attempt_id": _id(row["attempt_id"],"attempt_id"), "run_no": int(row["run_no"]), "state": state,
                      "prepared_at": _canonical_timestamp(row["prepared_at"]), "started_at": row["started_at"], "finished_at": row["finished_at"],
                      "idempotency_key": _id(row["idempotency_key"],"idempotency_key"), "request_sha256": _hash(row["request_sha256"]),
                      "handoff_sha256": None if row["handoff_sha256"] is None else _hash(row["handoff_sha256"]), "plan_sha256": _hash(row["plan_sha256"]), "brief_sha256": _hash(row["brief_sha256"]),
                      "outcome": row["outcome"], "result_status": row["result_status"], "result_sha256": row["result_sha256"], "usage_status": row["usage_status"],
                      "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"], "show_argv": _rooted(root,"delegation","show",_id(row["id"],"run_id"))})
        shown[-1]["_host_agent_id"] = row["host_agent_id"]
    return {"items":shown,"limit":limit,"returned":len(shown),"truncated":len(rows)>limit,"redacted_items":redacted}


def _effects(connection: sqlite3.Connection, *, goal_id: str, unit_id: str, root: Path, limit: int,
             attempt_for: Any) -> dict[str, Any]:
    rows=connection.execute("SELECT * FROM effect_intents WHERE goal_id=? AND work_unit_id=? ORDER BY created_at DESC,idempotency_key DESC LIMIT ?",(goal_id,unit_id,limit+1)).fetchall()
    shown=[]; redacted=0
    for row in rows[:limit]:
        attempt = _effect_source(connection, row, unit_id=unit_id, attempt_for=attempt_for)
        if attempt is None:
            redacted+=1; continue
        try:
            descriptor=_intent_descriptor(row)
        except AutonomyError:
            redacted+=1; continue
        if not descriptor.is_trusted_shape:
            redacted+=1; continue
        latest=connection.execute("SELECT * FROM effect_attempts WHERE intent_key=? ORDER BY attempt_no DESC LIMIT 1",(row["idempotency_key"],)).fetchone()
        if latest is not None and attempt_for(latest["work_attempt_id"]) is None:
            redacted+=1; continue
        receipt = None if latest is None else connection.execute(
            """SELECT * FROM effect_receipt_events WHERE intent_key=? AND effect_attempt_id=?
               AND event_type='receipt' ORDER BY recorded_at DESC,id DESC LIMIT 1""",
            (row["idempotency_key"], latest["id"]),
        ).fetchone()
        receipt_outcome=None
        if receipt is not None and receipt["event_type"] == "receipt":
            try:
                observed=json.loads(receipt["observation"])
                candidate=observed.get("outcome") if isinstance(observed,dict) else None
                receipt_outcome=candidate if candidate in {"succeeded","failed","indeterminate"} else None
            except (TypeError,json.JSONDecodeError):
                redacted+=1; continue
        operation_digest=_digest(json.dumps([descriptor.provider,descriptor.capability,descriptor.action],separators=(",",":"),ensure_ascii=False))
        shown.append({"idempotency_key":_id(row["idempotency_key"],"idempotency_key"),"attempt_id":_id(row["work_attempt_id"],"attempt_id"),"protocol_version":2,
                      "provider": descriptor.provider, "capability":descriptor.capability, "action":descriptor.action,
                      "operation_sha256":operation_digest,"effect_class":row["effect_class"],"status":row["status"],"request_sha256":_hash(row["request_sha256"]),"created_at":_canonical_timestamp(row["created_at"]),"updated_at":_canonical_timestamp(row["updated_at"]),
                      "current_effect_attempt_id":None if latest is None else _id(latest["id"],"effect_attempt_id"),"current_effect_attempt_no":None if latest is None else latest["attempt_no"],"current_effect_attempt_started_at":None if latest is None else _canonical_timestamp(latest["dispatched_at"]),
                      "receipt_outcome": receipt_outcome,"receipt_before_sha256":None,"receipt_after_sha256":None,"last_reconciliation_event_id":None if row["last_reconciliation_event_id"] is None else _id(row["last_reconciliation_event_id"],"reconciliation_event_id"),"inspect_argv":_rooted(root,"effect","inspect",_id(row["idempotency_key"],"idempotency_key"))})
    return {"items":shown,"limit":limit,"returned":len(shown),"truncated":len(rows)>limit,"redacted_items":redacted}


def _actions(root: Path, goal_id: str, unit_id: str, unit: dict[str, Any], interventions: dict[str, Any], runs: dict[str, Any], effects: dict[str, Any], capture_head: dict[str, Any]) -> list[dict[str, Any]]:
    base={"capture_audit_head":capture_head,"capture_identity_enforced":False,"authority_evaluated":False,"claimability_evaluated":False}
    candidates=[dict(base,kind="read_only",reason_code="action.dependencies",summary="Inspect this unit's dependencies.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id},argv=_rooted(root,"work","dependencies",goal_id,"--work-unit-id",unit_id,"--limit","1","--offset","0"),required_inputs=[],existing_guard="Read-only dependency inspection."),
                dict(base,kind="read_only",reason_code="action.impact",summary="Inspect this unit's dependency impact.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id},argv=_rooted(root,"work","impact",goal_id,unit_id,"--direction","both","--limit","20","--offset","0"),required_inputs=[],existing_guard="Read-only impact inspection."),
                dict(base,kind="read_only",reason_code="action.explain_requires_actor",summary="Evaluate current actor-specific work explanation.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id},argv=_rooted(root,"work","explain",goal_id,"--actor","{performer_id}","--envelope-sha256","{envelope_sha256}","--lease-seconds","{lease_seconds}","--token-reservation","{token_reservation}","--work-unit-id",unit_id,"--limit","1","--offset","0"),required_inputs=["performer_id","current envelope digest","lease_seconds","token_reservation"],existing_guard="The existing command re-reads state and authorization.")]
    if unit["status"] == "complete": candidates.append(dict(base,kind="no_action",reason_code="action.unit_complete",summary="The unit is complete.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id},argv=None,required_inputs=[],existing_guard="No mutation is proposed."))
    elif unit["structural_ready"] and unit["status"] != "leased": candidates.append(dict(base,kind="deferred",reason_code="action.claim_selects_global_next",summary="Claim selection is global to the goal.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id},argv=None,required_inputs=[],existing_guard="work claim cannot bind this unit."))
    if unit["current_attempt"] is not None and unit["current_attempt"]["lease_expired_at_capture"]:
        candidates.append(dict(base,kind="deferred",reason_code="action.recover_not_exactly_bound",summary="Lease recovery is goal-scoped.",relevant_identities={"goal_id":goal_id,"work_unit_id":unit_id,"attempt_id":unit["current_attempt"]["attempt_id"]},argv=None,required_inputs=[],existing_guard="work recover can recover several eligible attempts and has no exact unit or attempt guard."))
    for item in interventions["items"]:
        identities={"goal_id":goal_id,"work_unit_id":unit_id,"attempt_id":item["attempt_id"],"request_id":item["request_id"]}
        candidates.extend((
            dict(base,kind="read_only",reason_code="action.intervention_show",summary="Inspect the intervention detail.",relevant_identities=identities,argv=item["show_argv"],required_inputs=[],existing_guard="This is an exact read-only request drilldown."),
            dict(base,kind="read_only",reason_code="action.intervention_responses",summary="Inspect the intervention response history.",relevant_identities=identities,argv=item["responses_argv"],required_inputs=[],existing_guard="This is an exact read-only response drilldown."),
        ))
        head=item["response_head"]
        if item["is_current"] and head is not None and head["disposition"] == "answered" and item["closure"] is None:
            exact=dict(identities,response_id=head["response_id"],response_sha256=head["response_sha256"])
            candidates.append(dict(base,kind="guarded_template",reason_code="action.structured_requeue_available",summary="Requeue only with the current structured response binding.",relevant_identities=exact,argv=_rooted(root,"work","requeue",unit_id,"--actor","{performer_id}","--envelope-sha256","{envelope_sha256}","--evidence-json","{evidence_json_file}","--request-id",item["request_id"],"--response-id",head["response_id"],"--response-sha256",head["response_sha256"]),required_inputs=["performer_id","current envelope digest","applicable approval and authority scope","nonempty evidence JSON file"],existing_guard="The existing structured requeue transaction binds unit, current request, expected response ID, and expected response digest, and rechecks state and authorization."))
    for item in runs["items"]:
        host_agent_id = item.pop("_host_agent_id")
        identities={"goal_id":goal_id,"work_unit_id":unit_id,"attempt_id":item["attempt_id"],"run_id":item["run_id"]}
        candidates.append(dict(base,kind="read_only",reason_code="action.codex_show",summary="Inspect the Codex run detail.",relevant_identities=identities,argv=item["show_argv"],required_inputs=[],existing_guard="This is an exact read-only run drilldown."))
        if item["state"] != "finished" and host_agent_id is None:
            candidates.append(dict(base,kind="guarded_template",reason_code="action.codex_reconciliation_available",summary="Record a bounded host observation for this unresolved run.",relevant_identities=identities,argv=_rooted(root,"delegation","reconcile",item["run_id"],"--actor","{observer_id}","--observation","{observation_json_file}"),required_inputs=["observer_id","closed host observation bound to the immutable run and host identity","result bytes on stdin only when the completed observation requires them"],existing_guard="The existing reconciliation binds the immutable run and recorded host identity; stale or conflicting observations are checked by that command."))
    for item in effects["items"]:
        identities={"goal_id":goal_id,"work_unit_id":unit_id,"attempt_id":item["attempt_id"],"idempotency_key":item["idempotency_key"]}
        candidates.append(dict(base,kind="read_only",reason_code="action.effect_inspect",summary="Inspect the provider effect detail.",relevant_identities=identities,argv=item["inspect_argv"],required_inputs=[],existing_guard="This is an exact read-only effect drilldown."))
        if item["status"] == "indeterminate": candidates.append(dict(base,kind="deferred",reason_code="action.provider_reconcile_not_exactly_bound",summary="Provider reconciliation lacks an expected dispatch identity guard.",relevant_identities=identities,argv=None,required_inputs=[],existing_guard="Show refreshed inspection and use the existing read-only effect detail; no mutation template is emitted."))
    rank={"read_only":0,"guarded_template":1,"deferred":2,"no_action":3}
    return sorted(candidates,key=lambda item:(rank[item["kind"]],item["reason_code"],json.dumps(item["relevant_identities"],sort_keys=True)))


def inspect_work_unit(store: StateStore, *, project_root: Path, goal_id: str, work_unit_id: str, limit: int = 20,
                      before_sequence: int | None = None, before_attempt_no: int | None = None,
                      at: str | datetime | None = None) -> dict[str, Any]:
    """Build the complete case file in one verified read transaction."""
    goal_id, work_unit_id = _identifier(goal_id,label="goal_id"), _identifier(work_unit_id,label="work_unit_id")
    limit,before_sequence,before_attempt_no=_bounds(limit,before_sequence,before_attempt_no)
    root=Path(project_root).resolve(); capture=_timestamp(at)
    if not store.path.is_file(): raise StateError("unable to read work-unit inspection state")
    try:
        with store._connection(write=False) as connection:
            connection.create_function("tasktra_lease_elapsed",2,_lease_elapsed)
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION: raise StateError(f"runtime schema requires migration to {SCHEMA_VERSION}")
            store._assert_audit_chain_in_transaction(connection); store._assert_current_state_integrity_in_transaction(connection)
            goal=connection.execute("SELECT 1 FROM goals WHERE id=?",(goal_id,)).fetchone()
            unit_row=connection.execute("SELECT * FROM work_units WHERE id=? AND goal_id=?",(work_unit_id,goal_id)).fetchone()
            if goal is None: raise StateError("unknown goal")
            if unit_row is None: raise StateError("unknown work unit for goal")
            graph=store._work_dependency_graph_in_transaction(connection,goal_id)
            operational,checkpoints=_operational_gates(connection,goal_id=goal_id,graph=graph)
            _,remaining,_,structural=_synthesize(goal_id,graph,limit=1,offset=0,checkpoint_summaries=checkpoints,include_rows=True)
            current_id=unit_row["current_attempt_id"]
            history_sql="SELECT * FROM work_attempts WHERE work_unit_id=?"
            history_args: list[Any]=[work_unit_id]
            if before_attempt_no is not None:
                history_sql += " AND attempt_no<?"; history_args.append(before_attempt_no)
            history=connection.execute(history_sql+" ORDER BY attempt_no DESC LIMIT ?",(*history_args,limit+1)).fetchall()
            attempts_by_id={row["id"]:row for row in history}
            def attempt_for(attempt_id: str | None) -> sqlite3.Row | None:
                if attempt_id is None:
                    return None
                cached=attempts_by_id.get(attempt_id)
                if cached is not None:
                    return cached
                row=connection.execute("SELECT * FROM work_attempts WHERE id=? AND work_unit_id=?",(attempt_id,work_unit_id)).fetchone()
                if row is not None:
                    attempts_by_id[attempt_id]=row
                return row
            if current_id is not None and attempt_for(current_id) is None: raise StateError("stored current attempt is invalid")
            attempts_page=_page(history,limit=limit,cursor=before_attempt_no,key="before_attempt_no",convert=lambda row:_attempt_item(row,current=row["id"]==current_id,capture=capture))
            current_attempt=None if current_id is None else _attempt_item(attempt_for(current_id),current=True,capture=capture)
            if unit_row["status"] == "complete":
                unit={"status":"complete","checkpoint_id":unit_row["checkpoint_id"],"category":"complete","category_reason_code":"unit.complete","structural_ready":bool(graph[work_unit_id]["ready"]),"incomplete_direct_prerequisite_ids":[],"remaining_wave":None,"downstream_structural_depth":None,"deepest_remaining_branch":None,"incomplete_direct_dependents_count":0,"direct_prerequisite_gates_cleared_if_completed":0,"remaining_direct_prerequisite_gates_cleared_if_completed":0,"terminal_attention":False,"frontier_memberships":[],"operational_reason_codes":[]}
            else: unit=dict(structural[work_unit_id])
            unit.pop("work_unit_id",None); unit["attempt_count"]=int(unit_row["attempt_count"]); unit["current_attempt"]=current_attempt
            intervention_id=unit_row["current_intervention_id"]; redacted=[]
            if intervention_id is not None:
                req=connection.execute("SELECT attempt_id FROM intervention_requests WHERE id=?",(intervention_id,)).fetchone()
                source=attempt_for(req["attempt_id"]) if req else None
                if source is None: raise StateError("stored current intervention is invalid")
                if not _safe_identity(intervention_id,source): intervention_id=None; redacted=["current_intervention_id"]
            head=connection.execute("SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
            head_sequence=0 if head is None else int(head["sequence"])
            audit_head={"sequence":head_sequence,"event_hash":None if head is None else _hash(head["event_hash"])}
            activity=_activity(connection,goal_id=goal_id,unit_id=work_unit_id,limit=limit,cursor=before_sequence,attempt_for=attempt_for)
            interventions=_interventions(connection,goal_id=goal_id,unit_id=work_unit_id,root=root,limit=limit,attempt_for=attempt_for,current_id=intervention_id)
            runs=_codex_runs(connection,unit_id=work_unit_id,root=root,limit=limit,attempt_for=attempt_for)
            effects=_effects(connection,goal_id=goal_id,unit_id=work_unit_id,root=root,limit=limit,attempt_for=attempt_for)
            selected_checkpoint=next((x for x in remaining["checkpoints"] if x["checkpoint_id"]==unit_row["checkpoint_id"]),None)
            goal_context={"goal_lifecycle":operational["goal_lifecycle"],"goal_dependencies":operational["goal_dependencies"],"checkpoint":{"current_checkpoint_id":operational["checkpoints"]["current_checkpoint_id"],"selected":selected_checkpoint},"budget":operational["budget"],"capacity":operational["capacity"],"emergency_stop":operational["emergency_stop"],"intervention_attention":{"counts":operational["interventions"]["counts"],"attention_reason_codes":operational["interventions"]["attention_reason_codes"]},"unresolved_run_attention":{"counts":operational["unresolved_runs"]["counts"],"attention_reason_codes":operational["unresolved_runs"]["attention_reason_codes"]},"authority_contract":operational["authority_contract"]}
            actions=_actions(root,goal_id,work_unit_id,unit,interventions,runs,effects,audit_head)
            return {"kind":"tasktra.work-unit-inspection","version":1,"schema_version":SCHEMA_VERSION,"goal_id":goal_id,"work_unit_id":work_unit_id,"root":str(root),"read_only":True,"authority_evaluated":False,"claimability_evaluated":False,"notice":_NOTICE,"capture":{"captured_at":capture,"audit_head":audit_head,"unit_state":{"status":unit_row["status"],"current_attempt_id":current_id,"current_intervention_id":intervention_id,"updated_at":_canonical_timestamp(unit_row["updated_at"]),"redacted_fields":redacted}},"unit":unit,"goal_context":goal_context,"attempts":attempts_page,"activity":activity,"evidence_index":{"workflow":_workflow(connection,work_unit_id),"interventions":interventions,"codex_runs":runs,"provider_effects":effects},"action_candidates":actions}
    except (sqlite3.Error, StateError) as error:
        raise StateError("unable to read work-unit inspection state") from error
