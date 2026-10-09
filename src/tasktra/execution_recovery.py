"""Bounded, observational reconciliation for unresolved Codex host runs.

This module deliberately records only receipts already represented by schema 14.
It never creates execution or lifecycle authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from contextlib import contextmanager
from typing import Any, Mapping

from .autonomy import AutonomyError, AutonomyStore, _contains_persisted_lease_token
from .state import SCHEMA_VERSION, StateError, StateStore, _identifier, _timestamp

_MAX_OBSERVATION_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 64 * 1024
_CANONICAL = re.compile(r"/[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*)*$")


def _error(message: str) -> None:
    raise AutonomyError(message)


@contextmanager
def _readonly_snapshot(store: Any):
    """Use AutonomyStore's strict snapshot when available; retain StateStore compatibility."""
    opener = getattr(store, "_readonly_connection", None)
    if opener is not None:
        with opener() as connection:
            yield connection
    else:
        with store._connection(write=False) as connection:
            yield connection


def _as_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _scan_strings(value: Any, token_hash: str, token_length: int | None) -> bool:
    if isinstance(value, str):
        return _contains_persisted_lease_token(value, token_hash, token_length)
    if isinstance(value, Mapping):
        return any(_scan_strings(key, token_hash, token_length) or _scan_strings(item, token_hash, token_length)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_scan_strings(item, token_hash, token_length) for item in value)
    return False


