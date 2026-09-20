"""Bounded, read-only operational views over the durable Tasktra ledger."""

from __future__ import annotations

import json
from typing import Any

from .state import StateError, StateStore, _identifier, _now, _row


MAX_DETAIL_LIMIT = 32
MAX_AUDIT_EXPORT = 200


def _limit(value: int, *, maximum: int, label: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise StateError(f"{label} must be between {minimum} and {maximum}")
    return value


def _table_exists(connection: Any, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def operational_status(
    store: StateStore, *, goal_id: str | None = None, detail_limit: int = 0
) -> dict[str, Any]:
    """Return concise health by default and bounded goal detail on request."""
    detail_limit = _limit(
        detail_limit, maximum=MAX_DETAIL_LIMIT, label="detail_limit", allow_zero=True
    )
    if goal_id is not None:
        goal_id = _identifier(goal_id, label="goal_id")
    store._ensure()
    with store._connection(write=False) as connection:
        control = connection.execute("SELECT * FROM runtime_control WHERE id=1").fetchone()
        summary: dict[str, Any] = {
            "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "emergency_stop": {
                "active": bool(control["emergency_stopped"]),
                "reason": control["reason"],
                "set_at": control["set_at"],
            },
            "goals": connection.execute("SELECT count(*) FROM goals").fetchone()[0],
            "active_goals": connection.execute(
                "SELECT count(*) FROM goals WHERE status='active'"
            ).fetchone()[0],
            "active_leases": connection.execute(
                "SELECT count(*) FROM work_attempts WHERE status='leased'"
            ).fetchone()[0],
            "blocked_work_units": connection.execute(
                "SELECT count(*) FROM work_units WHERE status IN ('blocked','approval-required','failed','exhausted')"
            ).fetchone()[0],
            "audit_events": connection.execute(
                "SELECT count(*) FROM audit_events"
            ).fetchone()[0],
        }
        provider_effect_states = {
            "pending": 0,
            "executing": 0,
            "succeeded": 0,
            "failed": 0,
            "indeterminate": 0,
            "reconciled": 0,
        }
        for row in connection.execute(
            "SELECT status,count(*) AS count FROM effect_intents "
            "WHERE protocol_version=2 GROUP BY status"
        ):
            if row["status"] in provider_effect_states:
                provider_effect_states[row["status"]] = row["count"]
        summary["provider_effects"] = provider_effect_states
        exhausted = connection.execute(
            """SELECT count(*) FROM budgets WHERE
               (total_tokens IS NOT NULL AND consumed_tokens + reserved_tokens >= total_tokens)
               OR (total_attempts IS NOT NULL AND consumed_attempts >= total_attempts)
               OR (total_elapsed_ms IS NOT NULL AND consumed_elapsed_ms >= total_elapsed_ms)"""
        ).fetchone()[0]
        summary["goals_with_exhausted_budget"] = exhausted
        # Keep the aggregate keys at the top level for the stable Stage 1/2
        # status contract while also grouping them for newer clients.
        result: dict[str, Any] = {**summary, "summary": summary}
        if goal_id is None:
            return result

        goal = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None:
            raise StateError(f"Unknown goal: {goal_id}")
        contract = connection.execute(
            "SELECT envelope_sha256,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        budget = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
        unit_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status,count(*) AS count FROM work_units WHERE goal_id=? GROUP BY status",
                (goal_id,),
            )
        }
        live_elapsed = connection.execute(
            """SELECT COALESCE(sum(max(0,CAST((julianday(a.expires_at)-julianday(a.acquired_at))*86400000 AS INTEGER))),0)
               FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
               WHERE u.goal_id=? AND a.status='leased'""",
            (goal_id,),
        ).fetchone()[0]
        checkpoints: list[str] = []
        if contract is not None:
            checkpoints = json.loads(contract["contract"]).get("checkpoints", [])
        checkpoint_view: dict[str, Any] = {
            "next": checkpoints[0] if checkpoints else None,
            "reached": 0,
            "total": len(checkpoints),
        }
        if checkpoints and _table_exists(connection, "goal_checkpoints"):
            reached = connection.execute(
                "SELECT count(*) FROM goal_checkpoints WHERE goal_id=? AND status='reached'",
                (goal_id,),
            ).fetchone()[0]
            next_row = connection.execute(
                "SELECT checkpoint_id FROM goal_checkpoints WHERE goal_id=? AND status!='reached' ORDER BY position LIMIT 1",
                (goal_id,),
            ).fetchone()
            checkpoint_view = {
                "next": None if next_row is None else next_row[0],
                "reached": reached,
                "total": len(checkpoints),
            }
        now = _now()
        envelope = {} if contract is None else json.loads(contract["contract"])
        current_checkpoint = checkpoint_view["next"]
        candidate_query = "SELECT id,scope,checkpoint_id FROM work_units WHERE goal_id=? AND status IN ('planned','eligible','retry-wait')"
        candidate_params: list[Any] = [goal_id]
        if checkpoints:
            candidate_query += " AND checkpoint_id=?"
            candidate_params.append(current_checkpoint)
        approval_rows = connection.execute(
            """SELECT * FROM transition_approvals WHERE goal_id=? AND action='work-claim'
               AND decision='approved' AND revoked_at IS NULL AND valid_until>? AND effect='local-reversible-write'
               AND envelope_sha256=?""",
            (goal_id, now, contract["envelope_sha256"] if contract is not None else ""),
        ).fetchall()
        missing_claim_approvals = 0
        authorized_performers: set[str] = set()
        for unit in connection.execute(candidate_query, candidate_params):
            unit_scope = json.loads(unit["scope"])
            usable: set[str] = set()
            for approval in approval_rows:
                approval_scope = json.loads(approval["scope"])
                if approval["approver_id"] == approval["performer_id"]:
                    continue
                if approval["work_unit_id"] not in {None, unit["id"]}:
                    continue
                if not envelope or not StateStore._scope_within_contract(approval_scope, envelope["scope"]):
                    continue
                if not StateStore._scope_within_contract(unit_scope, approval_scope):
                    continue
                usable.add(approval["performer_id"])
            if not usable:
                missing_claim_approvals += 1
            authorized_performers.update(usable)
        goal_view: dict[str, Any] = {
            "id": goal_id,
            "status": goal["status"],
            "priority": goal["priority"],
            "envelope_sha256": None if contract is None else contract["envelope_sha256"],
            "budget": {
                "tokens_remaining": None if budget["total_tokens"] is None else max(0, budget["total_tokens"] - budget["consumed_tokens"] - budget["reserved_tokens"]),
                "attempts_remaining": None if budget["total_attempts"] is None else max(0, budget["total_attempts"] - budget["consumed_attempts"]),
                "elapsed_ms_remaining": None if budget["total_elapsed_ms"] is None else max(0, budget["total_elapsed_ms"] - budget["consumed_elapsed_ms"] - live_elapsed),
                "concurrency_available": None if budget["max_concurrency"] is None else max(0, budget["max_concurrency"] - unit_counts.get("leased", 0)),
            },
            "work_units": unit_counts,
            "checkpoint": checkpoint_view,
            "missing_claim_approvals": missing_claim_approvals,
            "authorized_claim_performers": sorted(authorized_performers)[:32],
            "workflow_evidence": connection.execute(
                "SELECT count(*) FROM workflow_evidence w JOIN work_units u ON u.id=w.work_unit_id WHERE u.goal_id=?",
                (goal_id,),
            ).fetchone()[0],
            "acceptance_evidence": connection.execute(
                "SELECT count(*) FROM acceptance_evidence WHERE goal_id=?", (goal_id,)
            ).fetchone()[0],
        }
        result["goal"] = goal_view
        if detail_limit:
            goal_view["units"] = [
                _row(row)
                for row in connection.execute(
                    """SELECT id,title,status,scope,checkpoint_id,attempt_count,last_outcome_class,lease_holder,lease_expires_at,updated_at
                       FROM work_units WHERE goal_id=? ORDER BY updated_at DESC,id LIMIT ?""",
                    (goal_id, detail_limit),
                )
            ]
        return result


