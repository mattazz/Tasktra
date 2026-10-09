"""Verified, bounded operator-intervention read projections."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from typing import Any, Iterator

from .overview import _public_text
from .state import SCHEMA_VERSION, StateError, StateStore, _identifier, _timestamp


_MAX_INTEGER = (1 << 63) - 1
_STATES = ("open", "answered", "declined", "cancelled", "closed", "legacy")
_CURRENT_JOINS = """
    FROM work_units u
    JOIN intervention_requests r ON r.id=u.current_intervention_id
    LEFT JOIN intervention_response_heads h ON h.request_id=r.id
    LEFT JOIN intervention_responses s ON s.id=h.current_response_id
"""
_CLOSED_JOINS = """
    FROM intervention_closures c
    JOIN intervention_requests r ON r.id=c.request_id
    JOIN work_units u ON u.id=r.work_unit_id
"""


def _integer(value: int, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StateError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


@contextmanager
def _snapshot(store: StateStore) -> Iterator[sqlite3.Connection]:
    if not store.path.is_file():
        raise StateError("Runtime database does not exist")
    try:
        with store._connection(write=False) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            if version > SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires a compatible Tasktra build")
            store._assert_audit_chain_in_transaction(connection)
            store._assert_current_state_integrity_in_transaction(connection)
            yield connection
    except sqlite3.Error as error:
        raise StateError("unable to read intervention state") from error


def _filters(connection: sqlite3.Connection, goal_id: str | None, work_unit_id: str | None) -> tuple[str, list[str]]:
    clauses, parameters = [], []
    if goal_id is not None:
        goal_id = _identifier(goal_id, label="goal_id")
        if connection.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
            raise StateError("Unknown goal")
        clauses.append("u.goal_id=?")
        parameters.append(goal_id)
    if work_unit_id is not None:
        work_unit_id = _identifier(work_unit_id, label="work_unit_id")
        row = connection.execute("SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)).fetchone()
        if row is None or (goal_id is not None and row[0] != goal_id):
            raise StateError("Unknown work unit or mismatched goal")
        clauses.append("u.id=?")
        parameters.append(work_unit_id)
    return (" AND ".join(clauses) or "1=1"), parameters


def intervention_counts(connection: sqlite3.Connection, *, goal_id: str | None = None,
                        work_unit_id: str | None = None, include_closed: bool = True,
                        include_legacy: bool = True) -> dict[str, int]:
    """Aggregate heads only within the caller's existing snapshot."""
    where, parameters = _filters(connection, goal_id, work_unit_id)
    result = dict.fromkeys(_STATES, 0)
    for row in connection.execute(
        "SELECT COALESCE(s.disposition,'open') AS state, count(*) AS count "
        + _CURRENT_JOINS + f" WHERE {where} GROUP BY state", parameters,
    ):
        result[str(row["state"])] = int(row["count"])
    if include_closed:
        result["closed"] = int(connection.execute("SELECT count(*) " + _CLOSED_JOINS + f" WHERE {where}", parameters).fetchone()[0])
    if include_legacy:
        result["legacy"] = int(connection.execute(
            f"SELECT count(*) FROM work_units u WHERE {where} AND u.current_intervention_id IS NULL "
            "AND u.status IN ('approval-required','blocked','failed','exhausted')", parameters,
        ).fetchone()[0])
    return result