def _validate_observation(observation: Any, *, result_bytes: bytes | None) -> tuple[dict[str, Any], str | None]:
    """Validate the intentionally closed v1 list_agents adapter vocabulary."""
    if not isinstance(observation, Mapping):
        _error("observation must be an object")
    try:
        encoded = json.dumps(dict(observation), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise AutonomyError("observation must be JSON-compatible") from error
    if len(encoded) > _MAX_OBSERVATION_BYTES:
        _error("observation exceeds the 32768-byte limit")
    if set(observation) != {"kind", "version", "source", "captured_at", "parent_canonical_name", "observed_agent_names", "target"}:
        _error("observation has unsupported fields")
    if observation.get("kind") != "tasktra.codex-host-tree-observation" or type(observation.get("version")) is not int or observation.get("version") != 1:
        _error("observation adapter version is unsupported")
    if observation.get("source") != "collaboration.list_agents":
        _error("observation source is not supported")
    captured_at = observation.get("captured_at")
    parent = observation.get("parent_canonical_name")
    names = observation.get("observed_agent_names")
    target = observation.get("target")
    if not isinstance(captured_at, str): _error("observation captured_at is required")
    try:
        canonical_capture = _timestamp(captured_at)
    except (TypeError, ValueError, OverflowError, StateError) as error: raise AutonomyError("observation captured_at is invalid") from error
    if captured_at != canonical_capture:
        _error("observation captured_at must be canonical UTC")
    if not isinstance(parent, str) or len(parent) > 256 or _CANONICAL.fullmatch(parent) is None:
        _error("observation parent canonical name is invalid")
    if not isinstance(names, list) or not 1 <= len(names) <= 100 or any(not isinstance(name, str) or len(name) > 256 or _CANONICAL.fullmatch(name) is None for name in names) or len(set(names)) != len(names):
        _error("observation agent names are invalid")
    if not isinstance(target, Mapping) or set(target) != {"canonical_name", "agent_id", "status"}:
        _error("observation target is invalid")
    canonical_name, agent_id, status = target.get("canonical_name"), target.get("agent_id"), target.get("status")
    if not isinstance(canonical_name, str) or len(canonical_name) > 256 or _CANONICAL.fullmatch(canonical_name) is None:
        _error("observation target canonical name is invalid")
    # list_agents exposes no actual opaque agent id in this adapter version.
    if agent_id is not None:
        _error("observation target agent id must be null")
    if not isinstance(status, Mapping) or set(status) != {"kind", "source_shape"}:
        _error("observation target status is invalid")
    kind, source_shape = status.get("kind"), status.get("source_shape")
    if not isinstance(kind, str) or not isinstance(source_shape, str) or (kind, source_shape) not in {("running", "running-string"), ("completed", "completed-object")}:
        _error("observation target status is unsupported")
    if kind == "running":
        if result_bytes is not None: _error("running observation cannot include result bytes")
        digest = None
    else:
        if not isinstance(result_bytes, bytes): _error("completed observation requires result bytes")
        if len(result_bytes) > _MAX_RESULT_BYTES: _error("result exceeds the 65536-byte limit")
        try: result_bytes.decode("utf-8")
        except UnicodeDecodeError as error: raise AutonomyError("result must be valid UTF-8") from error
        digest = sha256(result_bytes).hexdigest()
    return {"captured_at": captured_at, "parent": parent, "names": names, "canonical_name": canonical_name,
            "agent_id": None, "kind": kind}, digest


def _verify_identity(run: Any, start: Any, normalized: Mapping[str, Any]) -> None:
    canonical_name = normalized["canonical_name"]
    if canonical_name not in normalized["names"] or normalized["names"].count(canonical_name) != 1:
        _error("observation target must occur exactly once")
    if start is None:
        expected = normalized["parent"].rstrip("/") + "/" + run["requested_task_name"]
        if canonical_name != expected:
            _error("observation target does not match attributed parent context and requested task")
    elif (start["host_canonical_name"], start["host_agent_id"]) != (canonical_name, None):
        _error("idempotency_conflict: observation target conflicts with recorded host identity")


def _base_projection(store: AutonomyStore, connection: Any, run: Any, *, captured: datetime | None = None) -> dict[str, Any]:
    """Reuse the existing public receipt shape, then add bounded recovery facts."""
    result = AutonomyStore._codex_projection(connection, run)
    now = captured or datetime.now(timezone.utc)
    attempt = connection.execute(
        "SELECT a.*,u.status AS unit_status,u.current_attempt_id FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id WHERE a.id=?",
        (run["attempt_id"],),
    ).fetchone()
    start = connection.execute("SELECT * FROM codex_run_starts WHERE run_id=?", (run["id"],)).fetchone()
    finish = connection.execute("SELECT * FROM codex_run_finishes WHERE run_id=?", (run["id"],)).fetchone()
    prepared = _as_utc(run["prepared_at"])
    result.update({
        "goal_id": run["goal_id"], "work_unit_id": run["work_unit_id"],
        "reason": "started-unterminated" if start is not None and finish is None else "prepared-unobserved",
        "age_seconds": max(0, int((now - prepared).total_seconds())),
        "parent": {"status": None if attempt is None else attempt["status"],
                   "is_current": attempt is not None and attempt["current_attempt_id"] == run["attempt_id"],
                   "is_live": attempt is not None and attempt["status"] == "leased" and attempt["expires_at"] > now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                   "work_unit_status": None if attempt is None else attempt["unit_status"]},
        "capacity": {"dispatch_slot_retained": finish is None,
                     "parent_lease_counted": attempt is not None and attempt["status"] == "leased",
                     "detached_goal_slot_retained": finish is None and (attempt is None or attempt["status"] != "leased")},
        "next_action": {
            "show": {"argv": ["tasktra", "delegation", "show", run["id"]]},
            "reconcile": {
                "argv_template": ["tasktra", "delegation", "reconcile", run["id"],
                                  "--actor", "<actor>", "--observation", "<observation.json>"],
                "required_inputs": ["actor", "observation"],
                "completed_result": {"flag": "--result-stdin", "transport": "exact-utf8-stdin"},
            },
        },
    })
    result["attempt"]["is_live"] = attempt is not None and attempt["status"] == "leased" and attempt["expires_at"] > now.isoformat(timespec="seconds").replace("+00:00", "Z")
    return result


def unresolved_codex_runs(store: StateStore, goal_id: str | None = None, limit: int = 50,
                          after_run_id: str | None = None, at: str | datetime | None = None) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        _error("limit must be between 1 and 100")
    if goal_id is not None: goal_id = _identifier(goal_id, label="goal_id")
    if after_run_id is not None: after_run_id = _identifier(after_run_id, label="after_run_id")
    capture = _as_utc(_timestamp(at))
    with _readonly_snapshot(store) as connection:
        AutonomyStore._prepare_readonly(connection)
        sql = "SELECT p.* FROM codex_run_preparations p LEFT JOIN codex_run_finishes f ON f.run_id=p.id WHERE f.run_id IS NULL"
        params: list[Any] = []
        if goal_id is not None: sql += " AND p.goal_id=?"; params.append(goal_id)
        if after_run_id is not None: sql += " AND p.id>?"; params.append(after_run_id)
        sql += " ORDER BY p.id LIMIT ?"; params.append(limit + 1)
        rows = connection.execute(sql, params).fetchall()
        page = rows[:limit]
        return {"items": [_base_projection(store, connection, row, captured=capture) for row in page],
                "next_after_run_id": None if len(rows) <= limit else page[-1]["id"]}


def execution_attention_counts(connection: Any, goal_id: str | None = None) -> dict[str, int]:
    """Aggregate unresolved receipt facts from an already verified snapshot."""
    clause, params = "", []
    if goal_id is not None:
        clause = " AND p.goal_id=?"; params.append(goal_id)
    row = connection.execute(
        """SELECT count(p.id) AS unresolved,
                  sum(CASE WHEN s.run_id IS NULL THEN 1 ELSE 0 END) AS prepared_unobserved,
                  sum(CASE WHEN s.run_id IS NOT NULL THEN 1 ELSE 0 END) AS started_unterminated,
                  count(DISTINCT CASE WHEN a.status IS NULL OR a.status!='leased' THEN p.attempt_id END) AS detached_capacity
           FROM codex_run_preparations p
           LEFT JOIN codex_run_starts s ON s.run_id=p.id
           LEFT JOIN codex_run_finishes f ON f.run_id=p.id
           LEFT JOIN work_attempts a ON a.id=p.attempt_id
           WHERE f.run_id IS NULL""" + clause, params).fetchone()
    return {key: int(row[key] or 0) for key in ("unresolved", "prepared_unobserved", "started_unterminated", "detached_capacity")}


def _reconciliation_projection(connection: Any, run: Any, observer_id: str) -> dict[str, Any]:
    """Distinguish this caller from the immutable receipt authors in this snapshot."""
    result = AutonomyStore._codex_projection(connection, run)
    attribution = {}
    for phase, table in (("start", "codex_run_starts"), ("finish", "codex_run_finishes")):
        row = connection.execute(
            f"SELECT observed_by,recorded_at FROM {table} WHERE run_id=?", (run["id"],),
        ).fetchone()
        attribution[phase] = None if row is None else dict(row)
    result["reconciliation_observer"] = observer_id
    result["recorded_attribution"] = attribution
    return result


def reconcile_codex_run(store: StateStore, run_id: str, observer_id: str, observation: Mapping[str, Any],
                        result_bytes: bytes | None = None, at: str | datetime | None = None) -> dict[str, Any]:
    run_id, observer_id = _identifier(run_id, label="run_id"), _identifier(observer_id, label="observer_id")
    normalized, digest = _validate_observation(observation, result_bytes=result_bytes)
    timestamp = _timestamp(at)
    # A normal StateStore write may initialize a blank path.  Recovery is an
    # observer of an existing schema-14 ledger, so reject absent and older
    # databases before entering its writer transaction.
    if not store.path.exists():
        raise StateError("runtime database cannot be opened read-only")
    with _readonly_snapshot(store) as preflight:
        version = int(preflight.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
    with store._connection() as connection:
        # Recheck under the writer lock.  This closes a replacement race
        # between the read-only preflight and StateStore's initializer.
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise StateError(f"runtime schema {version} requires migration to {SCHEMA_VERSION}")
        store._prepare_write(connection)
        append = getattr(store, "_append", AutonomyStore._append)
        run = connection.execute("SELECT * FROM codex_run_preparations WHERE id=?", (run_id,)).fetchone()
        if run is None: _error("unknown Codex run")
        attempt = connection.execute("SELECT lease_token_hash FROM work_attempts WHERE id=?", (run["attempt_id"],)).fetchone()
        if attempt is None: _error("Codex run parent attempt is missing")
        token_hash, token_length = attempt["lease_token_hash"], run["lease_token_char_length"]
        scan = {"observer_id": observer_id, "observation": observation}
        if result_bytes is not None:
            scan["result"] = result_bytes.decode("utf-8", errors="ignore")
        if _scan_strings(scan, token_hash, token_length): _error("recovery observation must not contain a lease token")
        start = connection.execute("SELECT * FROM codex_run_starts WHERE run_id=?", (run_id,)).fetchone()
        finish = connection.execute("SELECT * FROM codex_run_finishes WHERE run_id=?", (run_id,)).fetchone()
        _verify_identity(run, start, normalized)
        if finish is not None:
            if normalized["kind"] == "running":
                result = _reconciliation_projection(connection, run, observer_id)
                result.update({"mutation": "none", "idempotent": True, "attribution_preserved": True,
                               "ignored_stale_observation": True})
                return result
            expected = ("completed", "observed", digest, "unavailable", None, None)
            actual = tuple(finish[key] for key in ("outcome", "result_status", "result_sha256", "usage_status", "input_tokens", "output_tokens"))
            if actual != expected: _error("idempotency_conflict")
            result = _reconciliation_projection(connection, run, observer_id)
            result.update({"mutation": "none", "idempotent": True, "attribution_preserved": True})
            return result
        if start is not None:
            if (start["host_canonical_name"], start["host_agent_id"]) != (normalized["canonical_name"], None): _error("idempotency_conflict")
            start_new = False
        else:
            duplicate = connection.execute("SELECT 1 FROM codex_run_starts WHERE host_canonical_name=? LIMIT 1", (normalized["canonical_name"],)).fetchone()
            if duplicate is not None: _error("host identity is already bound to another Codex run")
            connection.execute("INSERT INTO codex_run_starts(run_id,host_canonical_name,host_agent_id,observed_by,recorded_at) VALUES(?,?,?,?,?)",
                               (run_id, normalized["canonical_name"], None, observer_id, timestamp))
            append(connection, "codex_run.started", goal_id=run["goal_id"], work_unit_id=run["work_unit_id"],
                          payload={"run_id": run_id, "host_canonical_name": normalized["canonical_name"], "host_agent_id": None,
                                   "observed_by": observer_id, "recorded_at": timestamp})
            start_new = True
        if normalized["kind"] == "running" and not start_new:
            result = _reconciliation_projection(connection, run, observer_id)
            result.update({"mutation": "none", "idempotent": True, "attribution_preserved": True})
            return result
        if normalized["kind"] == "completed":
            connection.execute("INSERT INTO codex_run_finishes(run_id,outcome,result_status,result_sha256,usage_status,input_tokens,output_tokens,observed_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (run_id, "completed", "observed", digest, "unavailable", None, None, observer_id, timestamp))
            append(connection, "codex_run.finished", goal_id=run["goal_id"], work_unit_id=run["work_unit_id"],
                          payload={"run_id": run_id, "outcome": "completed", "result_status": "observed", "result_sha256": digest,
                                   "usage_status": "unavailable", "input_tokens": None, "output_tokens": None,
                                   "observed_by": observer_id, "recorded_at": timestamp})
        result = _reconciliation_projection(connection, run, observer_id)
        result.update({"mutation": "applied", "idempotent": False, "attribution_preserved": not start_new})
        return result
