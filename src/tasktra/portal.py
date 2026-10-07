"""Read-only local status portal for a Tasktra project.

The portal intentionally reads SQLite directly through ``mode=ro`` connections.
It is a status surface, not another StateStore client: opening it must never
create a runtime database, apply migrations, or write an execution receipt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from itertools import islice
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .config import ConfigError, load_project_config
from .execution import ExecutionError, ExecutionStore
from .efficiency import summarize_executions
from .codex_usage import USAGE_FIELDS
from .state import SCHEMA_VERSION
from .portal_activity import agent_activity
from .portal_insights import build_insights, project_key


_MAX_GOALS = 200
_MAX_JOBS = 500
_MAX_AGENTS = 500
_MAX_EVENTS = 80
_MAX_ANALYTICS_RECORDS = 5_000
_MAX_ANALYTICS_BUCKETS = 720
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/portal-insights.js": ("portal-insights.js", "application/javascript; charset=utf-8"),
    "/relationship-map.js": ("relationship-map.js", "application/javascript; charset=utf-8"),
    "/vendor-cytoscape-3.34.3.min.js": ("vendor-cytoscape-3.34.3.min.js", "application/javascript; charset=utf-8"),
    "/vendor-layout-base-2.0.1.js": ("vendor-layout-base-2.0.1.js", "application/javascript; charset=utf-8"),
    "/vendor-cose-base-2.2.0.js": ("vendor-cose-base-2.2.0.js", "application/javascript; charset=utf-8"),
    "/vendor-cytoscape-fcose-2.2.0.js": ("vendor-cytoscape-fcose-2.2.0.js", "application/javascript; charset=utf-8"),
    "/vendor-graph-licenses.js": ("vendor-graph-licenses.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_snapshot(name: str, message: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": _timestamp(),
        "project": {"name": name},
        "runtime": {"available": False, "emergency_stopped": False, "message": message},
        "summary": {
            "goals": 0, "active_goals": 0, "jobs": 0, "completed_jobs": 0,
            "running_jobs": 0, "blocked_jobs": 0, "agents": 0, "running_agents": 0,
        },
        "goals": [], "jobs": [], "agents": [], "events": [], "warnings": [],
    }


def _with_insights(snapshot: dict[str, Any], root: Path, *, goal_id: str | None,
                   runtime_schema_version: int | None = None) -> dict[str, Any]:
    """Attach Phase 1 safe projections on every snapshot return path."""
    project = snapshot.get("project")
    if isinstance(project, dict):
        project["key"] = project_key(root)
    snapshot["insights"] = build_insights(
        snapshot, root, goal_id=goal_id, runtime_schema_version=runtime_schema_version,
        expected_schema_version=SCHEMA_VERSION,
    )
    return snapshot


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    # quote keeps spaces and '#' from changing the SQLite URI while preserving
    # the Windows drive separator used by an absolute Path.
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    # Keep counts, rows, and latest events in one committed read snapshot.
    connection.execute("BEGIN")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _as_int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _valid_observed_usage(value: Any) -> bool:
    return (isinstance(value, dict) and all(isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0 for key in USAGE_FIELDS)
            and value["total_tokens"] == value["input_tokens"] + value["output_tokens"]
            and value["cached_input_tokens"] <= value["input_tokens"] and value["cache_write_input_tokens"] <= value["input_tokens"]
            and value["reasoning_output_tokens"] <= value["output_tokens"])


def _decoded_list(value: Any, warnings: list[str], label: str) -> list[Any]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        warnings.append(f"{label} is malformed and was omitted.")
        return []
    if not isinstance(decoded, list):
        warnings.append(f"{label} is malformed and was omitted.")
        return []
    return decoded


def _lease_stale(value: Any, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.astimezone(timezone.utc) <= now


def _limited_rows(connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...], limit: int) -> tuple[list[sqlite3.Row], int]:
    total = int(connection.execute(f"SELECT count(*) FROM ({query})", parameters).fetchone()[0])
    return connection.execute(f"{query} LIMIT ?", (*parameters, limit)).fetchall(), total


def _execution_ledger(project_root: Path, warnings: list[str]) -> sqlite3.Connection | None:
    """Unavailable optional receipts must never suppress runtime lease evidence."""
    connection = None
    try:
        store = ExecutionStore(project_root)
        store._assert_private_path(store.path, must_exist=store.path.exists())
        if not store.path.exists():
            return None
        if not store.path.is_file():
            raise ExecutionError("Execution ledger is not a regular file.")
        connection = _sqlite_readonly(store.path)
        if "execution" not in _tables(connection) or not {"work_id", "role", "state"}.issubset(_columns(connection, "execution")):
            raise ExecutionError("Execution ledger has an unsupported schema.")
        return connection
    except (ExecutionError, OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        warning = "Execution ledger is unavailable or has an unsupported schema; showing runtime leases."
        if warning not in warnings:
            warnings.append(warning)
        return None


def _execution_rows(project_root: Path, warnings: list[str], *, goal_id: str | None = None, linked_work_ids: tuple[str, ...] = ()) -> tuple[list[sqlite3.Row], int, int]:
    """Read the optional receipt ledger after applying its link protections."""
    try:
        connection = _execution_ledger(project_root, warnings)
        if connection is None:
            return [], 0, 0
        try:
            columns = _columns(connection, "execution")
            names = (
                "work_id", "role", "state", "agent_id", "configured_model", "configured_effort",
                "requested_model", "requested_effort", "observed_model", "observed_effort",
                "start_provenance", "finish_provenance", "usage_provenance", "usage_json",
                "rollout_agent_id", "rollout_model", "rollout_effort", "source_sha256", "parent_work_id",
                "goal_id", "goal_attribution_reason", "provider", "thread_id", "turn_id", "outcome", "unknown_reason",
                "observed_started_at", "last_observed_at", "usage_events_json",
            )
            selected = ", ".join(name if name in columns else f"NULL AS {name}" for name in names)
            ids = tuple(linked_work_ids[:900])
            if goal_id is not None and "goal_id" in columns and ids:
                marks = ",".join("?" for _ in ids); where = f" WHERE goal_id=? OR work_id IN ({marks}) OR parent_work_id IN ({marks})"; parameters = (goal_id, *ids, *ids)
            elif goal_id is not None and "goal_id" in columns:
                where, parameters = " WHERE goal_id=?", (goal_id,)
            elif goal_id is not None and ids:
                marks = ",".join("?" for _ in ids); where, parameters = f" WHERE work_id IN ({marks}) OR parent_work_id IN ({marks})", (*ids, *ids)
            elif goal_id is not None:
                where, parameters = " WHERE 0", ()
            else:
                where, parameters = "", ()
            total, running = connection.execute(
                f"SELECT count(*),sum(state='started') FROM execution{where}", parameters
            ).fetchone()
            rows = connection.execute(f"SELECT {selected} FROM execution{where} ORDER BY work_id LIMIT ?", (*parameters, _MAX_AGENTS)).fetchall()
            return rows, int(total), int(running or 0)
        finally:
            connection.close()
    except (ExecutionError, OSError, sqlite3.Error):
        warnings.append("Execution ledger could not be read.")
        return [], 0, 0


def _execution_parent(row: sqlite3.Row) -> str:
    """Resolve a stage receipt to its runtime work unit when it has one."""
    parent = row["parent_work_id"]
    return str(parent) if isinstance(parent, str) and parent else str(row["work_id"])


def _attribution_only_receipt(row: sqlite3.Row) -> bool:
    return (row["role"] == "coordinator" and row["state"] == "planned"
            and row["attribution_reason"] in {"run-supervisor", "parent-attribution"}
            and all(row[field] is None for field in (
                "provider", "thread_id", "start_provenance", "finish_provenance", "usage_json", "usage_provenance"
            )))


def _fresh_running_execution_count(project_root: Path, runtime: sqlite3.Connection,
                                   now: datetime, warnings: list[str], *, goal_id: str | None = None) -> int | None:
    """Count started receipts that still correspond to fresh linked leases.

    The receipt ledger is separate from StateStore, so this streams work IDs
    in small batches instead of materializing an unbounded execution history.
    Unlinked started receipts remain live observations; linked terminal or
    stale jobs do not.
    """
    try:
        ledger = _execution_ledger(project_root, warnings)
        if ledger is None:
            return 0
        try:
            columns = _columns(ledger, "execution")
            parent = "parent_work_id" if "parent_work_id" in columns else "NULL AS parent_work_id"
            goal_column = "goal_id" if "goal_id" in columns else "NULL AS goal_id"
            cursor = ledger.execute(f"SELECT work_id,{parent},{goal_column} FROM execution WHERE state='started'")
            running = 0
            while batch := cursor.fetchmany(200):
                work_ids = [_execution_parent(row) for row in batch]
                marks = ",".join("?" for _ in work_ids)
                linked = {
                    str(row["id"]): row
                    for row in runtime.execute(
                        f"SELECT id,goal_id,status,lease_expires_at FROM work_units WHERE id IN ({marks})", work_ids
                    )
                }
                for receipt, work_id in zip(batch, work_ids):
                    job = linked.get(work_id)
                    receipt_goal = job["goal_id"] if job is not None else receipt["goal_id"]
                    if goal_id is not None and receipt_goal != goal_id:
                        continue
                    if job is None:
                        running += 1
                    elif job["status"] == "leased" and not _lease_stale(job["lease_expires_at"], now):
                        running += 1
            return running
        finally:
            ledger.close()
    except (ExecutionError, OSError, sqlite3.Error):
        warnings.append("Execution lease freshness could not be read.")
        return None


def _lease_agents(project_root: Path, runtime: sqlite3.Connection, now: datetime,
                  limit: int, warnings: list[str], *, goal_id: str | None = None) -> tuple[list[dict[str, Any]], int, int, int, int]:
    """Project current leases over absent, planned, or terminal receipts."""
    ledger: sqlite3.Connection | None = None
    try:
        ledger = _execution_ledger(project_root, warnings)
        receipt_ledger_known = ledger is not None or not ExecutionStore(project_root).path.exists()
        cursor = runtime.execute(
            """SELECT w.id,w.goal_id,w.lease_holder,w.lease_expires_at,a.owner_id,a.heartbeat_at,a.expires_at AS attempt_expires_at
               FROM work_units w LEFT JOIN work_attempts a ON a.id=w.current_attempt_id
               WHERE w.status='leased' AND COALESCE(a.owner_id,w.lease_holder) IS NOT NULL
                 AND (? IS NULL OR w.goal_id=?)
               ORDER BY w.updated_at DESC,w.id""",
            (goal_id, goal_id),
        )
        items: list[dict[str, Any]] = []
        new_total = running = projected_total = missing_receipts = 0
        while batch := cursor.fetchmany(200):
            linked_receipts: dict[str, list[sqlite3.Row]] = {}
            if ledger is not None:
                ids = [str(row["id"]) for row in batch]
                marks = ",".join("?" for _ in ids)
                columns = _columns(ledger, "execution")
                names = ("work_id", "state", "parent_work_id", "role", "attribution_reason", "provider", "thread_id",
                         "start_provenance", "finish_provenance", "usage_json", "usage_provenance")
                selected = ", ".join(name if name in columns else f"NULL AS {name}" for name in names)
                for receipt in ledger.execute(
                    f"SELECT {selected} FROM execution WHERE work_id IN ({marks}) OR parent_work_id IN ({marks})"
                    if "parent_work_id" in columns else f"SELECT {selected} FROM execution WHERE work_id IN ({marks})",
                    (*ids, *ids) if "parent_work_id" in columns else ids,
                ):
                    linked_receipts.setdefault(_execution_parent(receipt), []).append(receipt)
            for row in batch:
                work_id = str(row["id"])
                receipts = linked_receipts.get(work_id, [])
                if any(receipt["state"] == "started" for receipt in receipts):
                    continue
                if receipt_ledger_known and not any(not _attribution_only_receipt(receipt) for receipt in receipts):
                    missing_receipts += 1
                expiry = row["attempt_expires_at"] or row["lease_expires_at"]
                stale = _lease_stale(expiry, now)
                projected_total += 1
                # All linked planned/terminal receipts are replaced by this
                # single current-lease projection, including staged children.
                new_total += 1
                running += not stale
                if len(items) < limit:
                    items.append({
                        "id": row["owner_id"] or row["lease_holder"], "work_id": work_id, "job_id": work_id,
                        "goal_id": str(row["goal_id"]), "role": "worker", "state": "leased",
                        "model": None, "effort": None, "provenance": "work-lease", "total_tokens": None,
                        "heartbeat_at": row["heartbeat_at"], "lease_expires_at": expiry, "lease_stale": stale,
                        "heartbeat": {"kind": "lease", "observed_at": row["heartbeat_at"], "expires_at": expiry, "stale": stale, "live_status": "unknown"},
                    })
        return items, new_total, running, projected_total, missing_receipts
    except (ExecutionError, OSError, sqlite3.Error):
        warnings.append("Lease-derived agents could not be read.")
        return [], 0, 0, 0, 0
    finally:
        if ledger is not None:
            ledger.close()


def _add_execution_agents(snapshot: dict[str, Any], execution_rows: list[sqlite3.Row],
                          total: int, running: int, work_details: dict[str, dict[str, Any]],
                          warnings: list[str]) -> tuple[int, int]:
    """Add bounded receipt rows, returning exact ledger totals for summaries."""
    if total > len(execution_rows):
        warnings.append(f"Showing {len(execution_rows)} of {total} execution records.")
    for row in execution_rows:
        work_id = str(row["work_id"])
        usage = None
        if isinstance(row["usage_json"], str):
            try:
                usage = json.loads(row["usage_json"])
            except ValueError:
                warnings.append(f"Execution usage for {work_id} is malformed and was omitted.")
        trusted_usage = usage if row["usage_provenance"] in {"host-callback", "rollout-verified"} and _valid_observed_usage(usage) else None
        total_tokens = trusted_usage["total_tokens"] if trusted_usage is not None else None
        parent_work_id = _execution_parent(row)
        job = work_details.get(parent_work_id)
        provenance = row["start_provenance"] or row["finish_provenance"] or row["usage_provenance"]
        state = str(row["state"])
        # A plan is an assertion of intended routing, never an observation of
        # a running agent.  Its configured model is intentionally omitted.
        observed = state != "planned"
        verified_agent = row["rollout_agent_id"]
        verified_model = row["rollout_model"]
        verified_effort = row["rollout_effort"]
        host_observation = row["start_provenance"] == "host-callback"
        snapshot["agents"].append({
            "id": verified_agent or (row["agent_id"] if host_observation else None) or work_id, "work_id": work_id,
            "parent_work_id": None if parent_work_id == work_id else parent_work_id,
            "job_id": parent_work_id if job is not None else None,
            "goal_id": job["goal_id"] if job is not None else row["goal_id"], "role": str(row["role"]), "state": state,
            "model": (verified_model if verified_model is not None else row["observed_model"] if host_observation else None) if observed else None,
            "effort": (verified_effort if verified_effort is not None else row["observed_effort"] if host_observation else None) if observed else None,
            "configured_model": row["configured_model"], "configured_effort": row["configured_effort"],
            "requested_model": row["requested_model"], "requested_effort": row["requested_effort"],
            "observed_model": verified_model if verified_model is not None else row["observed_model"] if host_observation else None,
            "observed_effort": verified_effort if verified_effort is not None else row["observed_effort"] if host_observation else None,
            "thread_id": row["thread_id"], "turn_id": row["turn_id"], "outcome": row["outcome"],
            "unknown_reason": row["unknown_reason"], "started_at": row["observed_started_at"],
            "finished_at": None, "last_observed_at": row["last_observed_at"],
            "duration_seconds": None,
            "goal_title": None if job is None else job.get("goal_title"), "job_title": None if job is None else job.get("job_title"),
            "goal_attribution_reason": row["goal_attribution_reason"],
            "heartbeat": {"kind": "lease" if job is not None else "none", "observed_at": None if job is None else job["heartbeat_at"], "expires_at": None if job is None else job["lease_expires_at"], "stale": None if job is None else job["lease_stale"], "live_status": "unknown" if state == "started" and job is not None else "not-applicable"},
            "provenance": "rollout-verified" if row["source_sha256"] is not None else provenance, "total_tokens": total_tokens,
            "input_tokens": trusted_usage["input_tokens"] if trusted_usage is not None else None,
            "cached_input_tokens": trusted_usage["cached_input_tokens"] if trusted_usage is not None else None,
            "uncached_input_tokens": trusted_usage["input_tokens"]-trusted_usage["cached_input_tokens"] if trusted_usage is not None else None,
            "output_tokens": trusted_usage["output_tokens"] if trusted_usage is not None else None,
            "heartbeat_at": None if job is None else job["heartbeat_at"],
            "lease_expires_at": None if job is None else job["lease_expires_at"],
            # Only an unfinished execution can have a stale live-work claim.
            # Terminal receipts keep their actual success/failure state.
            "lease_stale": False if job is None or state != "started" else (
                job["status"] != "leased" or job["lease_stale"]
            ),
        })
    return total, running


def _visible_execution_rows(
    rows: list[sqlite3.Row], work_details: dict[str, dict[str, Any]],
) -> list[sqlite3.Row]:
    """Hide coordinator plans and stale stage plans behind their runtime job.

    A supervisor records one coordinator plan per work unit and child receipts
    for its stages.  The parent is attribution only.  A fresh current lease
    replaces planned or terminal child receipts; an actual started child stays
    visible as the observed agent.
    """
    parent_ids = {
        str(row["parent_work_id"]) for row in rows
        if isinstance(row["parent_work_id"], str) and row["parent_work_id"]
    }
    started = {_execution_parent(row) for row in rows if row["state"] == "started"}
    visible: list[sqlite3.Row] = []
    for row in rows:
        work_id = str(row["work_id"])
        linked_work = _execution_parent(row)
        if work_id in parent_ids and row["role"] == "coordinator" and row["state"] == "planned":
            continue
        job = work_details.get(linked_work)
        if job is not None and job["status"] == "leased":
            if linked_work in started and row["state"] != "started":
                continue
            if linked_work not in started:
                continue
        visible.append(row)
    return visible


def _add_execution_only_agents(snapshot: dict[str, Any], project_root: Path) -> None:
    """Keep an optional receipt ledger useful when no StateStore exists yet."""
    rows, total, running = _execution_rows(project_root, snapshot["warnings"])
    rows = _visible_execution_rows(rows, {})
    total = len(rows)
    running = sum(1 for row in rows if row["state"] == "started")
    total, running = _add_execution_agents(snapshot, rows, total, running, {}, snapshot["warnings"])
    snapshot["summary"]["agents"] = total
    snapshot["summary"]["running_agents"] = running


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("since must be a bounded UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("since must include a timezone")
    return parsed.astimezone(timezone.utc)


def _analytics(records: list[dict[str, Any]], *, goal_id: str | None, work_ids: set[str],
               model: str | None, role: str | None, since: datetime | None, truncated: bool) -> dict[str, Any]:
    trusted_usage = frozenset({"host-callback", "rollout-verified"})
    def valid_usage(value: Any) -> bool:
        return _valid_observed_usage(value)
    selected: list[dict[str, Any]] = []
    scoped_options = [record for record in records if record.get("state") != "planned" and (goal_id is None or record.get("goal_id") == goal_id or (record.get("parent_work_id") or record.get("work_id")) in work_ids)]
    excluded = undated = 0
    for record in records:
        linked = record.get("parent_work_id") or record.get("work_id")
        if record.get("state") == "planned":
            continue
        if goal_id is not None and record.get("goal_id") != goal_id and linked not in work_ids:
            excluded += 1; continue
        effective_model = (record.get("rollout_model") or record.get("observed_model")) if record.get("usage_provenance") in trusted_usage else None
        if model is not None and effective_model != model:
            excluded += 1; continue
        if role is not None and record.get("role") != role:
            excluded += 1; continue
        receipt_usage = record.get("usage")
        trusted = record.get("usage_provenance") in trusted_usage and valid_usage(receipt_usage)
        events = record.get("usage_events") if isinstance(record.get("usage_events"), list) else []
        timed_events = []
        for event in events if trusted else []:
            if not isinstance(event, dict) or not valid_usage(event.get("usage")) or not isinstance(event.get("at"), str):
                continue
            try:
                _parse_since(event["at"])
            except (ValueError, OverflowError):
                continue
            timed_events.append(event)
        timed_totals = {key: sum(item["usage"][key] for item in timed_events) for key in USAGE_FIELDS}
        if trusted and (not timed_events or len(timed_events) != len(events) or timed_totals != receipt_usage):
            undated += 1
        # Event deltas cannot establish more measured spend than the trusted
        # aggregate receipt. A mismatched import remains usable only in totals.
        if trusted and any(timed_totals[key] > receipt_usage[key] for key in USAGE_FIELDS):
            timed_events = []
        if since is not None:
            timed_events = [item for item in timed_events if _parse_since(item["at"]) >= since]
            if not timed_events:
                excluded += 1
                continue
            record = {**record, "portal_usage": {key: sum(item["usage"][key] for item in timed_events) for key in USAGE_FIELDS}}
        record = {**record, "usage_events": timed_events}
        selected.append(record)
    def usage(record: dict[str, Any]) -> dict[str, int] | None:
        value = record.get("portal_usage", record.get("usage"))
        return value if record.get("usage_provenance") in trusted_usage and valid_usage(value) else None
    known = [usage(item) for item in selected]; known = [item for item in known if item is not None]
    def report(items: list[dict[str, Any]], label: str | None = None) -> dict[str, Any]:
        values = [usage(item) for item in items]; values = [item for item in values if item is not None]
        count = len(items); local = {key: sum(item[key] for item in values) for key in USAGE_FIELDS}
        sample = sorted(item["total_tokens"] for item in values)
        completed = {key: sum(1 for item in items if item.get("state") == key) for key in ("succeeded", "failed", "cancelled")}
        ended = sum(completed.values())
        value = {"model": label, "records": count, "measured_records": len(values), "unknown_records": count-len(values),
                 "total_tokens": local["total_tokens"] if values else None, "input_tokens": local["input_tokens"] if values else None,
                 "uncached_input_tokens": local["input_tokens"]-local["cached_input_tokens"] if values else None,
                 "output_tokens": local["output_tokens"] if values else None, "cached_input_tokens": local["cached_input_tokens"] if values else None,
                 "cache_utilization_percent": round(100*local["cached_input_tokens"]/local["input_tokens"],2) if local["input_tokens"] else None,
                 "average_measured_tokens": round(sum(sample)/len(sample),2) if sample else None,
                 "median_measured_tokens": (sample[(len(sample)-1)//2]+sample[len(sample)//2])/2 if sample else None,
                 "successful_records": completed["succeeded"], "failed_records": completed["failed"], "cancelled_records": completed["cancelled"],
                 "success_rate_percent": round(100*completed["succeeded"]/ended,2) if ended else None,
                 "failure_rate_percent": round(100*completed["failed"]/ended,2) if ended else None}
        return value
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for record in selected: groups.setdefault((record.get("rollout_model") or record.get("observed_model")) if record.get("usage_provenance") in trusted_usage else None, []).append(record)
    buckets: dict[str, dict[str, int]] = {}
    for record in selected:
        if usage(record) is None:
            continue
        for event in record["usage_events"]:
            at = _parse_since(event["at"])
            bucket = at.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds").replace("+00:00", "Z")
            target = buckets.setdefault(bucket, {"records": 0, **{key: 0 for key in USAGE_FIELDS}})
            target["records"] += 1
            for key in USAGE_FIELDS:
                target[key] += event["usage"][key]
    series = [{"bucket_start": key, "records": value["records"], "measured_records": value["records"],
               "total_tokens": value["total_tokens"], "input_tokens": value["input_tokens"],
               "cached_input_tokens": value["cached_input_tokens"], "output_tokens": value["output_tokens"]}
              for key, value in sorted(buckets.items())[-_MAX_ANALYTICS_BUCKETS:]]
    summary = report(selected)
    return {"filters": {"goal_id": goal_id, "model": model, "role": role, "since": None if since is None else since.isoformat(timespec="seconds").replace("+00:00", "Z")},
            "summary": summary, "by_model": [report(value, key) for key, value in sorted(groups.items(), key=lambda item: str(item[0]))],
            "options": {"models": sorted({item.get("rollout_model") or item.get("observed_model") for item in scoped_options if item.get("usage_provenance") in trusted_usage and isinstance(item.get("rollout_model") or item.get("observed_model"), str)})[:200], "roles": sorted({str(item["role"]) for item in scoped_options if isinstance(item.get("role"), str)})[:200]},
            "time_series": series,
            "coverage": {"scoped_records": len(selected), "visible_records": len(selected), "unknown_records": len(selected)-len(known),
                         "undated_records": undated, "excluded_by_filters": excluded, "time_series_complete": not truncated and undated == 0 and len(buckets) <= _MAX_ANALYTICS_BUCKETS,
                         "truncated": truncated or len(buckets) > _MAX_ANALYTICS_BUCKETS,
                         "reason": "undated native usage is excluded from the timeline" if undated else ("analytics record limit reached" if truncated else "hourly bucket limit reached" if len(buckets) > _MAX_ANALYTICS_BUCKETS else None)},
            "limits": {"max_models": 200, "max_buckets": _MAX_ANALYTICS_BUCKETS, "bucket": "hour", "visible_rows": _MAX_AGENTS}}


def _apply_goal_scope(snapshot: dict[str, Any], goal_id: str | None) -> None:
    if goal_id is None: return
    snapshot["goals"] = [item for item in snapshot["goals"] if item["id"] == goal_id]
    snapshot["jobs"] = [item for item in snapshot["jobs"] if item["goal_id"] == goal_id]
    snapshot["agents"] = [item for item in snapshot["agents"] if item.get("goal_id") == goal_id]
    snapshot["events"] = [item for item in snapshot["events"] if item.get("goal_id") == goal_id]
    # Summary totals are computed by scoped SQL before visible-row limits.  Do
    # not replace them with capped collection lengths here.


def portal_snapshot(root: Path, *, goal_id: str | None = None, model: str | None = None, role: str | None = None, since: str | None = None) -> dict[str, Any]:
    """Return the deliberately small, safe portal API document for ``root``."""
    project_root = Path(root)
    try:
        project_root = project_root.resolve(strict=True)
        config = load_project_config(project_root)
    except (ConfigError, OSError, RuntimeError, ValueError):
        return _with_insights(
            _empty_snapshot(project_root.name or "Tasktra", "Project configuration is unavailable."),
            project_root, goal_id=goal_id,
        )

    snapshot = _empty_snapshot(config.name, "Runtime database is unavailable.")
    runtime_schema_version: int | None = None
    snapshot["filters"] = {"goal_id": goal_id, "model": model, "role": role, "since": since}
    snapshot["goal_options"] = []
    snapshot["global_summary"] = dict(snapshot["summary"])
    warnings: list[str] = snapshot["warnings"]
    try:
        snapshot["efficiency"] = summarize_executions(islice(ExecutionStore(project_root).iter_records(goal_id=goal_id), _MAX_ANALYTICS_RECORDS))
    except (ExecutionError, OSError, sqlite3.Error, ValueError):
        snapshot["efficiency"] = None
    try:
        database = config.database_path(project_root)
        if not database.is_file():
            _add_execution_only_agents(snapshot, project_root)
            return _with_insights(snapshot, project_root, goal_id=goal_id)
        connection = _sqlite_readonly(database)
    except (ConfigError, OSError, sqlite3.Error):
        warnings.append("Runtime database could not be opened read-only.")
        _add_execution_only_agents(snapshot, project_root)
        return _with_insights(snapshot, project_root, goal_id=goal_id)

    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        runtime_schema_version = version
        if version != SCHEMA_VERSION:
            snapshot["runtime"]["message"] = f"Runtime schema {version} requires migration to {SCHEMA_VERSION}."
            return _with_insights(snapshot, project_root, goal_id=goal_id, runtime_schema_version=version)
        tables = _tables(connection)
        needed = {"goals", "work_units", "budgets", "runtime_control", "audit_events", "goal_checkpoints", "acceptance_evidence"}
        if not needed.issubset(tables):
            warnings.append("Runtime database has an unsupported schema.")
            return _with_insights(snapshot, project_root, goal_id=goal_id, runtime_schema_version=version)

        now = _utc_now()
        option_rows, _ = _limited_rows(connection, "SELECT id,title,status FROM goals ORDER BY updated_at DESC,id", (), _MAX_GOALS)
        scoped_goal_where, scoped_goal_params = (" WHERE id=?", (goal_id,)) if goal_id is not None else ("", ())
        goal_rows, goals_total = _limited_rows(
            connection,
            "SELECT id,title,description,status,created_at,updated_at,acceptance FROM goals" + scoped_goal_where + " ORDER BY updated_at DESC,id",
            scoped_goal_params, _MAX_GOALS,
        )
        scoped_job_where, scoped_job_params = (" WHERE w.goal_id=?", (goal_id,)) if goal_id is not None else ("", ())
        job_rows, jobs_total = _limited_rows(
            connection,
            """SELECT w.id,w.goal_id,w.title,w.status,w.attempt_count,w.lease_holder,w.lease_expires_at,w.updated_at,w.last_outcome_class,
                      a.owner_id,a.heartbeat_at,a.expires_at AS attempt_expires_at
                 FROM work_units w LEFT JOIN work_attempts a ON a.id=w.current_attempt_id""" + scoped_job_where + " ORDER BY w.updated_at DESC,w.id",
            scoped_job_params, _MAX_JOBS,
        )
        count_where, count_params = (" WHERE goal_id=?", (goal_id,)) if goal_id is not None else ("", ())
        counts = connection.execute(
            """SELECT
                  (SELECT count(*) FROM goals""" + (" WHERE id=?" if goal_id is not None else "") + """) AS goals,
                  (SELECT count(*) FROM goals""" + (" WHERE id=? AND status='active'" if goal_id is not None else " WHERE status='active'") + """) AS active_goals,
                  (SELECT count(*) FROM work_units""" + count_where + (" AND status='complete'" if count_where else " WHERE status='complete'") + """) AS completed_jobs,
                  (SELECT count(*) FROM work_units""" + count_where + (" AND status='blocked'" if count_where else " WHERE status='blocked'") + """) AS blocked_jobs""",
            ((goal_id, goal_id, goal_id, goal_id) if goal_id is not None else ())
        ).fetchone()
        # Count active leases separately so expired claims are not reported as
        # live work.  Invalid timestamps remain visible on their job row.
        running_jobs = 0
        lease_cursor = connection.execute("SELECT lease_expires_at FROM work_units WHERE status='leased' AND (? IS NULL OR goal_id=?)", (goal_id, goal_id))
        while batch := lease_cursor.fetchmany(200):
            running_jobs += sum(not _lease_stale(row["lease_expires_at"], now) for row in batch)
        snapshot["summary"].update({
            "goals": _as_int(counts["goals"]), "active_goals": _as_int(counts["active_goals"]),
            "jobs": jobs_total, "completed_jobs": _as_int(counts["completed_jobs"]),
            "running_jobs": running_jobs, "blocked_jobs": _as_int(counts["blocked_jobs"]),
        })
        if goals_total > len(goal_rows):
            warnings.append(f"Showing {len(goal_rows)} of {goals_total} goals.")
        if jobs_total > len(job_rows):
            warnings.append(f"Showing {len(job_rows)} of {jobs_total} jobs.")

        displayed_goal_ids = [str(row["id"]) for row in goal_rows]
        marks = ",".join("?" for _ in displayed_goal_ids)
        job_totals = {
            str(row["goal_id"]): (int(row["total"]), int(row["complete"]))
            for row in connection.execute(
                f"SELECT goal_id,count(*) AS total,sum(status='complete') AS complete FROM work_units WHERE goal_id IN ({marks}) GROUP BY goal_id",
                displayed_goal_ids,
            )
        } if displayed_goal_ids else {}
        acceptance_counts = {
            str(row["goal_id"]): int(row["total"])
            for row in connection.execute(
                f"SELECT goal_id,count(*) AS total FROM acceptance_evidence WHERE goal_id IN ({marks}) GROUP BY goal_id",
                displayed_goal_ids,
            )
        } if displayed_goal_ids else {}
        budgets = {
            str(row["goal_id"]): row
            for row in connection.execute(
                f"SELECT goal_id,total_tokens,consumed_tokens,reserved_tokens FROM budgets WHERE goal_id IN ({marks})",
                displayed_goal_ids,
            )
        } if displayed_goal_ids else {}
        checkpoints: dict[str, list[dict[str, str]]] = {}
        if displayed_goal_ids:
            for row in connection.execute(
                f"SELECT goal_id,checkpoint_id,status FROM goal_checkpoints WHERE goal_id IN ({marks}) ORDER BY goal_id,position",
                displayed_goal_ids,
            ):
                checkpoints.setdefault(str(row["goal_id"]), []).append({"id": str(row["checkpoint_id"]), "status": str(row["status"])})
        for row in goal_rows:
            listed_goal_id = str(row["id"])
            total, complete = job_totals.get(listed_goal_id, (0, 0))
            criteria = _decoded_list(row["acceptance"], warnings, f"Acceptance criteria for goal {listed_goal_id}")
            budget = budgets.get(listed_goal_id)
            snapshot["goals"].append({
                "id": listed_goal_id, "title": str(row["title"]), "description": str(row["description"]),
                "status": str(row["status"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
                "jobs_total": total, "jobs_complete": complete,
                "progress_percent": None if total == 0 else round((complete / total) * 100, 2),
                "acceptance_total": len(criteria), "acceptance_recorded": acceptance_counts.get(listed_goal_id, 0),
                "budget": {
                    "total_tokens": None if budget is None or budget["total_tokens"] is None else _as_int(budget["total_tokens"]),
                    "consumed_tokens": 0 if budget is None else _as_int(budget["consumed_tokens"]),
                    "reserved_tokens": 0 if budget is None else _as_int(budget["reserved_tokens"]),
                },
                "checkpoints": checkpoints.get(listed_goal_id, []),
            })
        for row in job_rows:
            expiry = row["attempt_expires_at"] or row["lease_expires_at"]
            status = str(row["status"])
            snapshot["jobs"].append({
                "id": str(row["id"]), "goal_id": str(row["goal_id"]), "title": str(row["title"]),
                "status": status, "owner_id": row["owner_id"] or row["lease_holder"],
                "attempt_count": _as_int(row["attempt_count"]), "heartbeat_at": row["heartbeat_at"],
                "lease_expires_at": expiry, "lease_stale": status == "leased" and _lease_stale(expiry, now),
                "updated_at": str(row["updated_at"]), "last_outcome_class": row["last_outcome_class"],
            })

        scoped_work_ids = tuple(str(row["id"]) for row in connection.execute("SELECT id FROM work_units WHERE goal_id=? ORDER BY id LIMIT 900", (goal_id,))) if goal_id is not None else ()
        if goal_id is not None:
            scoped_work_count = int(connection.execute("SELECT count(*) FROM work_units WHERE goal_id=?", (goal_id,)).fetchone()[0])
            if scoped_work_count > len(scoped_work_ids):
                warnings.append("Goal has more than 900 runtime work units; legacy receipt linkage beyond that bound is omitted unless explicitly attributed.")
        execution_rows, execution_total, execution_running = _execution_rows(project_root, warnings, goal_id=goal_id, linked_work_ids=scoped_work_ids)
        if execution_total > len(execution_rows):
            warnings.append(f"Showing {len(execution_rows)} of {execution_total} execution records; the agent count reflects this displayed subset plus current leases.")
        execution_ids = sorted({_execution_parent(row) for row in execution_rows})
        # Resolve exact work-to-goal joins for displayed receipt rows without
        # exposing or scanning unbounded job detail.
        work_details: dict[str, dict[str, Any]] = {}
        if execution_ids:
            marks = ",".join("?" for _ in execution_ids)
            for row in connection.execute(
                f"""SELECT w.id,w.goal_id,w.title AS job_title,g.title AS goal_title,w.status,w.lease_expires_at,a.heartbeat_at,a.expires_at AS attempt_expires_at
                    FROM work_units w JOIN goals g ON g.id=w.goal_id LEFT JOIN work_attempts a ON a.id=w.current_attempt_id
                    WHERE w.id IN ({marks})""", execution_ids,
            ):
                expiry = row["attempt_expires_at"] or row["lease_expires_at"]
                status = str(row["status"])
                work_details[str(row["id"])] = {
                    "goal_id": str(row["goal_id"]), "goal_title": str(row["goal_title"]), "job_title": str(row["job_title"]), "heartbeat_at": row["heartbeat_at"],
                    "lease_expires_at": expiry,
                    "lease_stale": status == "leased" and _lease_stale(expiry, now), "status": status,
                }
        execution_rows = _visible_execution_rows(execution_rows, work_details)
        execution_total = len(execution_rows)
        execution_running = sum(1 for row in execution_rows if row["state"] == "started")
        execution_total, execution_running = _add_execution_agents(
            snapshot, execution_rows, execution_total, execution_running, work_details, warnings,
        )
        snapshot["summary"]["agents"] = execution_total
        fresh_running = _fresh_running_execution_count(project_root, connection, now, warnings, goal_id=goal_id)
        snapshot["summary"]["running_agents"] = execution_running if fresh_running is None else fresh_running
        lease_items, new_lease_agents, lease_running, projected_leases, missing_execution_receipts = _lease_agents(
            project_root, connection, now, _MAX_AGENTS, warnings, goal_id=goal_id,
        )
        existing = {agent["work_id"]: index for index, agent in enumerate(snapshot["agents"])}
        visible_leases = 0
        for item in lease_items:
            index = existing.get(item["work_id"])
            if index is not None:
                snapshot["agents"][index] = item
                visible_leases += 1
            elif len(snapshot["agents"]) < _MAX_AGENTS:
                snapshot["agents"].append(item)
                visible_leases += 1
        if projected_leases > visible_leases:
            warnings.append(f"Showing {visible_leases} of {projected_leases} lease-projected agents.")
        if missing_execution_receipts:
            warnings.append(
                f"{missing_execution_receipts} leased work unit(s) have no linked execution receipt; "
                "record a native Codex plan and start before importing telemetry."
            )
        snapshot["summary"]["agents"] += new_lease_agents
        snapshot["summary"]["running_agents"] += lease_running

        event_rows, events_total = _limited_rows(
            connection,
            "SELECT sequence,event_type,goal_id,work_unit_id,created_at FROM audit_events" + (" WHERE goal_id=?" if goal_id is not None else "") + " ORDER BY sequence DESC",
            (goal_id,) if goal_id is not None else (), _MAX_EVENTS,
        )
        if events_total > len(event_rows):
            warnings.append(f"Showing latest {len(event_rows)} of {events_total} events.")
        snapshot["events"] = [
            {"id": _as_int(row["sequence"]), "event_type": str(row["event_type"]), "goal_id": row["goal_id"],
             "work_unit_id": row["work_unit_id"], "created_at": str(row["created_at"])}
            for row in event_rows
        ]
        control = connection.execute("SELECT emergency_stopped FROM runtime_control WHERE id=1").fetchone()
        snapshot["runtime"] = {
            "available": True, "emergency_stopped": bool(control and control["emergency_stopped"]), "message": None,
        }
        snapshot["goal_options"] = [{"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])} for row in option_rows]
        snapshot["global_summary"] = dict(snapshot["summary"])
        linked_work_ids = {str(row["id"]) for row in connection.execute("SELECT id FROM work_units WHERE goal_id=?", (goal_id,))} if goal_id is not None else set()
        try:
            # Analytics has a wider bound than visible rows, while never
            # materializing an unbounded receipt history in the portal process.
            records = list(islice(ExecutionStore(project_root).iter_records(goal_id=goal_id, linked_work_ids=scoped_work_ids) or (), _MAX_ANALYTICS_RECORDS + 1))
            analytics_truncated = len(records) > _MAX_ANALYTICS_RECORDS
            records = records[:_MAX_ANALYTICS_RECORDS]
            snapshot["analytics"] = _analytics(records, goal_id=goal_id, work_ids=linked_work_ids, model=model, role=role, since=_parse_since(since), truncated=analytics_truncated)
            scoped_records = [record for record in records if record.get("state") != "planned" and (goal_id is None or record.get("goal_id") == goal_id or (record.get("parent_work_id") or record.get("work_id")) in linked_work_ids)]
            snapshot["efficiency"] = summarize_executions(scoped_records)
        except (ExecutionError, OSError, sqlite3.Error, ValueError):
            snapshot["analytics"] = None
        _apply_goal_scope(snapshot, goal_id)
        return _with_insights(snapshot, project_root, goal_id=goal_id, runtime_schema_version=version)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        warnings.append("Runtime database could not be read safely.")
        return _with_insights(snapshot, project_root, goal_id=goal_id, runtime_schema_version=runtime_schema_version)
    finally:
        connection.close()


def _is_local_host(value: str | None, port: int) -> bool:
    if not value:
        return False
    return value.casefold() in {"127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"}


def _is_local_origin(value: str, port: int) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "http" and parsed.path == "" and not parsed.query and not parsed.fragment and _is_local_host(parsed.netloc, port)
    except ValueError:
        return False


def make_portal_server(root: Path, *, port: int = 8765) -> ThreadingHTTPServer:
    """Create a loopback-only server for the fixed portal routes."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("portal port must be an integer from 0 through 65535")
    project_root = Path(root).resolve()
    assets = Path(__file__).with_name("portal_static")

    class PortalHandler(BaseHTTPRequestHandler):
        server_version = "TasktraPortal/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # Portal requests are local UI noise, and response logs can reveal
            # user-supplied Host headers.
            return

        def _headers(self, content_type: str | None = None, *, api: bool = False, length: int = 0) -> None:
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store" if api else "no-cache")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))

        def _reject(self, status: HTTPStatus) -> None:
            body = (status.phrase + "\n").encode("utf-8")
            self.send_response(status)
            if status == HTTPStatus.METHOD_NOT_ALLOWED:
                self.send_header("Allow", "GET, HEAD")
            self._headers("text/plain; charset=utf-8", length=len(body))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _allowed_request(self) -> bool:
            if not _is_local_host(self.headers.get("Host"), self.server.server_port):
                self._reject(HTTPStatus.BAD_REQUEST)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and not _is_local_origin(origin, self.server.server_port):
                self._reject(HTTPStatus.FORBIDDEN)
                return False
            return True

        def _get(self) -> None:
            if not self._allowed_request():
                return
            parsed = urlsplit(self.path)
            if parsed.fragment:
                self._reject(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/snapshot":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"goal_id", "model", "role", "since"} or any(len(values) != 1 or not values[0] or len(values[0]) > 128 for values in query.values()):
                    self._reject(HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = json.dumps(portal_snapshot(project_root, **{key: values[0] for key, values in query.items()}), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                except ValueError:
                    self._reject(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self._headers("application/json; charset=utf-8", api=True, length=len(body))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if parsed.path == "/api/agent-activity":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) != {"work_id"} or len(query["work_id"]) != 1 or not query["work_id"][0] or len(query["work_id"][0]) > 128:
                    self._reject(HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = json.dumps(agent_activity(project_root, query["work_id"][0]), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                except ValueError:
                    self._reject(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self._headers("application/json; charset=utf-8", api=True, length=len(body))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            item = _STATIC_ROUTES.get(parsed.path)
            if item is None:
                self._reject(HTTPStatus.NOT_FOUND)
                return
            filename, content_type = item
            path = assets / filename
            if not path.is_file():
                self._reject(HTTPStatus.NOT_FOUND)
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._reject(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, length=len(body))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            self._get()

        def do_HEAD(self) -> None:
            self._get()

        def do_POST(self) -> None:
            if self._allowed_request():
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_PUT(self) -> None:
            if self._allowed_request():
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_DELETE(self) -> None:
            if self._allowed_request():
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_OPTIONS(self) -> None:
            if self._allowed_request():
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

    return ThreadingHTTPServer(("127.0.0.1", port), PortalHandler)