def _age(captured_at: str, created_at: str) -> int:
    return max(0, int((datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                       - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds()))


def _evidence(value: list[dict[str, Any]], *, detail: bool) -> list[dict[str, Any]]:
    return [dict(item) if detail else {key: item[key] for key in ("id", "kind", "summary")} for item in value]


def _impact(connection: sqlite3.Connection, unit_id: str) -> int:
    return int(connection.execute(
        "SELECT count(*) FROM work_unit_dependencies d JOIN work_units u ON u.id=d.work_unit_id "
        "WHERE d.prerequisite_id=? AND u.status!='complete'", (unit_id,),
    ).fetchone()[0])


def _response(row: sqlite3.Row | None, *, detail: bool) -> dict[str, Any] | None:
    if row is None:
        return None
    value = json.loads(row["response_json"])
    result = {
        "id": row["id"], "sha256": row["response_sha256"], "revision_no": row["revision_no"],
        "previous_response_id": row["previous_response_id"],
        "disposition": row["disposition"], "responder": value["responder"],
        "created_at": row["created_at"], "evidence_refs": _evidence(value["evidence_refs"], detail=detail),
    }
    if detail:
        result.update(answer=value["answer"], rationale=value["rationale"],
                      expected_current_response=value["expected_current_response"])
    return result


def _request_item(connection: sqlite3.Connection, request_id: str, *, captured_at: str,
                  detail: bool) -> dict[str, Any]:
    row = connection.execute(
        "SELECT r.*,u.title AS unit_title,u.status AS unit_status,u.current_intervention_id "
        "FROM intervention_requests r JOIN work_units u ON u.id=r.work_unit_id WHERE r.id=?", (request_id,),
    ).fetchone()
    if row is None:
        raise StateError("Unknown intervention request")
    value = json.loads(row["request_json"])
    head = connection.execute(
        "SELECT s.* FROM intervention_response_heads h JOIN intervention_responses s ON s.id=h.current_response_id "
        "WHERE h.request_id=?", (request_id,),
    ).fetchone()
    closure = connection.execute("SELECT * FROM intervention_closures WHERE request_id=?", (request_id,)).fetchone()
    result = {
        "record_kind": "structured-request", "request_id": request_id, "request_sha256": row["request_sha256"],
        "goal_id": row["goal_id"], "work_unit_id": row["work_unit_id"], "attempt_id": row["attempt_id"],
        "producer_id": row["producer_id"], "unit_title": _public_text(row["unit_title"]), "unit_status": row["unit_status"],
        "created_at": row["created_at"], "age_seconds": _age(captured_at, row["created_at"]),
        "outcome_class": row["outcome_class"], "prompt": value["prompt"], "rationale": value["rationale"],
        "impact": value["impact"], "requires_human_approval": value["requires_human_approval"],
        "current": row["current_intervention_id"] == request_id,
        "response_state": "open" if head is None else head["disposition"],
        "response": _response(head, detail=detail), "response_revision_count": 0 if head is None else head["revision_no"],
        "closure": None if closure is None else {
            "id": closure["id"], "kind": closure["closure_kind"], "closed_by": closure["closed_by"],
            "closed_at": closure["closed_at"], "response_id": closure["response_id"],
            "response_sha256": closure["response_sha256"],
        },
        "incomplete_direct_dependents_count": _impact(connection, row["work_unit_id"]),
        "evidence_refs": _evidence(value["evidence_refs"], detail=detail),
        "inspection_argv": ["tasktra", "intervention", "show", request_id],
    }
    return result


def _legacy_item(connection: sqlite3.Connection, unit_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT id,goal_id,title,status,last_outcome_class,updated_at FROM work_units WHERE id=?", (unit_id,)).fetchone()
    return {
        "record_kind": "legacy-blocker", "goal_id": row["goal_id"], "work_unit_id": row["id"],
        "unit_title": _public_text(row["title"]), "unit_status": row["status"], "last_outcome_class": row["last_outcome_class"],
        "updated_at": row["updated_at"], "request_id": None, "response_state": "unstructured",
        "incomplete_direct_dependents_count": _impact(connection, unit_id),
        "inspection_argv": ["tasktra", "overview", "--goal-id", row["goal_id"]],
    }


def _closed_page_query(*, goal_id: str | None, work_unit_id: str | None) -> tuple[str, list[str]]:
    clauses, parameters = [], []
    if goal_id is not None:
        clauses.append('r.goal_id=?')
        parameters.append(goal_id)
    if work_unit_id is not None:
        clauses.append('r.work_unit_id=?')
        parameters.append(work_unit_id)
    # Keep closure order at the outside of the join: a sparse goal filter may
    # scan index entries, but never sorts or materializes the closed history.
    query = (
        "SELECT 'structured-request' AS kind,c.request_id AS id "
        "FROM intervention_closures c INDEXED BY intervention_closures_closed_request "
        "CROSS JOIN intervention_requests r ON r.id=c.request_id"
    )
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    return query + ' ORDER BY c.closed_at,c.request_id COLLATE BINARY LIMIT ? OFFSET ?', parameters


def intervention_inbox(store: StateStore, *, goal_id: str | None = None, work_unit_id: str | None = None,
                       include_closed: bool = False, include_legacy: bool = True,
                       limit: int = 20, offset: int = 0, at: str | datetime | None = None) -> dict[str, Any]:
    limit = _integer(limit, "limit", 1, 200)
    offset = _integer(offset, "offset", 0, 1_000_000)
    if type(include_closed) is not bool or type(include_legacy) is not bool:
        raise StateError("include_closed and include_legacy must be booleans")
    captured_at = _timestamp(at)
    with _snapshot(store) as connection:
        where, parameters = _filters(connection, goal_id, work_unit_id)
        aggregate = intervention_counts(connection, goal_id=goal_id, work_unit_id=work_unit_id,
                                        include_closed=include_closed, include_legacy=include_legacy)
        current_count = sum(aggregate[state] for state in ('open', 'answered', 'declined', 'cancelled'))
        slices = [(current_count,
            "SELECT 'structured-request' AS kind,r.id AS id,"
            "CASE COALESCE(s.disposition,'open') WHEN 'open' THEN 0 WHEN 'answered' THEN 1 WHEN 'declined' THEN 2 ELSE 3 END AS response_rank,"
            "CASE r.outcome_class WHEN 'approval-required' THEN 0 ELSE 1 END AS outcome_rank,r.created_at AS ordered_at "
            + _CURRENT_JOINS + f" WHERE {where}"
            + " ORDER BY response_rank,outcome_rank,ordered_at,r.id COLLATE BINARY LIMIT ? OFFSET ?", parameters)]
        if include_legacy:
            slices.append((aggregate['legacy'],
                "SELECT 'legacy-blocker' AS kind,u.id AS id,CASE u.status WHEN 'approval-required' THEN 0 WHEN 'blocked' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END AS outcome_rank,u.updated_at "
                f"FROM work_units u WHERE {where} AND u.current_intervention_id IS NULL "
                "AND u.status IN ('approval-required','blocked','failed','exhausted')"
                " ORDER BY outcome_rank,u.updated_at,u.id COLLATE BINARY LIMIT ? OFFSET ?", parameters))
        if include_closed:
            query, closed_parameters = _closed_page_query(goal_id=goal_id, work_unit_id=work_unit_id)
            slices.append((aggregate['closed'], query, closed_parameters))
        rows: list[sqlite3.Row] = []
        remaining_offset = offset
        for count, query, query_parameters in slices:
            if remaining_offset >= count:
                remaining_offset -= count
                continue
            remaining_limit = limit - len(rows)
            if remaining_limit == 0:
                break
            rows.extend(connection.execute(query, [*query_parameters, remaining_limit, remaining_offset]))
            remaining_offset = 0
        items = [(_legacy_item(connection, row["id"]) if row["kind"] == "legacy-blocker" else
                  _request_item(connection, row["id"], captured_at=captured_at, detail=False)) for row in rows]
        total = sum(aggregate.values())
        return {
            "kind": "tasktra.intervention-inbox", "version": 1, "read_only": True, "captured_at": captured_at,
            "filters": {"goal_id": goal_id, "work_unit_id": work_unit_id,
                        "include_closed": include_closed, "include_legacy": include_legacy},
            "pagination": {"limit": limit, "offset": offset, "total": total, "returned": len(items), "has_more": offset + len(items) < total},
            "aggregates": aggregate, "items": items,
        }


def intervention_detail(store: StateStore, request_id: str, *, at: str | datetime | None = None) -> dict[str, Any]:
    request_id = _identifier(request_id, label="request_id")
    captured_at = _timestamp(at)
    with _snapshot(store) as connection:
        return {"kind": "tasktra.intervention-detail", "version": 1, "read_only": True,
                "captured_at": captured_at,
                "request": _request_item(connection, request_id, captured_at=captured_at, detail=True)}


def intervention_response_history(store: StateStore, request_id: str, *, after_revision: int = 0,
                                  limit: int = 20, at: str | datetime | None = None) -> dict[str, Any]:
    request_id = _identifier(request_id, label="request_id")
    after_revision = _integer(after_revision, "after_revision", 0, _MAX_INTEGER)
    limit = _integer(limit, "limit", 1, 200)
    captured_at = _timestamp(at)
    with _snapshot(store) as connection:
        request = connection.execute("SELECT id FROM intervention_requests WHERE id=?", (request_id,)).fetchone()
        if request is None:
            raise StateError("Unknown intervention request")
        head = connection.execute("SELECT revision_no FROM intervention_response_heads WHERE request_id=?", (request_id,)).fetchone()
        total = 0 if head is None else int(head[0])
        rows = connection.execute(
            "SELECT * FROM intervention_responses WHERE request_id=? AND revision_no>? ORDER BY revision_no LIMIT ?",
            (request_id, after_revision, limit),
        ).fetchall()
        last = after_revision if not rows else int(rows[-1]["revision_no"])
        has_more = last < total
        return {
            "kind": "tasktra.intervention-response-history", "version": 1, "read_only": True,
            "captured_at": captured_at, "request_id": request_id,
            "pagination": {"limit": limit, "after_revision": after_revision, "total": total, "returned": len(rows),
                           "has_more": has_more, "next_after_revision": last if has_more else None},
            "responses": [_response(row, detail=True) for row in rows],
        }