def export_audit(
    store: StateStore, *, after_sequence: int = 0, limit: int = 50,
    goal_id: str | None = None,
) -> dict[str, Any]:
    """Export a bounded ordered audit slice with decoded, human-readable payloads."""
    limit = _limit(limit, maximum=MAX_AUDIT_EXPORT, label="limit")
    if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
        raise StateError("after_sequence must be a non-negative integer")
    if goal_id is not None:
        goal_id = _identifier(goal_id, label="goal_id")
    store._ensure()
    with store._connection(write=False) as connection:
        query = "SELECT * FROM audit_events WHERE sequence>?"
        params: list[Any] = [after_sequence]
        if goal_id is not None:
            query += " AND goal_id=?"
            params.append(goal_id)
        query += " ORDER BY sequence LIMIT ?"
        params.append(limit)
        events = []
        for row in connection.execute(query, params):
            events.append({
                "sequence": row["sequence"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "goal_id": row["goal_id"],
                "work_unit_id": row["work_unit_id"],
                "payload": json.loads(row["payload"]),
                "event_hash": row["event_hash"],
            })
        return {
            "after_sequence": after_sequence,
            "limit": limit,
            "goal_id": goal_id,
            "events": events,
            "next_sequence": None if not events else events[-1]["sequence"],
        }
