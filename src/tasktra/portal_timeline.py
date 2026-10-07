"""Bounded, evidence-only timeline projections for the local portal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


MAX_TIMELINE_ROWS = 500


def _time(value: Any) -> tuple[str | None, float | None]:
    if not isinstance(value, str) or len(value) > 64:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None, None
        normalized = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rank = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None, None
    return normalized, rank


def _text(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = " ".join(value.split())
    return value[:256] if value else fallback


def _field(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _attempt_row(item: dict[str, Any]) -> dict[str, Any] | None:
    attempt_id, job_id, goal_id = item.get("id"), item.get("work_unit_id"), item.get("goal_id")
    if not all(isinstance(value, str) for value in (attempt_id, job_id, goal_id)):
        return None
    started_at, started_rank = _time(item.get("acquired_at"))
    ended_at, ended_rank = _time(item.get("ended_at"))
    elapsed = item.get("elapsed_ms")
    valid_elapsed = isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0
    completed = started_at is not None and ended_at is not None and ended_rank is not None and started_rank is not None and ended_rank >= started_rank and valid_elapsed
    timing = "recorded_duration" if completed else "open_attempt" if started_at is not None and ended_at is None and item.get("status") in {"leased", "active"} else "unknown"
    if timing == "unknown":
        started_at = ended_at = started_rank = ended_rank = None
    return {
        "id": f"job_attempt:{attempt_id}", "kind": "job_attempt", "label": _text(item.get("job_title"), job_id),
        "goal_id": goal_id, "job_id": job_id, "work_id": job_id,
        "state": _text(item.get("outcome_class"), _text(item.get("status"), "unknown")), "model": None, "role": None,
        "started_at": started_at, "ended_at": ended_at, "last_observed_at": None,
        "duration_ms": elapsed if completed else None, "timing": timing,
        "target": {"type": "job", "id": job_id}, "_start": started_rank, "_end": ended_rank,
    }


def _agent_row(item: dict[str, Any]) -> dict[str, Any] | None:
    work_id = item.get("work_id") if isinstance(item.get("work_id"), str) else item.get("id")
    if not isinstance(work_id, str):
        return None
    started_at, started_rank = _time(item.get("started_at"))
    observed_at, observed_rank = _time(item.get("last_observed_at"))
    if started_rank is not None and observed_rank is not None and observed_rank < started_rank:
        started_at = observed_at = started_rank = observed_rank = None
    timing = "observation_window" if started_at is not None or observed_at is not None else "unknown"
    role = _field(item.get("role"))
    short_id = work_id if len(work_id) <= 18 else f"{work_id[:9]}…{work_id[-8:]}"
    label = f"{role or 'agent'} · {short_id}"
    return {
        "id": f"agent:{work_id}", "kind": "agent", "label": label,
        "goal_id": item.get("goal_id") if isinstance(item.get("goal_id"), str) else None,
        "job_id": item.get("job_id") if isinstance(item.get("job_id"), str) else None,
        "work_id": work_id, "state": _text(item.get("state"), "unknown"),
        "model": _field(item.get("model")), "role": role,
        "started_at": started_at, "ended_at": None, "last_observed_at": observed_at,
        "duration_ms": None, "timing": timing,
        "target": {"type": "agent", "id": work_id}, "_start": started_rank, "_end": observed_rank,
    }


def build_timeline(snapshot: dict[str, Any], attempts: Iterable[dict[str, Any]], *, attempt_total: int | None = None) -> dict[str, Any]:
    """Return the contract's timeline v1 using only already scoped records."""
    attempts = list(attempts)
    rows = [row for row in (_attempt_row(item) for item in attempts if isinstance(item, dict)) if row is not None]
    rows.extend(row for row in (_agent_row(item) for item in snapshot.get("agents", []) if isinstance(item, dict)) if row is not None)
    warnings = snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else []
    rows.sort(key=lambda row: (row["_start"] is None, row["_start"] if row["_start"] is not None else 0, row["id"]))
    recorded_attempts = attempt_total if isinstance(attempt_total, int) and not isinstance(attempt_total, bool) else len(attempts)
    recorded_agents = snapshot.get("summary", {}).get("agents", len(snapshot.get("agents", [])))
    if not isinstance(recorded_agents, int) or isinstance(recorded_agents, bool):
        recorded_agents = len(snapshot.get("agents", []))
    total = max(recorded_attempts, len(attempts)) + max(recorded_agents, len(snapshot.get("agents", [])))
    shown = rows[:MAX_TIMELINE_ROWS]
    known = [value for row in shown for value in (row["_start"], row["_end"]) if value is not None]
    for row in shown:
        row.pop("_start", None)
        row.pop("_end", None)
    start_at = None if not known else datetime.fromtimestamp(min(known), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    end_at = None if not known else datetime.fromtimestamp(max(known), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"version": 1, "rows": shown, "total": total, "shown": len(shown), "partial": bool(warnings) or total > len(shown), "start_at": start_at, "end_at": end_at}
