"""Bounded, read-only snapshot capture for the offline operator cockpit."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from uuid import uuid4

from .overview import _lease_elapsed, _overview_in_transaction, _public_text
from .state import SCHEMA_VERSION, StateError, StateStore, _GENESIS_HASH


MAX_GOALS = 2_000
MAX_WORK_UNITS = 10_000
MAX_WORK_UNITS_PER_GOAL = 2_000
MAX_EDGES = 50_000
MAX_DIRECT_PREREQUISITES = 64
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_HTML_BYTES = 24 * 1024 * 1024
_TEMPLATES = {
    "goal-overview": {"label": "Inspect this goal overview", "read_only": True,
                      "argv_suffix": ["overview", "--root", "{root}", "--goal-id", "{goal_id}", "--limit", "{limit}", "--offset", "{offset}", "--json"]},
    "goal-status": {"label": "Inspect this goal status", "read_only": True,
                    "argv_suffix": ["status", "--root", "{root}", "--goal-id", "{goal_id}", "--detail-limit", "{limit}"]},
    "work-dependencies": {"label": "Inspect direct prerequisite readiness", "read_only": True,
                          "argv_suffix": ["work", "--root", "{root}", "dependencies", "{goal_id}", "--work-unit-id", "{work_unit_id}", "--limit", "{limit}", "--offset", "{offset}"]},
    "work-impact": {"label": "Inspect prerequisite and dependent impact", "read_only": True,
                    "argv_suffix": ["work", "--root", "{root}", "impact", "{goal_id}", "{work_unit_id}", "--direction", "both", "--limit", "{limit}", "--offset", "{offset}"]},
    "doctor": {"label": "Inspect Tasktra source provenance", "read_only": True,
               "argv_suffix": ["doctor", "--root", "{root}"]},
}
_ATTENTION_ORDER = {
    "provider-effect-failed": 0, "provider-effect-indeterminate": 1,
    "provider-effect-reconciliation-required": 2, "expired-lease-recovery": 3,
    "budget-exhausted": 4, "goal-blocked": 5, "goal-paused": 6,
    "goal-stopped": 7, "goal-draining": 8, "work-approval-required": 9,
    "work-blocked": 10, "work-failed": 11, "work-exhausted": 12,
    "dependency-waiting": 13, "missing-contract": 14, "missing-work": 15,
    "goal-planned": 16,
}
_UNIT_STATUS_ORDER = (
    "approval-required", "blocked", "failed", "exhausted", "leased", "retry-wait",
    "eligible", "planned", "paused", "stopped", "complete",
)


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise StateError("at must be an ISO-8601 timestamp") from error
    else:
        raise StateError("at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise StateError("at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _guidance_templates() -> dict[str, dict[str, Any]]:
    """Return detached template data so one returned snapshot cannot taint another."""
    return {
        identifier: {
            "label": str(template["label"]), "read_only": True,
            "argv_suffix": list(template["argv_suffix"]),
        }
        for identifier, template in _TEMPLATES.items()
    }


def _provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("package_version", "package_kind", "package_path", "loaded_source_checkout",
            "project_source_path", "source_matches_project", "foreign_source_checkout",
            "supported_runtime_schema", "python_executable")
    result = {key: value.get(key) for key in keys}
    result["package_version"] = str(result["package_version"] or "unknown")
    result["package_kind"] = str(result["package_kind"] or "unknown")
    result["package_path"] = str(result["package_path"] or "")
    result["python_executable"] = str(result["python_executable"] or sys.executable)
    result["supported_runtime_schema"] = int(result["supported_runtime_schema"] or SCHEMA_VERSION)
    for key in ("loaded_source_checkout", "project_source_path"):
        if result[key] is not None:
            result[key] = str(result[key])
    result["source_matches_project"] = result["source_matches_project"] if isinstance(result["source_matches_project"], bool) else None
    result["foreign_source_checkout"] = bool(result["foreign_source_checkout"])
    return result


def _unit(row: sqlite3.Row, *, now: str, graph: Mapping[str, Any] | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": str(row["id"]), "title": _public_text(row["title"]), "status": str(row["status"]),
        "checkpoint_id": row["checkpoint_id"], "attempt_count": int(row["attempt_count"]),
        "last_outcome_class": row["last_outcome_class"], "lease": None,
        "updated_at": row["updated_at"],
    }
    if row["lease_expires_at"] is not None:
        item["lease"] = {"state": "live" if str(row["lease_expires_at"]) > now else "expired", "expires_at": row["lease_expires_at"]}
    if graph is not None:
        node = graph[str(row["id"])]
        item["structural_ready"] = bool(node["ready"])
        item["prerequisite_ids"] = sorted(str(edge["id"]) for edge in node["prerequisites"])
    return item


def capture_operator_cockpit(
    store: StateStore, *, project_root: Path, project_name: str,
    source_provenance: Mapping[str, Any], page_size: int = 20,
    at: str | datetime | None = None,
) -> dict[str, Any]:
    """Capture one fully verified, coherent, bounded runtime snapshot."""
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise StateError("page_size must be an integer from 1 to 100")
    if not store.path.is_file():
        raise StateError(f"Runtime database does not exist: {store.path}")
    captured_at = _timestamp(at)
    root = Path(project_root).resolve()
    provenance = _provenance(source_provenance)
    try:
        with store._connection(write=False) as connection:
            connection.create_function("tasktra_lease_elapsed", 2, _lease_elapsed)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
            if version > SCHEMA_VERSION:
                raise StateError(f"runtime schema {version} requires a compatible Tasktra build")
            audit_sequence = store._assert_audit_chain_in_transaction(connection)
            store._assert_current_state_integrity_in_transaction(connection)
            audit_row = connection.execute("SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
            audit_head = _GENESIS_HASH if audit_row is None else str(audit_row["event_hash"])
            manifest = StateStore._state_manifest_hash(connection)
            control = connection.execute("SELECT emergency_stopped,reason,set_at FROM runtime_control WHERE id=1").fetchone()
            total_goals = int(connection.execute("SELECT count(*) FROM goals").fetchone()[0])
            # The internal builder is intentionally not subject to the public
            # overview page cap: cockpit needs every goal's attention facts
            # before it can safely choose the bounded portfolio.
            overview = _overview_in_transaction(
                connection, capture_timestamp=captured_at, goal_id=None, limit=total_goals, offset=0,
            )
            aggregates = overview["aggregates"]
            total_units = int(aggregates["work_units"]["total"])
            aggregates["provider_effects"] = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,count(*) AS count FROM effect_intents GROUP BY status ORDER BY status"
                )
            }
            total_edges = int(connection.execute("SELECT count(*) FROM work_unit_dependencies").fetchone()[0])
            base_views = list(overview["goals"])
            attention = [view for view in base_views if view["attention"]]
            if len(attention) > MAX_GOALS:
                raise StateError("operator cockpit cannot capture more than 2000 attention goals")
            normal = [view for view in base_views if not view["attention"]]
            attention.sort(key=lambda view: (
                min(_ATTENTION_ORDER.get(str(reason["code"]), len(_ATTENTION_ORDER)) for reason in view["attention"]),
                -int(view["priority"]), str(view["id"]),
            ))
            normal.sort(key=lambda view: (-int(view["priority"]), str(view["id"])))
            selected = attention + normal[:MAX_GOALS - len(attention)]
            captured_units = 0
            captured_edges = 0
            goals: list[dict[str, Any]] = []
            truncated: list[str] = []
            for view in selected:
                goal_id = str(view["id"])
                units_total = int(view["progress"]["work"]["total"])
                edges_total = int(connection.execute(
                    "SELECT count(*) FROM work_unit_dependencies d JOIN work_units u ON u.id=d.work_unit_id WHERE u.goal_id=?", (goal_id,)
                ).fetchone()[0])
                remaining_units = min(MAX_WORK_UNITS_PER_GOAL, MAX_WORK_UNITS - captured_units)
                status_case = "CASE status " + " ".join(
                    f"WHEN '{status}' THEN {position}" for position, status in enumerate(_UNIT_STATUS_ORDER)
                ) + f" ELSE {len(_UNIT_STATUS_ORDER)} END"
                rows = connection.execute(
                    f"SELECT id,title,status,checkpoint_id,attempt_count,last_outcome_class,lease_expires_at,updated_at FROM work_units WHERE goal_id=? ORDER BY {status_case},id LIMIT ?",
                    (goal_id, remaining_units),
                ).fetchall()
                complete_units = len(rows) == units_total
                graph: Mapping[str, Any] | None = None
                if complete_units and captured_edges + edges_total <= MAX_EDGES:
                    candidate = store._work_dependency_graph_in_transaction(connection, goal_id)
                    if all(len(node["prerequisites"]) <= MAX_DIRECT_PREREQUISITES for node in candidate.values()):
                        graph = candidate
                graph_complete = graph is not None
                unit_rows = [_unit(row, now=captured_at, graph=graph) for row in rows]
                units_captured = len(unit_rows)
                edges_captured = edges_total if graph_complete else 0
                captured_units += units_captured
                captured_edges += edges_captured
                goal = {key: view[key] for key in ("id", "title", "status", "priority", "contract", "progress", "budget", "dependencies", "checkpoints", "leases", "intake", "attention", "provider_effects")}
                goal["completeness"] = {
                    "work_units_total": units_total, "work_units_captured": units_captured,
                    "work_units_omitted": units_total - units_captured, "dependency_edges_total": edges_total,
                    "dependency_edges_captured": edges_captured,
                    "dependency_edges_omitted": edges_total - edges_captured,
                    "graph_complete": graph_complete,
                }
                goal["guidance_template_ids"] = ["goal-overview", "goal-status"]
                goal["work_units"] = unit_rows
                goals.append(goal)
                if not graph_complete:
                    truncated.append(goal_id)
            loaded = provenance.get("loaded_source_checkout")
            env = {"PYTHONPATH": str(Path(loaded) / "src")} if loaded else {}
            snapshot = {
                "kind": "tasktra.operator-cockpit.snapshot", "version": 1, "read_only": True,
                "claimability_evaluated": False,
                "capture": {"captured_at": captured_at, "schema_version": version, "audit_sequence": audit_sequence,
                            "audit_head_sha256": audit_head, "state_manifest_sha256": manifest},
                "filesystem_observations": {
                    "project_config": {"captured_at": captured_at, "path": str(root / ".tasktra" / "project.toml")},
                    "runtime_source": {"captured_at": captured_at, "path": provenance["package_path"] or None},
                },
                "project": {"name": _public_text(project_name), "root": str(root)}, "source_provenance": provenance,
                "guidance_context": {"cwd": str(root), "env": env,
                                     "argv_prefix": [provenance["python_executable"], "-m", "tasktra"],
                                     "shell": "powershell" if os.name == "nt" else "posix"},
                "guidance_templates": _guidance_templates(),
                "bounds": {"goal_summaries": MAX_GOALS, "captured_work_units_total": MAX_WORK_UNITS,
                           "captured_work_units_per_goal": MAX_WORK_UNITS_PER_GOAL,
                           "captured_dependency_edges_total": MAX_EDGES,
                           "direct_prerequisites_per_unit": MAX_DIRECT_PREREQUISITES,
                           "canonical_snapshot_json_bytes": MAX_SNAPSHOT_BYTES, "final_html_bytes": MAX_HTML_BYTES,
                           "page_size": page_size},
                "completeness": {"goals_total": total_goals, "goals_captured": len(goals),
                                 "attention_goals_total": len(attention), "normal_goals_omitted": total_goals - len(goals),
                                 "work_units_total": total_units, "work_units_captured": captured_units,
                                 "dependency_edges_total": total_edges, "dependency_edges_captured": captured_edges,
                                 "truncated_goal_ids": sorted(truncated)},
                "runtime": {"emergency_stop": {"active": bool(control["emergency_stopped"]),
                                                  "reason": _public_text(control["reason"]) if control["reason"] else None,
                                                  "set_at": control["set_at"]}},
                "aggregates": aggregates, "goals": goals,
            }
    except sqlite3.Error as error:
        raise StateError("unable to read operator cockpit state") from error
    if len(_canonical_bytes(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise StateError("operator cockpit snapshot exceeds 20 MiB")
    return snapshot


def export_operator_cockpit(snapshot: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """Render and exclusively publish a cockpit file without replacing output."""
    from .cockpit_view import render
    try:
        snapshot_size = len(_canonical_bytes(snapshot))
    except (TypeError, ValueError) as error:
        raise StateError("operator cockpit snapshot is not canonical JSON") from error
    if snapshot_size > MAX_SNAPSHOT_BYTES:
        raise StateError("operator cockpit snapshot exceeds 20 MiB")
    requested = Path(output).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if os.path.lexists(requested):
        raise StateError(f"operator cockpit output already exists: {requested}")
    target = requested.resolve(strict=False)
    if target.suffix.lower() != ".html":
        raise StateError("operator cockpit output must be a .html file")
    if os.path.lexists(target):
        raise StateError(f"operator cockpit output already exists: {target}")
    rendered = render(snapshot)
    if not isinstance(rendered, bytes):
        raise StateError("operator cockpit renderer must return bytes")
    if len(rendered) > MAX_HTML_BYTES:
        raise StateError("operator cockpit HTML exceeds 24 MiB")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except (FileExistsError, OSError) as error:
            raise StateError("operator cockpit exclusive publication failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"output": str(target), "bytes": len(rendered), "sha256": sha256(rendered).hexdigest(),
            "capture": dict(snapshot["capture"]), "completeness": dict(snapshot["completeness"])}
