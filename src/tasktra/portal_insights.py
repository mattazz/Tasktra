"""Safe, bounded Phase 1 insight projections for the read-only portal."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


INSIGHTS_VERSION = 1
MAX_ATTENTION_ITEMS = 100
MAX_VALIDATION_REPORT_BYTES = 128 * 1024
MAX_VALIDATION_CHECKS = 200
_VALIDATION_STATUSES = {"passed", "failed", "unavailable", "timed_out", "partial", "running"}


def project_key(root: Path) -> str:
    """Return an opaque, stable key without publishing the project path."""
    resolved = Path(root).resolve(strict=False)
    return sha256(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()


def _build_version() -> str:
    try:
        return version("tasktra")
    except PackageNotFoundError:
        return "unknown"


def _build_metadata() -> dict[str, str | None]:
    """Read optional installed build metadata without exposing its location."""
    fallback = _build_version()
    path = Path(__file__).parent.parent / "BUILD.json"
    return _read_build_metadata(path, fallback)


def _read_build_metadata(path: Path, fallback: str) -> dict[str, str | None]:
    """Validate an installed BUILD document without retaining its path."""
    try:
        if _is_linklike(path) or not path.is_file():
            return {"version": fallback, "commit": None}
        with path.open("rb") as handle:
            raw = handle.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return {"version": fallback, "commit": None}
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return {"version": fallback, "commit": None}
    packaged_version = value.get("version") if isinstance(value, dict) else None
    commit = value.get("commit") if isinstance(value, dict) else None
    return {
        "version": packaged_version if isinstance(packaged_version, str) and 0 < len(packaged_version) <= 128 else fallback,
        "commit": commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None,
    }


def _is_linklike(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _safe_validation_report_path(root: Path) -> Path | None:
    """Bind the fixed report read to a non-link path below this project."""
    try:
        base = Path(root).resolve(strict=True)
        path = Path(os.path.abspath(base / ".tasktra" / "runtime" / "validation" / "latest.json"))
        relative = path.relative_to(base)
    except (OSError, ValueError):
        return None
    current = base
    for part in relative.parts:
        current = current / part
        if _is_linklike(current):
            return None
    try:
        if path.exists() and not path.is_file():
            return None
    except OSError:
        return None
    return path


def _safe_text(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 128 else None


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validation_summary(root: Path) -> dict[str, Any]:
    """Read the one fixed local report and return a strict public projection."""
    unavailable = {
        "available": False, "report_id": None, "status": None,
        "started_at": None, "finished_at": None, "checks": [], "partial": True,
    }
    path = _safe_validation_report_path(root)
    if path is None:
        return unavailable
    try:
        if not path.is_file():
            return unavailable
        with path.open("rb") as handle:
            raw = handle.read(MAX_VALIDATION_REPORT_BYTES + 1)
        if len(raw) > MAX_VALIDATION_REPORT_BYTES:
            return unavailable
        report = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return unavailable
    if not isinstance(report, dict) or report.get("kind") != "tasktra.validation-report" or report.get("version") != 1:
        return unavailable
    report_id = _safe_text(report.get("report_id"))
    status = report.get("status")
    started_at = _safe_text(report.get("started_at"))
    finished_at = _safe_text(report.get("finished_at"))
    checks = report.get("checks")
    partial = report.get("partial")
    if (report_id is None or not isinstance(status, str) or status not in _VALIDATION_STATUSES
            or not _valid_timestamp(started_at)
            or not isinstance(checks, list) or len(checks) > MAX_VALIDATION_CHECKS
            or not isinstance(partial, bool)):
        return unavailable
    if status == "running":
        if finished_at is not None or not partial:
            return unavailable
    elif not _valid_timestamp(finished_at):
        return unavailable
    public_checks: list[dict[str, Any]] = []
    has_truncated_output = False
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            return unavailable
        item_index = item.get("index")
        item_status = item.get("status")
        exit_code = item.get("exit_code")
        elapsed_ms = item.get("elapsed_ms")
        stdout_truncated = item.get("stdout_truncated")
        stderr_truncated = item.get("stderr_truncated")
        if (not isinstance(item_index, int) or isinstance(item_index, bool) or item_index != index
                or not isinstance(item_status, str) or item_status not in _VALIDATION_STATUSES
                or not (isinstance(exit_code, int) and not isinstance(exit_code, bool) or exit_code is None)
                or not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool) or elapsed_ms < 0
                or not isinstance(stdout_truncated, bool) or not isinstance(stderr_truncated, bool)
                or item_status == "passed" and exit_code != 0):
            return unavailable
        has_truncated_output = has_truncated_output or stdout_truncated or stderr_truncated
        public_checks.append({"index": item_index, "status": item_status, "exit_code": exit_code, "elapsed_ms": elapsed_ms})
    if has_truncated_output and not partial:
        return unavailable
    if status == "passed" and (not public_checks or partial or has_truncated_output or any(item["status"] != "passed" for item in public_checks)):
        return unavailable
    return {
        "available": True, "report_id": report_id, "status": status,
        "started_at": started_at, "finished_at": finished_at,
        "checks": public_checks, "partial": partial or status == "partial",
    }


def _timestamp_rank(value: Any) -> float:
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _attention_item(*, item_id: str, severity: str, category: str, title: str, detail: str,
                    goal_id: Any = None, job_id: Any = None, work_id: Any = None,
                    occurred_at: Any = None, target_type: str, target_id: Any = None) -> dict[str, Any]:
    return {
        "id": item_id, "severity": severity, "category": category, "title": title, "detail": detail,
        "goal_id": goal_id if isinstance(goal_id, str) else None,
        "job_id": job_id if isinstance(job_id, str) else None,
        "work_id": work_id if isinstance(work_id, str) else None,
        "occurred_at": occurred_at if isinstance(occurred_at, str) else None,
        "target": {"type": target_type, "id": target_id if isinstance(target_id, str) else None},
    }


def _attention(snapshot: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for job in snapshot.get("jobs", []):
        if not isinstance(job, dict):
            continue
        job_id, goal_id = job.get("id"), job.get("goal_id")
        if not isinstance(job_id, str):
            continue
        occurred_at = job.get("updated_at")
        if job.get("status") == "blocked":
            items.append(_attention_item(
                item_id=f"blocked-job:{job_id}", severity="error", category="blocked-job",
                title="Job is blocked", detail="This job is currently recorded as blocked.",
                goal_id=goal_id, job_id=job_id, work_id=job_id, occurred_at=occurred_at,
                target_type="job", target_id=job_id,
            ))
        elif job.get("status") == "leased" and job.get("lease_stale") is True:
            items.append(_attention_item(
                item_id=f"expired-lease:{job_id}", severity="warning", category="expired-lease",
                title="Work lease expired", detail="This leased job has passed its recorded expiry.",
                goal_id=goal_id, job_id=job_id, work_id=job_id, occurred_at=job.get("lease_expires_at"),
                target_type="job", target_id=job_id,
            ))
        outcome = job.get("last_outcome_class")
        if isinstance(outcome, str) and outcome in {"failed", "permanent", "transient", "exhausted"}:
            items.append(_attention_item(
                item_id=f"historical-failure:{job_id}:{outcome}", severity="info", category="historical-failure",
                title="Recorded prior failure", detail="A prior attempt recorded a failure outcome; this is not current blockage.",
                goal_id=goal_id, job_id=job_id, work_id=job_id, occurred_at=occurred_at,
                target_type="job", target_id=job_id,
            ))
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict) or agent.get("state") == "planned":
            continue
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            continue
        work_id = agent.get("work_id")
        canonical_id = work_id if isinstance(work_id, str) else agent_id
        common = {
            "goal_id": agent.get("goal_id"), "job_id": agent.get("job_id"), "work_id": work_id,
            "occurred_at": agent.get("last_observed_at"), "target_type": "agent", "target_id": canonical_id,
        }
        if not isinstance(agent.get("total_tokens"), int):
            items.append(_attention_item(item_id=f"missing-usage:{canonical_id}", severity="info", category="missing-usage",
                                         title="Usage is not recorded", detail="This observed agent has no measured usage.", **common))
        if not isinstance(agent.get("model"), str):
            items.append(_attention_item(item_id=f"missing-model:{canonical_id}", severity="info", category="missing-model",
                                         title="Model is not recorded", detail="This observed agent has no measured model.", **common))
        if not isinstance(agent.get("job_id"), str):
            items.append(_attention_item(item_id=f"missing-job:{canonical_id}", severity="info", category="missing-job",
                                         title="Job association is not recorded", detail="This observed agent is not linked to a runtime job.", **common))
        if agent.get("state") in {"failed", "permanent", "transient", "exhausted", "timed_out"}:
            items.append(_attention_item(
                item_id=f"historical-agent-failure:{canonical_id}", severity="info", category="historical-agent-failure",
                title="Recorded run failure", detail="This run recorded a failure outcome; it is not current blockage.",
                **common,
            ))
    if coverage["partial"]:
        items.append(_attention_item(
            item_id="partial-coverage", severity="warning", category="partial-coverage",
            title="Coverage is partial", detail="Some runtime or telemetry records could not be fully projected.",
            target_type="diagnostics", target_id=None,
        ))
    priority = {"error": 0, "warning": 1, "info": 2}
    items.sort(key=lambda item: (priority[item["severity"]], -_timestamp_rank(item["occurred_at"]), item["id"]))
    shown = items[:MAX_ATTENTION_ITEMS]
    return {"items": shown, "total": len(items), "shown": len(shown), "partial": len(shown) < len(items) or coverage["partial"]}


def build_insights(snapshot: dict[str, Any], root: Path, *, goal_id: str | None,
                   runtime_schema_version: int | None = None, expected_schema_version: int | None = None) -> dict[str, Any]:
    """Project only stable, safe diagnostics from an already-safe snapshot."""
    agents = [item for item in snapshot.get("agents", []) if isinstance(item, dict)]
    measurable = [item for item in agents if item.get("state") != "planned"]
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    warnings = snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else []
    loaded_total = summary.get("agents") if isinstance(summary.get("agents"), int) else len(agents)
    partial = not runtime.get("available", False) or bool(warnings) or loaded_total > len(agents)
    coverage = {
        "goal_id": goal_id, "loaded_agents": len(agents),
        "measured_agents": sum(isinstance(item.get("total_tokens"), int) for item in measurable),
        "unknown_usage_agents": sum(not isinstance(item.get("total_tokens"), int) for item in measurable),
        "missing_model_agents": sum(not isinstance(item.get("model"), str) for item in measurable),
        "missing_job_agents": sum(not isinstance(item.get("job_id"), str) for item in measurable),
        "partial": partial,
    }
    observed = [item.get("last_observed_at") for item in agents if isinstance(item.get("last_observed_at"), str)]
    observed.sort(key=_timestamp_rank, reverse=True)
    return {
        "version": INSIGHTS_VERSION,
        "attention": _attention(snapshot, coverage),
        "diagnostics": {
            "build": _build_metadata(),
            "runtime": {
                "available": bool(runtime.get("available", False)), "schema_version": runtime_schema_version,
                "expected_schema_version": expected_schema_version,
                "emergency_stopped": bool(runtime.get("emergency_stopped", False)),
            },
            "coverage": coverage,
            "telemetry": {"last_observed_at": observed[0] if observed else None},
            "validation": validation_summary(root),
        },
    }
