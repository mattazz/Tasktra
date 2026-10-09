"""Verified, observational execution-readiness projection for one goal.

The report deliberately separates residual dependency structure from stored
operational gates.  It never evaluates authority or claimability.
"""

from __future__ import annotations

from collections import Counter, deque
import json
import sqlite3
from typing import Any

from .execution_recovery import execution_attention_counts
from .intervention_views import intervention_counts
from .overview import _lease_elapsed, _remaining
from .state import SCHEMA_VERSION, StateError, StateStore, _identifier


_TERMINAL_ATTENTION = {"blocked", "approval-required", "failed", "exhausted", "stopped"}
_STATUS_ORDER = (
    "approval-required", "blocked", "complete", "eligible", "exhausted", "failed",
    "leased", "paused", "planned", "retry-wait", "stopped",
)
_NOTICE = (
    "Structural readiness is not authorization, priority, or a schedule; use work explain "
    "with actor and authority inputs before claiming work."
)


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise StateError("limit must be an integer from 1 to 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
        raise StateError("offset must be an integer from 0 to 1000000")
    return limit, offset


def _page(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    selected = items[offset:offset + limit]
    return {
        "items": selected,
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(selected) if offset + len(selected) < len(items) else None,
    }


def _unit_page(rows: list[dict[str, Any]], *, goal_id: str, limit: int, offset: int) -> dict[str, Any]:
    """Page unit facts before adding bounded per-row drilldown arrays."""
    selected = rows[offset:offset + limit]
    return {
        "items": [dict(row, drilldowns=_drilldowns(goal_id, row["work_unit_id"], row["terminal_attention"])) for row in selected],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(selected) if offset + len(selected) < len(rows) else None,
    }


def _category(status: str, ready: bool) -> tuple[str, str]:
    if status == "complete":
        return "complete", "unit.complete"
    if status == "leased":
        return "leased", "candidate.lease_held"
    if status in _TERMINAL_ATTENTION:
        return "terminal_attention", "candidate.status_not_claimable"
    if ready:
        return "structurally_ready", "structure.ready"
    return "structurally_waiting", "candidate.prerequisites_incomplete"


def _drilldowns(goal_id: str, unit_id: str, terminal_attention: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dependencies_argv": [
            "tasktra", "work", "dependencies", goal_id, "--work-unit-id", unit_id,
            "--limit", "1", "--offset", "0",
        ],
        "impact_argv": [
            "tasktra", "work", "impact", goal_id, unit_id, "--direction", "both",
            "--limit", "20", "--offset", "0",
        ],
        "intervention_argv": None,
        "explain_argv_template": [
            "tasktra", "work", "explain", goal_id, "--actor", "{performer_id}",
            "--envelope-sha256", "{envelope_sha256}", "--lease-seconds", "{lease_seconds}",
            "--token-reservation", "{token_reservation}", "--work-unit-id", unit_id,
            "--limit", "1", "--offset", "0",
        ],
    }
    if terminal_attention:
        result["intervention_argv"] = [
            "tasktra", "intervention", "list", "--goal-id", goal_id,
            "--work-unit-id", unit_id, "--limit", "20", "--offset", "0",
        ]
    return result


def _read_contract(connection: sqlite3.Connection, goal_id: str) -> tuple[bool, dict[str, Any]]:
    row = connection.execute("SELECT contract FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
    if row is None:
        return False, {}
    try:
        value = json.loads(row["contract"])
    except (TypeError, json.JSONDecodeError) as error:
        raise StateError(f"stored authority contract is invalid for goal {goal_id}") from error
    if not isinstance(value, dict):
        raise StateError(f"stored authority contract is invalid for goal {goal_id}")
    return True, value


def _operational_gates(
    connection: sqlite3.Connection, *, goal_id: str, graph: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return stored operational facts from the caller's verified snapshot."""
    goal = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
    assert goal is not None
    contract_present, contract = _read_contract(connection, goal_id)
    lifecycle_codes: list[str] = []
    if goal["status"] == "draining":
        lifecycle_codes.append("goal.intake_draining")
    elif goal["status"] != "active":
        lifecycle_codes.append("goal.lifecycle_not_active")

    dependencies = contract.get("dependencies", []) if contract_present else []
    if not isinstance(dependencies, list):
        raise StateError(f"stored authority contract is invalid for goal {goal_id}")
    incomplete_ids: list[str] = []
    for dependency_id in dependencies:
        row = connection.execute("SELECT status FROM goals WHERE id=?", (dependency_id,)).fetchone()
        if row is None or row["status"] != "complete":
            incomplete_ids.append(str(dependency_id))
    incomplete_ids.sort()
    dependency_codes = ["goal.dependencies_incomplete"] if incomplete_ids else []

    checkpoint_rows = connection.execute(
        "SELECT checkpoint_id,position,status FROM goal_checkpoints WHERE goal_id=? ORDER BY position", (goal_id,),
    ).fetchall()
    current = next((str(row["checkpoint_id"]) for row in checkpoint_rows if row["status"] != "reached"), None)
    checkpoint_codes = ["goal.checkpoints_complete"] if checkpoint_rows and current is None else []
    checkpoint_summaries: list[dict[str, Any]] = []
    for row in checkpoint_rows:
        checkpoint_id = str(row["checkpoint_id"])
        # The caller fills residual values after synthesis; retain stable base data.
        checkpoint_summaries.append({
            "checkpoint_id": checkpoint_id, "position": int(row["position"]), "status": str(row["status"]),
            "units_total": 0, "complete_total": 0,
            "remaining_total": 0, "structural_ready_total": 0, "leased_total": 0,
            "terminal_attention_total": 0, "minimum_remaining_wave": None, "maximum_remaining_wave": None,
        })
    checkpoint_by_id = {item["checkpoint_id"]: item for item in checkpoint_summaries}
    for unit in graph.values():
        if unit["checkpoint_id"] in checkpoint_by_id:
            summary = checkpoint_by_id[unit["checkpoint_id"]]
            summary["units_total"] += 1
            summary["complete_total" if unit["status"] == "complete" else "remaining_total"] += 1

    lease = connection.execute(
        """SELECT count(*) AS held,
                  COALESCE(sum(tasktra_lease_elapsed(a.acquired_at,a.expires_at)),0) AS held_elapsed
             FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id
             WHERE u.goal_id=? AND a.status='leased'""", (goal_id,),
    ).fetchone()
    held = int(lease["held"])
    held_elapsed = int(lease["held_elapsed"])
    budget_row = connection.execute("SELECT * FROM budgets WHERE goal_id=?", (goal_id,)).fetchone()
    if budget_row is None:
        budget = {
            "present": False, "execution_budgets_configured": False,
            "tokens": None, "attempts": None, "elapsed_ms": None,
            "fully_allocated_dimensions": [], "selection_reason_codes": ["goal.budgets_missing"],
        }
        maximum = None
    else:
        totals = {
            "tokens": budget_row["total_tokens"], "attempts": budget_row["total_attempts"],
            "elapsed_ms": budget_row["total_elapsed_ms"],
        }
        budget = {
            "present": True,
            "execution_budgets_configured": budget_row["max_concurrency"] is not None and totals["attempts"] is not None,
            "tokens": {"total": totals["tokens"], "consumed": int(budget_row["consumed_tokens"]),
                       "reserved": int(budget_row["reserved_tokens"]),
                       "remaining": _remaining(totals["tokens"], int(budget_row["consumed_tokens"]), int(budget_row["reserved_tokens"]))},
            "attempts": {"total": totals["attempts"], "consumed": int(budget_row["consumed_attempts"]),
                         "remaining": _remaining(totals["attempts"], int(budget_row["consumed_attempts"]))},
            "elapsed_ms": {"total": totals["elapsed_ms"], "consumed": int(budget_row["consumed_elapsed_ms"]),
                           "reserved_held": held_elapsed,
                           "remaining": _remaining(totals["elapsed_ms"], int(budget_row["consumed_elapsed_ms"]), held_elapsed)},
            "fully_allocated_dimensions": [], "selection_reason_codes": [],
        }
        if not budget["execution_budgets_configured"]:
            budget["selection_reason_codes"].append("goal.budgets_missing")
        if budget["attempts"]["total"] is not None and budget["attempts"]["consumed"] >= budget["attempts"]["total"]:
            budget["selection_reason_codes"].append("budget.attempts_exhausted")
        if budget["elapsed_ms"]["total"] is not None and budget["elapsed_ms"]["consumed"] + held_elapsed >= budget["elapsed_ms"]["total"]:
            budget["selection_reason_codes"].append("budget.elapsed_exhausted")
        for dimension in ("attempts", "elapsed_ms", "tokens"):
            if budget[dimension]["total"] is not None and budget[dimension]["remaining"] == 0:
                budget["fully_allocated_dimensions"].append(dimension)
        maximum = budget_row["max_concurrency"]

    unresolved = execution_attention_counts(connection, goal_id)
    effective = held + unresolved["detached_capacity"]
    capacity_codes = ["budget.concurrency_exhausted"] if maximum is not None and effective >= int(maximum) else []
    control = connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()
    active = bool(control["emergency_stopped"])
    interventions = intervention_counts(connection, goal_id=goal_id, include_closed=False, include_legacy=True)
    intervention_codes = [f"intervention.{key}" for key in ("open", "answered", "declined", "cancelled", "legacy") if interventions[key]]
    unresolved_codes = [
        f"execution.{key}" for key in ("prepared_unobserved", "started_unterminated", "detached_capacity")
        if unresolved[key]
    ]
    return {
        "goal_lifecycle": {"status": str(goal["status"]), "selection_reason_codes": lifecycle_codes},
        "goal_dependencies": {"dependencies_total": len(dependencies), "incomplete_total": len(incomplete_ids),
                              "incomplete_ids": incomplete_ids, "selection_reason_codes": dependency_codes},
        "checkpoints": {"present": bool(checkpoint_rows), "total": len(checkpoint_rows),
                        "reached": sum(row["status"] == "reached" for row in checkpoint_rows),
                        "current_checkpoint_id": current, "selection_reason_codes": checkpoint_codes},
        "budget": budget,
        "capacity": {"stored_leased_attempts": held, "detached_unresolved_runs": unresolved["detached_capacity"],
                     "effective_occupancy": effective, "maximum": maximum,
                     "available": None if maximum is None else max(0, int(maximum) - effective),
                     "selection_reason_codes": capacity_codes},
        "emergency_stop": {"active": active, "selection_reason_codes": ["runtime.emergency_stopped"] if active else []},
        "interventions": {"counts": interventions, "attention_reason_codes": intervention_codes,
                          "inspection_argv": ["tasktra", "intervention", "list", "--goal-id", goal_id, "--limit", "50", "--offset", "0"]},
        "unresolved_runs": {"counts": unresolved, "attention_reason_codes": unresolved_codes,
                            "inspection_argv": ["tasktra", "delegation", "unresolved", "--goal-id", goal_id, "--limit", "50"]},
        "authority_contract": {"present": contract_present,
                               "selection_reason_codes": [] if contract_present else ["goal.contract_missing"]},
    }, checkpoint_summaries


def _synthesize(goal_id: str, graph: dict[str, dict[str, Any]], *, limit: int, offset: int,
                checkpoint_summaries: list[dict[str, Any]], include_rows: bool = False) -> tuple[Any, ...]:
    """Build the residual DAG facts in iterative, bounded passes."""
    checkpoint_summaries = [dict(item) for item in checkpoint_summaries]
    ids = list(graph)
    residual = {unit_id for unit_id, unit in graph.items() if unit["status"] != "complete"}
    prerequisites = {unit_id: [item["id"] for item in unit["prerequisites"]] for unit_id, unit in graph.items()}
    dependents: dict[str, list[str]] = {unit_id: [] for unit_id in graph}
    for dependent, parents in prerequisites.items():
        for parent in parents:
            dependents[parent].append(dependent)
    residual_parents = {unit_id: [parent for parent in prerequisites[unit_id] if parent in residual] for unit_id in residual}
    residual_children = {unit_id: [child for child in dependents[unit_id] if child in residual] for unit_id in residual}
    indegree = {unit_id: len(parents) for unit_id, parents in residual_parents.items()}
    wave = {unit_id: 0 for unit_id in residual}
    queue = deque(unit_id for unit_id, degree in indegree.items() if degree == 0)
    topological: list[str] = []
    while queue:
        unit_id = queue.popleft()
        topological.append(unit_id)
        for child in residual_children[unit_id]:
            wave[child] = max(wave[child], wave[unit_id] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    # The shared loader already rejects cycles; retain a defensive fail-closed check.
    if len(topological) != len(residual):
        raise StateError("work unit dependency graph contains a cycle")
    depth = {unit_id: 1 for unit_id in residual}
    for unit_id in reversed(topological):
        if residual_children[unit_id]:
            depth[unit_id] = 1 + max(depth[child] for child in residual_children[unit_id])
    maximum_depth = max(depth.values(), default=0)
    direct_gates = dict.fromkeys(ids, 0)
    remaining_direct_gates = dict.fromkeys(ids, 0)
    for dependent, parents in prerequisites.items():
        incomplete = [parent for parent in parents if parent in residual]
        if len(incomplete) == 1:
            direct_gates[incomplete[0]] += 1
            if dependent in residual:
                remaining_direct_gates[incomplete[0]] += 1

    categories = Counter()
    statuses = Counter()
    rows: dict[str, dict[str, Any]] = {}
    ready_ids: list[str] = []
    blocking_ids: list[str] = []
    for unit_id in ids:
        unit = graph[unit_id]
        status = unit["status"]
        ready = bool(unit["ready"])
        category, category_code = _category(status, ready)
        categories[category] += 1
        statuses[status] += 1
        if unit_id not in residual:
            continue
        terminal = status in _TERMINAL_ATTENTION
        incomplete_parents = sorted(residual_parents[unit_id])
        blocking = bool(residual_children[unit_id]) or terminal
        memberships = ([] if not blocking else ["blocking"]) + ([] if not ready else ["ready"])
        memberships.sort()
        # A unit's checkpoint may be structurally ready before its contract position is current.
        # Current checkpoint is supplied via the closed checkpoint summaries later in this function.
        rows[unit_id] = {
            "work_unit_id": unit_id, "status": status, "checkpoint_id": unit["checkpoint_id"],
            "category": category, "category_reason_code": category_code, "structural_ready": ready,
            "incomplete_direct_prerequisite_ids": incomplete_parents, "remaining_wave": wave[unit_id],
            "downstream_structural_depth": depth[unit_id],
            "deepest_remaining_branch": wave[unit_id] + depth[unit_id] == maximum_depth,
            "incomplete_direct_dependents_count": len(residual_children[unit_id]),
            "direct_prerequisite_gates_cleared_if_completed": direct_gates[unit_id],
            "remaining_direct_prerequisite_gates_cleared_if_completed": remaining_direct_gates[unit_id],
            "terminal_attention": terminal, "frontier_memberships": memberships,
            "operational_reason_codes": [],
        }
        if ready:
            ready_ids.append(unit_id)
        if blocking:
            blocking_ids.append(unit_id)

    current_checkpoint = next((item["checkpoint_id"] for item in checkpoint_summaries if item["status"] != "reached"), None)
    checkpoints_present = bool(checkpoint_summaries)
    for unit_id, row in rows.items():
        if (checkpoints_present and row["checkpoint_id"] != current_checkpoint) or (not checkpoints_present and row["checkpoint_id"] is not None):
            row["operational_reason_codes"] = ["candidate.checkpoint_not_current"]
    blocking_set = set(blocking_ids)
    by_wave: dict[int, list[str]] = {}
    for unit_id in residual:
        by_wave.setdefault(wave[unit_id], []).append(unit_id)
    wave_rows: list[dict[str, Any]] = []
    for wave_number in range(maximum_depth):
        members = by_wave[wave_number]
        bucket_categories = Counter(_category(graph[unit_id]["status"], graph[unit_id]["ready"])[0] for unit_id in members)
        wave_rows.append({
            "remaining_wave": wave_number, "units_total": len(members),
            "by_category": {name: bucket_categories[name] for name in ("complete", "leased", "terminal_attention", "structurally_ready", "structurally_waiting")},
            "blocking_frontier_total": sum(unit_id in blocking_set for unit_id in members),
            "direct_gates_clearable_total": sum(direct_gates[unit_id] for unit_id in members),
            "remaining_direct_gates_clearable_total": sum(remaining_direct_gates[unit_id] for unit_id in members),
        })
    checkpoint_by_id = {item["checkpoint_id"]: item for item in checkpoint_summaries}
    for unit_id in residual:
        unit = graph[unit_id]
        if unit["checkpoint_id"] not in checkpoint_by_id:
            continue
        summary = checkpoint_by_id[unit["checkpoint_id"]]
        summary["structural_ready_total"] += int(unit["ready"])
        summary["leased_total"] += int(unit["status"] == "leased")
        summary["terminal_attention_total"] += int(unit["status"] in _TERMINAL_ATTENTION)
        if summary["minimum_remaining_wave"] is None or wave[unit_id] < summary["minimum_remaining_wave"]:
            summary["minimum_remaining_wave"] = wave[unit_id]
        if summary["maximum_remaining_wave"] is None or wave[unit_id] > summary["maximum_remaining_wave"]:
            summary["maximum_remaining_wave"] = wave[unit_id]
    ready_rows = [rows[unit_id] for unit_id in sorted(ready_ids)]
    blocking_rows = [rows[unit_id] for unit_id in blocking_ids]
    blocking_rows.sort(key=lambda row: (
        not row["terminal_attention"], -row["direct_prerequisite_gates_cleared_if_completed"],
        -row["incomplete_direct_dependents_count"], not row["deepest_remaining_branch"], row["work_unit_id"],
    ))
    summary = {
        "units_total": len(ids), "incomplete_total": len(residual),
        "by_category": {name: categories[name] for name in ("complete", "leased", "terminal_attention", "structurally_ready", "structurally_waiting")},
        "by_status": {name: statuses[name] for name in _STATUS_ORDER},
        "structural_ready_incomplete_total": len(ready_rows),
        "structural_waiting_incomplete_total": len(residual) - len(ready_rows),
    }
    remaining = {
        "units_total": len(residual), "edges_total": sum(len(parents) for parents in residual_parents.values()),
        "wave_count": len(wave_rows), "maximum_structural_depth": maximum_depth,
        "maximum_dependency_edges": max(0, maximum_depth - 1), "waves": _page(wave_rows, limit=limit, offset=offset),
        "checkpoints": checkpoint_summaries,
    }
    frontiers = {
        "ready_frontier": _unit_page(ready_rows, goal_id=goal_id, limit=limit, offset=offset),
        "blocking_frontier": _unit_page(blocking_rows, goal_id=goal_id, limit=limit, offset=offset),
    }
    # Inspection needs one selected row from this same graph pass.  Keep the
    # default return shape so the established public readiness projection is
    # byte-for-byte unchanged.
    if include_rows:
        return summary, remaining, frontiers, rows
    return summary, remaining, frontiers


def goal_readiness(store: StateStore, *, goal_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Return a one-snapshot, read-only execution-readiness report."""
    goal_id = _identifier(goal_id, label="goal_id")
    limit, offset = _bounds(limit, offset)
    if not store.path.is_file():
        raise StateError(f"Runtime database does not exist: {store.path}")
    try:
        with store._connection(write=False) as connection:
            connection.create_function("tasktra_lease_elapsed", 2, _lease_elapsed)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            store._assert_audit_chain_in_transaction(connection)
            store._assert_current_state_integrity_in_transaction(connection)
            if connection.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
                raise StateError(f"Unknown goal: {goal_id}")
            graph = store._work_dependency_graph_in_transaction(connection, goal_id)
            operational, checkpoints = _operational_gates(connection, goal_id=goal_id, graph=graph)
            summary, remaining, frontiers = _synthesize(
                goal_id, graph, limit=limit, offset=offset, checkpoint_summaries=checkpoints,
            )
    except sqlite3.Error as error:
        raise StateError("unable to read goal readiness state") from error
    return {
        "kind": "tasktra.goal-readiness", "version": 1, "schema_version": SCHEMA_VERSION,
        "goal_id": goal_id, "read_only": True, "claimability_evaluated": False, "notice": _NOTICE,
        "summary": summary, "remaining_structure": remaining, "frontiers": frontiers,
        "operational_gates": operational,
        "claimability_drilldown": {
            "evaluated": False,
            "unevaluated_inputs": [
                "performer_id", "current envelope digest", "transition approval", "authority scope and requested resource scope",
                "lease duration", "prospective token reservation", "retry timestamp at an actor-specific evaluation time", "claim transaction race",
            ],
            "report_template": [
                "tasktra", "work", "explain", goal_id, "--actor", "{performer_id}",
                "--envelope-sha256", "{envelope_sha256}", "--lease-seconds", "{lease_seconds}",
                "--token-reservation", "{token_reservation}", "--limit", "20", "--offset", "0",
            ],
        },
    }
