"""Read-only, anchored dependency-impact projections.

The projection deliberately reports structural dependency facts only.  It does
not decide whether a work unit is claimable or authorize any lifecycle action.
"""

from __future__ import annotations

from collections import deque
import sqlite3
from typing import Any

from .state import SCHEMA_VERSION, StateError, StateStore, _identifier


_DIRECTIONS = {"both", "prerequisites", "dependents"}
_NOTICE = (
    "Structural impact only. Completing a prerequisite does not by itself "
    "authorize a claim; tasktra work explain rechecks all claim gates."
)


def _pagination(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise StateError("limit must be an integer from 1 to 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
        raise StateError("offset must be an integer from 0 to 1000000")
    return limit, offset


def _distances(adjacency: dict[str, list[str]], anchor: str) -> dict[str, int]:
    """Return minimum positive distances using an iterative breadth-first walk."""
    found: dict[str, int] = {}
    pending: deque[tuple[str, int]] = deque((related, 1) for related in adjacency[anchor])
    while pending:
        identifier, distance = pending.popleft()
        if identifier in found:
            continue
        found[identifier] = distance
        pending.extend((related, distance + 1) for related in adjacency[identifier])
    return found


def dependency_impact(
    store: StateStore, *, goal_id: str, work_unit_id: str, direction: str = "both",
    limit: int = 20, offset: int = 0,
) -> dict[str, Any]:
    """Project an anchor's verified prerequisite and dependent closures.

    All durable reads occur in one immutable SQLite snapshot.  The graph loader
    remains the authority for graph, checkpoint, and status integrity.
    """
    goal_id = _identifier(goal_id, label="goal_id")
    work_unit_id = _identifier(work_unit_id, label="work_unit_id")
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise StateError("direction must be one of both, prerequisites, or dependents")
    limit, offset = _pagination(limit, offset)
    if not store.path.is_file():
        raise StateError(f"Runtime database does not exist: {store.path}")

    try:
        with store._connection(write=False) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            store._assert_audit_chain_in_transaction(connection)
            store._assert_current_state_integrity_in_transaction(connection)
            if connection.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
                raise StateError(f"Unknown goal: {goal_id}")
            anchor_row = connection.execute(
                "SELECT goal_id FROM work_units WHERE id=?", (work_unit_id,)
            ).fetchone()
            if anchor_row is None or anchor_row["goal_id"] != goal_id:
                raise StateError("work unit belongs to a different or unknown goal")
            graph = store._work_dependency_graph_in_transaction(connection, goal_id)
    except sqlite3.Error as error:
        raise StateError("unable to read work unit dependency state") from error

    prerequisites = {
        identifier: [item["id"] for item in unit["prerequisites"]]
        for identifier, unit in graph.items()
    }
    dependents: dict[str, list[str]] = {identifier: [] for identifier in graph}
    for dependent_id, related_prerequisites in prerequisites.items():
        for prerequisite_id in related_prerequisites:
            dependents[prerequisite_id].append(dependent_id)
    for related in dependents.values():
        related.sort()

    prerequisite_distances = _distances(prerequisites, work_unit_id)
    dependent_distances = _distances(dependents, work_unit_id)
    anchor = graph[work_unit_id]

    clears_gate: set[str] = set()
    if anchor["status"] != "complete":
        for dependent_id in dependents[work_unit_id]:
            dependent = graph[dependent_id]
            if not dependent["ready"] and all(
                item["id"] == work_unit_id or item["status"] == "complete"
                for item in dependent["prerequisites"]
            ):
                clears_gate.add(dependent_id)

    def relation_row(identifier: str, relation: str, distance: int) -> dict[str, Any]:
        unit = graph[identifier]
        return {
            "work_unit_id": identifier,
            "relation": relation,
            "distance": distance,
            "direct": distance == 1,
            "status": unit["status"],
            "checkpoint_id": unit["checkpoint_id"],
            "structural_ready": unit["ready"],
            "incomplete_blocker": relation == "prerequisite" and unit["status"] != "complete",
            "would_clear_direct_prerequisite_gate": relation == "dependent" and identifier in clears_gate,
        }

    prerequisite_rows = [
        relation_row(identifier, "prerequisite", distance)
        for identifier, distance in prerequisite_distances.items()
    ]
    dependent_rows = [
        relation_row(identifier, "dependent", distance)
        for identifier, distance in dependent_distances.items()
    ]
    prerequisite_rows.sort(key=lambda row: (row["distance"], row["work_unit_id"]))
    dependent_rows.sort(key=lambda row: (row["distance"], row["work_unit_id"]))
    if direction == "prerequisites":
        relations = prerequisite_rows
    elif direction == "dependents":
        relations = dependent_rows
    else:
        relations = prerequisite_rows + dependent_rows
    page = relations[offset:offset + limit]
    direct_prerequisites = prerequisites[work_unit_id]

    return {
        "goal_id": goal_id,
        "work_unit_id": work_unit_id,
        "read_only": True,
        "claimability_evaluated": False,
        "notice": _NOTICE,
        "anchor": {
            "work_unit_id": work_unit_id,
            "status": anchor["status"],
            "checkpoint_id": anchor["checkpoint_id"],
            "structural_ready": anchor["ready"],
            "direct_prerequisites_total": len(direct_prerequisites),
            "incomplete_direct_prerequisites_total": sum(
                item["status"] != "complete" for item in anchor["prerequisites"]
            ),
        },
        "summary": {
            "direct_prerequisites_total": len(direct_prerequisites),
            "all_prerequisites_total": len(prerequisite_distances),
            "incomplete_blockers_total": sum(
                graph[identifier]["status"] != "complete" for identifier in prerequisite_distances
            ),
            "direct_dependents_total": len(dependents[work_unit_id]),
            "all_dependents_total": len(dependent_distances),
            "direct_prerequisite_gates_cleared_if_completed": len(clears_gate),
        },
        "direction": direction,
        "relations": page,
        "total": len(relations),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(page) if offset + len(page) < len(relations) else None,
    }
