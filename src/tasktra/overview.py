"""Read-only, bounded portfolio views over Tasktra's durable ledger.

This module deliberately reports observations.  It does not infer that a
stored status grants authority to take any lifecycle action.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata

from .state import SCHEMA_VERSION, StateError, StateStore, _identifier


MAX_OVERVIEW_LIMIT = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SECRET = re.compile(
    r"(?i)((?:bearer\s+|(?:access[-_ ]?token|api[-_ ]?key|authorization|password|secret|cookie|credential|token)\s*[:=]\s*))\S+"
)
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_DISPLAY_TEXT = 500


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_OVERVIEW_LIMIT:
        raise StateError(f"limit must be between 1 and {MAX_OVERVIEW_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= _MAX_SQLITE_INTEGER:
        raise StateError(f"offset must be between 0 and {_MAX_SQLITE_INTEGER}")
    return limit, offset


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return fallback if value is None else json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _public_text(value: Any) -> str:
    """Return persisted display text without controls or credential-shaped values."""
    text = "".join(character for character in str(value) if unicodedata.category(character) not in {"Cc", "Cf"})
    return _SECRET.sub(r"\1[redacted]", _CONTROL.sub("", text))


def _lease_elapsed(acquired_at: str, expires_at: str) -> int:
    """Use the same exact timestamp arithmetic as the lease scheduler."""
    acquired = datetime.fromisoformat(acquired_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    return max(0, int((expires - acquired).total_seconds() * 1000))


def _remaining(total: int | None, used: int, reserved: int = 0) -> int | None:
    return None if total is None else max(0, int(total) - int(used) - int(reserved))


def _budget(row: sqlite3.Row | None, held_elapsed_ms: int, held_leases: int) -> tuple[dict[str, Any], bool]:
    if row is None:
        return {"present": False}, False
    total_tokens = row["total_tokens"]
    total_attempts = row["total_attempts"]
    total_elapsed_ms = row["total_elapsed_ms"]
    max_concurrency = row["max_concurrency"]
    consumed_tokens = int(row["consumed_tokens"])
    reserved_tokens = int(row["reserved_tokens"])
    consumed_attempts = int(row["consumed_attempts"])
    consumed_elapsed_ms = int(row["consumed_elapsed_ms"])
    exhausted = (
        (total_tokens is not None and consumed_tokens + reserved_tokens >= total_tokens)
        or (total_attempts is not None and consumed_attempts >= total_attempts)
        or (total_elapsed_ms is not None and consumed_elapsed_ms + held_elapsed_ms >= total_elapsed_ms)
    )
    return {
        "present": True,
        "tokens": {"total": total_tokens, "consumed": consumed_tokens, "reserved": reserved_tokens,
                   "remaining": _remaining(total_tokens, consumed_tokens, reserved_tokens)},
        "attempts": {"total": total_attempts, "consumed": consumed_attempts,
                     "remaining": _remaining(total_attempts, consumed_attempts)},
        "elapsed_ms": {"total": total_elapsed_ms, "consumed": consumed_elapsed_ms,
                       "reserved_held": held_elapsed_ms,
                       "remaining": _remaining(total_elapsed_ms, consumed_elapsed_ms, held_elapsed_ms)},
        "concurrency": {"maximum": max_concurrency, "occupied": held_leases,
                        "available": _remaining(max_concurrency, held_leases)},
        "exhausted": exhausted,
    }, exhausted


def _attention(
    *, goal: sqlite3.Row, contract: dict[str, Any] | None, dependencies: list[dict[str, str]],
    work_statuses: Counter[str], expired_leases: int, exhausted: bool, provider_effects: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    attention: list[dict[str, Any]] = []
    recommendations: list[dict[str, str]] = [{
        "kind": "read-only-command", "command": f"tasktra status --goal-id {goal['id']}",
    }]
    if contract is None:
        attention.append({"code": "missing-contract", "detail": "No stored authority contract."})
        recommendations.append({"kind": "advisory", "detail": "Review and define the required authority contract before lifecycle work."})
    if goal["status"] in {"planned", "draining", "paused", "stopped", "blocked"}:
        attention.append({"code": f"goal-{goal['status']}", "detail": f"Stored goal status is {goal['status']}."})
    waiting = [dependency["id"] for dependency in dependencies if dependency["status"] != "complete"]
    if waiting:
        attention.append({"code": "dependency-waiting", "count": len(waiting), "detail": "Dependencies are not complete."})
    if expired_leases:
        attention.append({"code": "expired-lease-recovery", "count": expired_leases, "detail": "Stored leases have expired and need lifecycle review."})
    for status in ("blocked", "failed", "approval-required", "exhausted"):
        if work_statuses[status]:
            attention.append({"code": f"work-{status}", "count": work_statuses[status], "detail": f"Work units are stored as {status}."})
    if exhausted:
        attention.append({"code": "budget-exhausted", "detail": "A recorded budget limit is exhausted."})
    for status in ("indeterminate", "reconciliation-required", "failed"):
        if provider_effects.get(status, 0):
            attention.append({"code": f"provider-effect-{status}", "count": provider_effects[status],
                              "detail": f"Provider effects are stored as {status}."})
            recommendations.append({"kind": "advisory", "detail": "Investigate the recorded provider effect state before any further provider action."})
    if not sum(work_statuses.values()):
        attention.append({"code": "missing-work", "detail": "No work units are stored for this goal."})
        recommendations.append({"kind": "advisory", "detail": "Review whether a bounded work unit should be proposed under the goal's authority."})
    return attention, recommendations


def _goal_view(
    connection: sqlite3.Connection, goal: sqlite3.Row, *, now: str,
    include_units: bool, limit: int, offset: int,
) -> dict[str, Any]:
    goal_id = str(goal["id"])
    contract_row = connection.execute(
        "SELECT version,contract FROM goal_contracts WHERE goal_id=?", (goal_id,)
    ).fetchone()
    if contract_row is None:
        contract = None
    else:
        try:
            from .authority import validate_authority_envelope
            decoded_contract = json.loads(contract_row["contract"])
            contract = validate_authority_envelope(decoded_contract)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateError(f"stored authority contract is invalid for goal {goal_id}") from error
    dependencies: list[dict[str, str]] = []
    checkpoints: list[dict[str, Any]] = []
    criteria_total: int | None = None
    if contract is not None:
        criteria = contract.get("acceptance_criteria", [])
        criteria_total = len(criteria) if isinstance(criteria, list) else 0
        declared_dependencies = contract.get("dependencies", [])
        if isinstance(declared_dependencies, list):
            for dependency_id in declared_dependencies:
                row = connection.execute("SELECT status FROM goals WHERE id=?", (dependency_id,)).fetchone()
                dependencies.append({"id": str(dependency_id), "status": "missing" if row is None else str(row["status"])})
        for row in connection.execute(
            "SELECT checkpoint_id,position,status,reached_at FROM goal_checkpoints WHERE goal_id=? ORDER BY position", (goal_id,)
        ):
            checkpoints.append({"id": row["checkpoint_id"], "position": row["position"], "status": row["status"], "reached_at": row["reached_at"]})
    work_rows = connection.execute(
        "SELECT status,count(*) AS count FROM work_units WHERE goal_id=? GROUP BY status", (goal_id,)
    ).fetchall()
    work_statuses = Counter({str(row["status"]): int(row["count"]) for row in work_rows})
    total_work = sum(work_statuses.values())
    lease_summary = connection.execute(
        """SELECT count(*) AS held, COALESCE(sum(CASE WHEN a.expires_at>? THEN 1 ELSE 0 END),0) AS live,
                  COALESCE(sum(tasktra_lease_elapsed(a.acquired_at,a.expires_at)),0) AS held_elapsed
           FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
           WHERE u.goal_id=? AND a.status='leased'""", (now, goal_id),
    ).fetchone()
    held_leases = int(lease_summary["held"])
    live = int(lease_summary["live"])
    expired = held_leases - live
    held_elapsed = int(lease_summary["held_elapsed"])
    budget_row = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
    budget, exhausted = _budget(budget_row, held_elapsed, held_leases)
    recorded_evidence = connection.execute(
        "SELECT count(*) FROM acceptance_evidence WHERE goal_id=?", (goal_id,)
    ).fetchone()[0]
    provider_effects = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status,count(*) AS count FROM effect_intents WHERE goal_id=? GROUP BY status", (goal_id,)
        )
    }
    attention, recommendations = _attention(
        goal=goal, contract=contract, dependencies=dependencies, work_statuses=work_statuses,
        expired_leases=expired, exhausted=exhausted, provider_effects=provider_effects,
    )
    result: dict[str, Any] = {
        "id": goal_id, "title": _public_text(goal["title"]), "status": goal["status"], "priority": goal["priority"],
        "contract": {"present": contract is not None, "version": None if contract_row is None else contract_row["version"]},
        "progress": {
            "work": {"complete": work_statuses["complete"], "total": total_work, "by_status": dict(sorted(work_statuses.items()))},
            "acceptance": {"evidence": int(recorded_evidence), "criteria": criteria_total},
        },
        "budget": budget,
        "dependencies": dependencies,
        "checkpoints": checkpoints,
        "leases": {"stored_leased": held_leases, "live": live, "expired": expired},
        "intake": {"accepting_claims": goal["status"] == "active", "draining": goal["status"] == "draining"},
        "attention": attention,
        "recommendations": recommendations,
        "provider_effects": provider_effects,
    }
    if include_units:
        units = connection.execute(
            """SELECT u.id,u.title,u.status,u.checkpoint_id,u.attempt_count,u.last_outcome_class,u.lease_expires_at,u.updated_at,
                      a.owner_id AS lease_owner,a.attempt_no AS lease_attempt_no
               FROM work_units u LEFT JOIN work_attempts a ON a.id=u.current_attempt_id
               WHERE u.goal_id=? ORDER BY u.id LIMIT ? OFFSET ?""", (goal_id, limit, offset),
        ).fetchall()
        result["work_units"] = [{
            "id": row["id"], "title": _public_text(row["title"]), "status": row["status"],
            "checkpoint_id": row["checkpoint_id"], "attempt_count": row["attempt_count"],
            "last_outcome_class": row["last_outcome_class"], "lease": None if row["lease_expires_at"] is None else {
                "state": "live" if str(row["lease_expires_at"]) > now else "expired", "expires_at": row["lease_expires_at"],
                "owner": row["lease_owner"], "attempt_no": row["lease_attempt_no"],
            }, "updated_at": row["updated_at"],
        } for row in units]
    return result


def _aggregates(connection: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    goal_statuses = {str(row["status"]): int(row["count"]) for row in connection.execute("SELECT status,count(*) AS count FROM goals GROUP BY status")}
    work_statuses = {str(row["status"]): int(row["count"]) for row in connection.execute("SELECT status,count(*) AS count FROM work_units GROUP BY status")}
    leases = connection.execute(
        "SELECT count(*) AS held,COALESCE(sum(CASE WHEN expires_at>? THEN 1 ELSE 0 END),0) AS live FROM work_attempts WHERE status='leased'", (now,)
    ).fetchone()
    exhausted = connection.execute(
        """SELECT count(*) FROM budgets WHERE
           (total_tokens IS NOT NULL AND consumed_tokens + reserved_tokens >= total_tokens)
           OR (total_attempts IS NOT NULL AND consumed_attempts >= total_attempts)
           OR (total_elapsed_ms IS NOT NULL AND consumed_elapsed_ms + COALESCE((
               SELECT sum(tasktra_lease_elapsed(a.acquired_at,a.expires_at))
               FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
               WHERE u.goal_id=budgets.goal_id AND a.status='leased'
           ),0) >= total_elapsed_ms)"""
    ).fetchone()[0]
    return {"goals": {"total": sum(goal_statuses.values()), "by_status": dict(sorted(goal_statuses.items()))},
            "work_units": {"total": sum(work_statuses.values()), "by_status": dict(sorted(work_statuses.items()))},
            "leases": {"stored_leased": int(leases["held"]), "live": int(leases["live"]), "expired": int(leases["held"] - leases["live"])},
            "budgets": {"exhausted_goals": int(exhausted)}}


def _overview_in_transaction(
    connection: sqlite3.Connection, *, capture_timestamp: str, goal_id: str | None,
    limit: int, offset: int,
) -> dict[str, Any]:
    """Build the overview projection using an already-open consistent snapshot.

    This is deliberately connection-scoped so other read-only projections can
    reuse the established overview field semantics without opening a second
    SQLite snapshot.
    """
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < SCHEMA_VERSION:
        raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
    if version > SCHEMA_VERSION:
        raise StateError(f"runtime schema {version} requires a compatible Tasktra build")
    aggregates = _aggregates(connection, now=capture_timestamp)
    control = connection.execute("SELECT emergency_stopped,reason,set_at FROM runtime_control WHERE id=1").fetchone()
    runtime = {"emergency_stop": {"active": bool(control["emergency_stopped"]),
                                  "reason": _public_text(control["reason"]) if control["reason"] else None,
                                  "set_at": control["set_at"]}}
    if goal_id is None:
        total = int(connection.execute("SELECT count(*) FROM goals").fetchone()[0])
        rows = connection.execute(
            "SELECT * FROM goals ORDER BY priority DESC,id ASC LIMIT ? OFFSET ?", (limit, offset),
        ).fetchall()
        goals = [_goal_view(connection, row, now=capture_timestamp, include_units=False, limit=limit, offset=offset) for row in rows]
        return {"schema_version": version, "filter": {"goal_id": None},
                "pagination": {"scope": "goals", "limit": limit, "offset": offset, "total": total,
                               "next_offset": None if offset + len(goals) >= total else offset + len(goals)},
                "aggregates": aggregates, "runtime": runtime, "goals": goals, "goal": None}
    goal = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if goal is None:
        raise StateError(f"Unknown goal: {goal_id}")
    total = int(connection.execute("SELECT count(*) FROM work_units WHERE goal_id=?", (goal_id,)).fetchone()[0])
    detail = _goal_view(connection, goal, now=capture_timestamp, include_units=True, limit=limit, offset=offset)
    unit_count = len(detail["work_units"])
    return {"schema_version": version, "filter": {"goal_id": goal_id},
            "pagination": {"scope": "work_units", "limit": limit, "offset": offset, "total": total,
                           "next_offset": None if offset + unit_count >= total else offset + unit_count},
            "aggregates": aggregates, "runtime": runtime, "goals": [], "goal": detail}


def orchestration_overview(
    store: StateStore, *, goal_id: str | None = None, limit: int = 20, offset: int = 0,
) -> dict[str, Any]:
    """Return a consistent, bounded, non-mutating orchestration overview."""
    limit, offset = _bounds(limit, offset)
    if goal_id is not None:
        goal_id = _identifier(goal_id, label="goal_id")
    if not store.path.is_file():
        raise StateError(f"Runtime database does not exist: {store.path}")
    try:
        disk_version = store.inspect_schema_version()
    except sqlite3.Error as error:
        raise StateError(f"unable to read runtime database: {error}") from error
    if disk_version < SCHEMA_VERSION:
        raise StateError(f"runtime schema {disk_version} requires migration to {SCHEMA_VERSION}")
    if disk_version > SCHEMA_VERSION:
        raise StateError(f"runtime schema {disk_version} requires a compatible Tasktra build")
    path = Path(store.path).resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(path, uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise StateError(f"unable to read runtime database: {error}") from error
    connection.row_factory = sqlite3.Row
    connection.create_function("tasktra_lease_elapsed", 2, _lease_elapsed)
    try:
        connection.execute("BEGIN")
        # Verify within this snapshot, before any execution state influences
        # attention or capacity. The cockpit verifies its own shared snapshot.
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
        if version > SCHEMA_VERSION:
            raise StateError(f"runtime schema {version} requires a compatible Tasktra build")
        store._assert_audit_chain_in_transaction(connection)
        store._assert_current_state_integrity_in_transaction(connection)
        result = _overview_in_transaction(
            connection, capture_timestamp=_now(), goal_id=goal_id, limit=limit, offset=offset,
        )
        # The cockpit consumes the shared builder's v1 shape. Enrich only the
        # interactive overview while keeping all its reads in this transaction.
        from .intervention_views import intervention_counts
        from .execution_recovery import execution_attention_counts
        result["aggregates"]["interventions"] = intervention_counts(connection, include_closed=False)
        result["aggregates"]["codex_runs"] = execution_attention_counts(connection)
        selected = result["goals"] if result["goal"] is None else [result["goal"]]
        for goal in selected:
            execution = execution_attention_counts(connection, goal_id=goal["id"])
            goal["codex_runs"] = execution
            # Leased attempts retain their slot after expiry until explicit
            # recovery ends the lease. Liveness remains a separate observation.
            held = goal["leases"]["stored_leased"]
            goal["execution_capacity"] = {
                "active_leases": held,
                "detached_unresolved_runs": execution["detached_capacity"],
                "effective_occupancy": held + execution["detached_capacity"],
                "maximum": goal["budget"]["concurrency"]["maximum"] if goal["budget"]["present"] else None,
            }
            for key, code, detail in (
                ("prepared_unobserved", "codex-run-prepared-unobserved", "Prepared Codex runs have no recorded host identity."),
                ("started_unterminated", "codex-run-started-unterminated", "Started Codex workers have no terminal receipt."),
            ):
                if execution[key]:
                    goal["attention"].append({"code": code, "count": execution[key], "detail": detail})
            if execution["unresolved"]:
                goal["recommendations"].append({
                    "kind": "read-only-command",
                    "argv": ["tasktra", "delegation", "unresolved", "--goal-id", goal["id"]],
                    "detail": "Inspect unresolved Codex workers.",
                })
            counts = intervention_counts(connection, goal_id=goal["id"], include_closed=False)
            goal["interventions"] = counts
            for state in ("open", "answered", "declined", "cancelled"):
                if counts[state]:
                    goal["attention"].append({"code": f"intervention-{state}", "count": counts[state],
                                              "detail": f"Intervention requests have response state {state}."})
            if any(counts[state] for state in ("open", "answered", "declined", "cancelled")):
                goal["recommendations"].append({"kind": "read-only-command",
                                                "command": f"tasktra intervention list --goal-id {goal['id']}"})
        return result
    except sqlite3.Error as error:
        raise StateError(f"unable to read runtime database: {error}") from error
    finally:
        connection.rollback()
        connection.close()


def _display(value: Any) -> str:
    """Make persisted text safe for a terminal without exposing secret-shaped values."""
    text = _public_text(value)
    return text if len(text) <= _MAX_DISPLAY_TEXT else text[:_MAX_DISPLAY_TEXT - 3] + "..."


def _budget_display(value: int | None, *, uncapped: bool = False) -> str:
    if value is None:
        return "uncapped" if uncapped else "not set"
    return str(value)


def format_overview(report: dict[str, Any]) -> str:
    """Render a deliberately compact terminal view of an overview report."""
    aggregates = report["aggregates"]
    project = report.get("project")
    lines: list[str] = []
    if isinstance(project, dict) and project.get("name") and project.get("root"):
        lines.append(f"Project: {_display(project['name'])} ({_display(project['root'])})")
    lines.extend([
        f"Goals: {aggregates['goals']['total']}  Work units: {aggregates['work_units']['total']}",
        f"Leases: {aggregates['leases']['live']} live, {aggregates['leases']['expired']} expired",
    ])
    if "codex_runs" in aggregates:
        runs = aggregates["codex_runs"]
        lines.append(f"Codex runs: {runs['unresolved']} unresolved, {runs['detached_capacity']} detached capacity slots")
    emergency_stop = report.get("runtime", {}).get("emergency_stop", {})
    if emergency_stop.get("active"):
        lines.append(f"ATTENTION: runtime emergency stop active: {_display(emergency_stop.get('reason', ''))}")
    if report["goal"] is not None:
        goals = [report["goal"]]
    else:
        goals = report["goals"]
    for goal in goals:
        progress = goal["progress"]
        criteria = "?" if progress["acceptance"]["criteria"] is None else progress["acceptance"]["criteria"]
        lines.append(f"{_display(goal['id'])} [{_display(goal['status'])}] {_display(goal['title'])} - work {progress['work']['complete']}/{progress['work']['total']}, acceptance {progress['acceptance']['evidence']}/{criteria}")
        if goal.get("intake", {}).get("draining"):
            lines.append(f"  intake: closed; {goal['leases']['stored_leased']} current leases to finish or recover")
        if "execution_capacity" in goal:
            capacity = goal["execution_capacity"]
            lines.append(
                f"  execution capacity: {capacity['effective_occupancy']}/{_budget_display(capacity['maximum'], uncapped=True)}, "
                f"{capacity['active_leases']} stored leases, {capacity['detached_unresolved_runs']} detached workers"
            )
        budget = goal["budget"]
        if budget["present"]:
            lines.append(
                "  budget: "
                f"tokens remaining {_budget_display(budget['tokens']['remaining'], uncapped=True)}, "
                f"attempts remaining {_budget_display(budget['attempts']['remaining'])}, "
                f"elapsed ms remaining {_budget_display(budget['elapsed_ms']['remaining'])}, "
                f"reserved tokens {_display(budget['tokens']['reserved'])}"
            )
        waiting = [item for item in goal["dependencies"] if item["status"] != "complete"]
        if waiting:
            shown = ", ".join(f"{_display(item['id'])} ({_display(item['status'])})" for item in waiting[:5])
            suffix = "" if len(waiting) <= 5 else ", ..."
            lines.append(f"  waiting dependencies: {shown}{suffix}")
        checkpoints = goal["checkpoints"]
        if checkpoints:
            reached = sum(item["status"] == "reached" for item in checkpoints)
            next_checkpoint = next((item["id"] for item in checkpoints if item["status"] != "reached"), None)
            lines.append(f"  checkpoints: {reached}/{len(checkpoints)} reached; next {_display(next_checkpoint)}")
        for item in goal["attention"]:
            lines.append(f"  attention: {_display(item['code'])}: {_display(item['detail'])}")
        if goal["attention"] and goal["recommendations"]:
            recommendation = goal["recommendations"][0]
            hint = recommendation.get("command", recommendation.get("detail", "Review the stored state."))
            lines.append(f"  next: {_display(hint)}")
        for unit in goal.get("work_units", []):
            lease = "" if unit["lease"] is None else (
                f", lease {_display(unit['lease']['state'])}"
                f" by {_display(unit['lease'].get('owner'))} attempt {_display(unit['lease'].get('attempt_no'))}"
            )
            lines.append(f"  {_display(unit['id'])} [{_display(unit['status'])}{lease}] {_display(unit['title'])}")
    page = report["pagination"]
    lines.append(f"Showing {page['scope']} offset {page['offset']} limit {page['limit']} of {page['total']}.")
    return "\n".join(lines) + "\n"
