"""Fail-closed, bounded verified-outcomes projection for the read-only portal."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlsplit

from .codex_usage import USAGE_FIELDS
from .state import validate_deterministic_review_completion_evidence
from .workflow import WorkflowError, is_workflow_complete, load_workflow, validate_workflow_completion_token

MAX_JOBS = 500
MAX_ATTEMPTS = 5_000
MAX_WORKFLOW_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_PORTAL_WORKFLOW_BYTES = 16 * 1024
MAX_PORTAL_EVIDENCE_BYTES = 16 * 1024
MAX_CHECKS = 200
MAX_OBJECT_DEPTH = 32
_SHA = re.compile(r"[0-9a-f]{40}")
_GITHUB_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_GITHUB_NUMBER = re.compile(r"[1-9][0-9]*")
_PROJECT_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/ -]*")
_CHECK_STATUS = {"passed", "failed", "unavailable", "timed_out", "partial", "running"}


def empty_outcomes(goal_id: str | None, *, partial: bool = True) -> dict[str, Any]:
    return {"version": 1, "goal_id": goal_id, "jobs": [], "total": 0, "shown": 0, "partial": partial,
            "summary": {"completed_jobs": 0, "verified_jobs": 0, "measured_verified_jobs": 0,
                        "attributable_runs": 0, "unattributed_runs": 0, "measured_tokens_for_verified_jobs": None,
                        "tokens_per_measured_verified_job": None, "unknown_usage_runs": 0, "partial": partial}, "by_model": []}


def _time(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128: return None
    try: datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return None
    return value


def _timed_duration(attempt: sqlite3.Row) -> int | None:
    """Accept only ordered timestamps with the same explicit timezone class."""
    if attempt["status"] != "finished" or not isinstance(attempt["elapsed_ms"], int) or isinstance(attempt["elapsed_ms"], bool) or attempt["elapsed_ms"] < 0:
        return None
    start, end = _time(attempt["acquired_at"]), _time(attempt["ended_at"])
    if start is None or end is None: return None
    try:
        start_dt, end_dt = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
        # Never compare a naive historical timestamp with an aware one.  Both
        # naive timestamps are retained as their recorded local ordering.
        if (start_dt.tzinfo is None) != (end_dt.tzinfo is None): return None
        if start_dt.tzinfo is not None:
            start_dt, end_dt = start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc)
        return int(attempt["elapsed_ms"]) if end_dt >= start_dt else None
    except (OverflowError, ValueError, TypeError):
        return None


def _object(value: Any, limit: int) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit: return None
    try: result = json.loads(value)
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError): return None
    if not isinstance(result, dict): return None
    # JSON decoder recursion limits differ across Python versions/platforms.
    # Apply our own iterative bound so the same evidence has the same verdict.
    pending = [(result, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_OBJECT_DEPTH: return None
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return result


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict): return None
    # The trusted portal agent projection deliberately publishes only the four
    # public counters.  Preserve its exact job_id linkage without requiring
    # hidden counters that the projection intentionally omits.
    if set(("total_tokens", "input_tokens", "cached_input_tokens", "output_tokens")).issubset(value) and not set(USAGE_FIELDS).issubset(value):
        candidate = {"total_tokens": value["total_tokens"], "input_tokens": value["input_tokens"], "cached_input_tokens": value["cached_input_tokens"], "output_tokens": value["output_tokens"], "cache_write_input_tokens": 0, "reasoning_output_tokens": 0}
        value = candidate
    if any(not isinstance(value.get(k), int) or isinstance(value[k], bool) or value[k] < 0 for k in USAGE_FIELDS): return None
    if value["total_tokens"] != value["input_tokens"] + value["output_tokens"]: return None
    if value["cached_input_tokens"] > value["input_tokens"] or value["cache_write_input_tokens"] > value["input_tokens"] or value["reasoning_output_tokens"] > value["output_tokens"]: return None
    return {k: value[k] for k in USAGE_FIELDS}


def _safe_path(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 256 or value != value.strip() or not _PROJECT_PATH.fullmatch(value): return None
    return value if all(part not in {"", ".", ".."} for part in value.split("/")) else None


def _safe_link(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 512 or any(ord(char) < 32 or char == "%" for char in value): return None
    try: parsed = urlsplit(value)
    except ValueError: return None
    try: invalid = parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
    except ValueError: return None
    if invalid: return None
    parts = parsed.path.split("/")
    repository = len(parts) >= 3 and all(_GITHUB_SEGMENT.fullmatch(part) and part not in {".", ".."} for part in parts[1:3])
    if len(parts) == 5 and parts[0] == "" and repository and parts[3] == "commit" and _SHA.fullmatch(parts[4]): return value
    if len(parts) == 5 and parts[0] == "" and repository and parts[3] == "pull" and _GITHUB_NUMBER.fullmatch(parts[4]): return value
    if len(parts) == 6 and parts[0] == "" and repository and parts[3:5] == ["actions", "runs"] and _GITHUB_NUMBER.fullmatch(parts[5]): return value
    return None


def _checks(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if value is None: return [], False
    if not isinstance(value, list) or len(value) > MAX_CHECKS: return [], True
    result = []
    for item in value:
        if not isinstance(item, dict): return [], True
        name, status, code, elapsed = item.get("name"), item.get("status"), item.get("exit_code"), item.get("elapsed_ms")
        if not isinstance(name, str) or not 0 < len(name) <= 256 or not isinstance(status, str) or status not in _CHECK_STATUS or not (isinstance(code, int) and not isinstance(code, bool) or code is None) or not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0: return [], True
        result.append({"name": name, "status": status, "exit_code": code, "elapsed_ms": elapsed})
    return result, False


def _deliverables(value: Any) -> tuple[list[dict[str, str]], bool]:
    if value is None: return [], False
    if not isinstance(value, list) or len(value) > 100: return [], True
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "value"}: return [], True
        kind, raw = item["kind"], item["value"]
        valid = _SHA.fullmatch(raw) if kind == "commit" and isinstance(raw, str) else _safe_path(raw) if kind == "path" else _safe_link(raw) if kind == "link" else None
        if not valid: return [], True
        result.append({"kind": kind, "value": raw})
    return result, False


def _verify(row: sqlite3.Row, evidence: dict[str, Any] | None) -> dict[str, Any]:
    policy = row["verification_policy"] if isinstance(row["verification_policy"], str) else None
    verification = {"verified": False, "policy": policy, "recorded_at": _time(row["recorded_at"]), "reason": "evidence is unavailable or invalid"}
    if row["completion_evidence_present"] and evidence is None: return verification
    text, token_text, digest = row["workflow_json"], row["completion_token_json"], row["workflow_sha256"]
    if row["status"] != "complete" or not isinstance(text, str) or not isinstance(token_text, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest): return verification
    try:
        workflow = load_workflow(text); token = _object(token_text, 16 * 1024)
        if token is None or sha256(text.encode("utf-8")).hexdigest() != digest or not is_workflow_complete(workflow): return verification
        validate_workflow_completion_token(workflow, token)
        workflow_policy = "implementation-review" if workflow.get("version") == 1 else workflow.get("verification_policy")
        if workflow.get("source") != {"goal_id": row["goal_id"], "work_unit_id": row["id"]} or workflow_policy != policy: return verification
        if policy == "implementation-deterministic-review":
            checks_text = row["acceptance_checks"]
            if not isinstance(checks_text, str) or len(checks_text.encode("utf-8")) > MAX_PORTAL_EVIDENCE_BYTES or evidence is None: return verification
            checks = json.loads(checks_text)
            validate_deterministic_review_completion_evidence(workflow, checks, evidence)
    except (TypeError, ValueError, UnicodeError, WorkflowError, RecursionError, OverflowError): return verification
    verification.update({"verified": True, "reason": "workflow completion token and policy verified"})
    return verification


def build_outcomes(connection: sqlite3.Connection, records: Iterable[dict[str, Any]], *, goal_id: str | None, records_partial: bool = False) -> dict[str, Any]:
    """Build one stable, bounded projection. Invalid evidence never raises."""
    result = empty_outcomes(goal_id, partial=records_partial)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"work_units", "work_attempts", "workflow_evidence"}.issubset(tables): return empty_outcomes(goal_id)
        total = int(connection.execute("SELECT count(*) FROM work_units WHERE (? IS NULL OR goal_id=?)", (goal_id, goal_id)).fetchone()[0])
        rows = connection.execute("""SELECT w.id,w.goal_id,w.title,w.status,w.verification_policy,
            CASE WHEN length(CAST(w.acceptance_checks AS BLOB))<=? THEN w.acceptance_checks END acceptance_checks,e.workflow_sha256,e.recorded_at,
            CASE WHEN length(CAST(e.workflow_json AS BLOB))<=? THEN e.workflow_json END workflow_json,
            CASE WHEN length(CAST(e.completion_token_json AS BLOB))<=16384 THEN e.completion_token_json END completion_token_json,
            CASE WHEN length(CAST(e.completion_evidence_json AS BLOB))<=? THEN e.completion_evidence_json END completion_evidence_json,
            e.completion_evidence_json IS NOT NULL AS completion_evidence_present
            FROM work_units w LEFT JOIN workflow_evidence e ON e.work_unit_id=w.id WHERE (? IS NULL OR w.goal_id=?) ORDER BY w.updated_at DESC,w.id LIMIT ?""", (MAX_PORTAL_EVIDENCE_BYTES, MAX_PORTAL_WORKFLOW_BYTES, MAX_PORTAL_EVIDENCE_BYTES, goal_id, goal_id, MAX_JOBS)).fetchall()
    except (sqlite3.Error, TypeError, ValueError): return empty_outcomes(goal_id)
    result.update({"total": total, "shown": len(rows), "partial": result["partial"] or total > len(rows)})
    ids = [str(row["id"]) for row in rows]; totals: dict[str, int] = defaultdict(int); attempts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    if ids:
        try:
            marks = ",".join("?" for _ in ids)
            for row in connection.execute(f"SELECT work_unit_id,count(*) total FROM work_attempts WHERE work_unit_id IN ({marks}) GROUP BY work_unit_id", ids): totals[str(row["work_unit_id"])] = int(row["total"])
            loaded = connection.execute(f"SELECT work_unit_id,status,acquired_at,ended_at,elapsed_ms FROM work_attempts WHERE work_unit_id IN ({marks}) ORDER BY work_unit_id,attempt_no DESC LIMIT ?", (*ids, MAX_ATTEMPTS + 1)).fetchall()
            if len(loaded) > MAX_ATTEMPTS: result["partial"] = True; loaded = loaded[:MAX_ATTEMPTS]
            for attempt in loaded: attempts[str(attempt["work_unit_id"])].append(attempt)
        except (sqlite3.Error, TypeError, ValueError): result["partial"] = True
    jobs: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence = _object(row["completion_evidence_json"], MAX_PORTAL_EVIDENCE_BYTES)
        verification = _verify(row, evidence)
        evidence_partial = bool(row["completion_evidence_present"] and evidence is None) or (row["verification_policy"] == "implementation-deterministic-review" and not verification["verified"])
        checks, bad_checks = _checks(None if evidence is None else evidence.get("checks")); deliverables, bad_deliverables = _deliverables(None if evidence is None else evidence.get("deliverables")); loaded = attempts[str(row["id"])]
        timed = [(item, _timed_duration(item)) for item in loaded]
        timed = [(item, duration) for item, duration in timed if duration is not None]
        timing_partial = any(item["status"] == "finished" for item in loaded) and len(timed) < sum(item["status"] == "finished" for item in loaded)
        partial_attempts = totals[str(row["id"])] > len(loaded) or timing_partial; partial = partial_attempts or evidence_partial or bad_checks or bad_deliverables
        job = {"job_id": str(row["id"]), "goal_id": str(row["goal_id"]), "title": str(row["title"]), "state": str(row["status"]), "verification": verification,
               "attempt_count": totals[str(row["id"])], "retry_count": max(totals[str(row["id"])] - 1, 0), "attempt_duration_ms": sum(duration for _, duration in timed) if timed else None, "timed_attempts": len(timed), "attempts_shown": len(loaded), "attempts_partial": partial_attempts,
               "usage": {"total_tokens": None, "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "measured_runs": 0, "unknown_runs": 0, "attributed_runs": 0, "partial": False},
               "evidence": {"workflow_sha256": row["workflow_sha256"] if isinstance(row["workflow_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["workflow_sha256"]) else None, "checks": checks, "deliverables": deliverables, "partial": partial}}
        if records_partial: job["usage"]["partial"] = True
        result["jobs"].append(job); jobs[job["job_id"]] = job; result["partial"] = result["partial"] or partial
    candidates = [record for record in records if isinstance(record, dict) and record.get("state") != "planned" and record.get("provenance") != "work-lease"]
    work_counts: dict[str, int] = defaultdict(int)
    for record in candidates:
        if isinstance(record.get("work_id"), str) and record["work_id"]: work_counts[record["work_id"]] += 1
    duplicate = any(count > 1 for count in work_counts.values()); unattributed = 0; models: dict[str, dict[str, Any]] = {}
    for record in candidates:
        # A lease fallback is live scheduling evidence, not an execution
        # receipt.  It must not become an attributed unknown-usage run.
        work_id = record.get("work_id")
        if not isinstance(work_id, str) or not work_id or work_counts[work_id] != 1: continue
        job = jobs.get(record.get("job_id"))
        if job is None: unattributed += 1; continue
        stats = job["usage"]; stats["attributed_runs"] += 1; usage = _usage(record.get("usage") if isinstance(record.get("usage"), dict) else record)
        if usage is None: stats["unknown_runs"] += 1; stats["partial"] = True; continue
        stats["measured_runs"] += 1
        for key in ("total_tokens", "input_tokens", "cached_input_tokens", "output_tokens"): stats[key] = (stats[key] or 0) + usage[key]
        model = record.get("model") if isinstance(record.get("model"), str) else record.get("observed_model")
        if job["verification"]["verified"]:
            model = model if isinstance(model, str) and 0 < len(model) <= 128 else "unknown"
            item = models.setdefault(model, {"model": model, "measured_runs": 0, "jobs": set(), "total_tokens": 0, "failed_tokens": 0, "cached": 0, "input": 0})
            item["measured_runs"] += 1; item["jobs"].add(job["job_id"]); item["total_tokens"] += usage["total_tokens"]; item["cached"] += usage["cached_input_tokens"]; item["input"] += usage["input_tokens"]
            if record.get("outcome") == "failed": item["failed_tokens"] += usage["total_tokens"]
    summary = result["summary"]; verified = [job for job in result["jobs"] if job["verification"]["verified"]]
    measured_jobs = sum(job["usage"]["measured_runs"] > 0 for job in verified)
    summary.update({"completed_jobs": sum(job["state"] == "complete" for job in result["jobs"]), "verified_jobs": len(verified), "measured_verified_jobs": measured_jobs, "attributable_runs": sum(job["usage"]["attributed_runs"] for job in verified), "unattributed_runs": unattributed, "measured_tokens_for_verified_jobs": sum(job["usage"]["total_tokens"] or 0 for job in verified) if measured_jobs else None, "unknown_usage_runs": sum(job["usage"]["unknown_runs"] for job in verified)})
    summary["tokens_per_measured_verified_job"] = summary["measured_tokens_for_verified_jobs"] / summary["measured_verified_jobs"] if summary["measured_verified_jobs"] else None
    result["partial"] = result["partial"] or duplicate; summary["partial"] = result["partial"] or any(job["usage"]["partial"] for job in result["jobs"])
    result["by_model"] = [{"model": item["model"], "measured_runs": item["measured_runs"], "verified_jobs": len(item["jobs"]), "total_tokens": item["total_tokens"], "tokens_per_measured_run": item["total_tokens"] / item["measured_runs"] if item["measured_runs"] else None, "failed_tokens": item["failed_tokens"], "cache_fraction": item["cached"] / item["input"] if item["input"] else None} for item in sorted(models.values(), key=lambda item: item["model"])]
    return result
