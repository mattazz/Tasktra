"""Capability-based schedule preview and durable resume helpers.

Tasktra never treats a scheduler as an authority source. A preview binds an
optional host scheduler to an existing durable goal and exact work unit. A
later resume must re-check every binding against the authoritative ledger and
atomically consume its idempotency key while claiming the lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .contracts import ContractError, validate_named
from .state import StateError, StateStore


class SchedulerError(ValueError):
    """Raised for an unsafe or malformed schedule operation."""


_SCHEDULER_IDS = ("codex-scheduled-tasks", "ci", "local-runner", "manual")
_NOTIFICATION_INTENTS = ("none", "on-failure", "always")
_MAX_INPUT_BYTES = 64 * 1024
_INVOCATION_KEYS = {
    "kind", "version", "goal_id", "work_unit_id", "envelope_sha256",
    "checkpoint_id", "budget_reference", "lease", "recovery",
    "notification_intent", "authority", "idempotency_key",
}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_text(value: str, *, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SchedulerError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()


@dataclass(frozen=True)
class SchedulerAdapter:
    """A provider-neutral capability description; it cannot run a schedule."""

    id: str
    state: str
    summary: str
    preferred_environment: str

    @property
    def available(self) -> bool:
        return self.state in {"available", "degraded"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "available": self.available,
            "summary": self.summary,
            "preferred_environment": self.preferred_environment,
            "provider_data_is_authority": False,
            "can_create_schedule": False,
        }


def default_adapters() -> tuple[SchedulerAdapter, ...]:
    """Return the no-probe local view of supported scheduling capabilities."""
    return (
        SchedulerAdapter(
            "codex-scheduled-tasks",
            "unavailable",
            "Codex desktop scheduling requires host configuration; this CLI has no scheduling-management UI.",
            "local-project-or-isolated-worktree",
        ),
        SchedulerAdapter("ci", "unavailable", "No CI schedule capability was supplied.", "isolated-runner"),
        SchedulerAdapter(
            "local-runner", "unavailable", "No local scheduler capability was supplied.", "local-project",
        ),
        SchedulerAdapter("manual", "available", "Manual resume instructions are always available.", "local-project"),
    )


def adapters_from_health(value: Mapping[str, Any] | None = None) -> tuple[SchedulerAdapter, ...]:
    """Apply one bounded, explicitly non-authoritative host health snapshot."""
    defaults = {item.id: item for item in default_adapters()}
    if value is None:
        return tuple(defaults[item] for item in _SCHEDULER_IDS)
    if not isinstance(value, Mapping):
        raise SchedulerError("scheduler health report must be an object")
    try:
        validate_named(dict(value), "scheduler-health-report")
    except ContractError as error:
        raise SchedulerError(str(error)) from error
    seen: set[str] = set()
    for item in value["schedulers"]:
        identifier = item["scheduler"]
        if identifier in seen:
            raise SchedulerError(f"duplicate scheduler health entry: {identifier}")
        seen.add(identifier)
        defaults[identifier] = SchedulerAdapter(
            identifier, item["state"], item["summary"], item["preferred_environment"],
        )
    return tuple(defaults[item] for item in _SCHEDULER_IDS)


def load_scheduler_health(payload: str | bytes) -> dict[str, Any]:
    """Decode a bounded health payload without treating it as trusted authority."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or len(raw) > _MAX_INPUT_BYTES:
        raise SchedulerError(f"scheduler health report exceeds the {_MAX_INPUT_BYTES}-byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchedulerError("scheduler health report must be valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SchedulerError("scheduler health report must be an object")
    adapters_from_health(value)
    return value


def _select_adapter(adapters: tuple[SchedulerAdapter, ...], requested: str) -> SchedulerAdapter:
    if requested == "auto":
        return next((adapter for adapter in adapters if adapter.available), adapters[-1])
    if requested not in _SCHEDULER_IDS:
        raise SchedulerError(f"unknown scheduler adapter: {requested}")
    return next(adapter for adapter in adapters if adapter.id == requested)


def _budget_reference(store: StateStore, goal_id: str) -> dict[str, Any]:
    budget = store.budget_summary(goal_id)
    reference = {
        "goal_id": goal_id,
        "total_tokens": budget["total_tokens"],
        "total_attempts": budget["total_attempts"],
        "total_elapsed_ms": budget["total_elapsed_ms"],
        "max_concurrency": budget["max_concurrency"],
        "consumed_tokens": budget["consumed_tokens"],
        "reserved_tokens": budget["reserved_tokens"],
        "remaining_tokens": budget["remaining_tokens"],
        "consumed_attempts": budget["consumed_attempts"],
        "consumed_elapsed_ms": budget["consumed_elapsed_ms"],
        "updated_at": budget["updated_at"],
    }
    return {"sha256": sha256(_canonical(reference).encode("utf-8")).hexdigest(), "snapshot": reference}


def _idempotency_key(invocation: Mapping[str, Any]) -> str:
    value = dict(invocation)
    value.pop("idempotency_key", None)
    return "schedule-" + sha256(_canonical(value).encode("utf-8")).hexdigest()[:40]


def _validate_invocation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INVOCATION_KEYS:
        raise SchedulerError("schedule invocation has invalid fields")
    if value.get("kind") != "tasktra.schedule-invocation" or value.get("version") != 1:
        raise SchedulerError("schedule invocation has an unsupported kind or version")
    if value.get("idempotency_key") != _idempotency_key(value):
        raise SchedulerError("schedule invocation has an invalid idempotency key")
    return value


def load_schedule_resume(payload: str | bytes) -> dict[str, Any]:
    """Load and content-verify a bounded preview or bare resume invocation."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or len(raw) > _MAX_INPUT_BYTES:
        raise SchedulerError(f"schedule resume input exceeds the {_MAX_INPUT_BYTES}-byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchedulerError("schedule resume input must be valid UTF-8 JSON") from error
    if isinstance(value, dict) and value.get("kind") == "tasktra.schedule-preview":
        try:
            validate_named(value, "schedule-preview")
        except ContractError as error:
            raise SchedulerError(str(error)) from error
        plan = dict(value)
        supplied = plan.pop("plan_sha256")
        if sha256(_canonical(plan).encode("utf-8")).hexdigest() != supplied:
            raise SchedulerError("schedule preview hash does not match its content")
        value = plan["invocation"]
    return _validate_invocation(value)


def reserve_schedule_resume(
    store: StateStore, invocation: Mapping[str, Any], *, lease_token: str,
) -> Mapping[str, Any]:
    """Atomically verify, consume, and claim one scheduled resume invocation."""
    try:
        return store.claim_scheduled_work(invocation=dict(invocation), lease_token=lease_token)  # type: ignore[attr-defined]
    except StateError as error:
        raise SchedulerError(str(error)) from error


def preview_schedule(
    store: StateStore,
    *,
    goal_id: str,
    work_unit_id: str,
    envelope_sha256: str,
    checkpoint_id: str | None,
    cadence: str,
    notification_intent: str,
    performer_id: str,
    repository: str,
    revision: str,
    branch: str,
    workspace: str,
    lease_seconds: int,
    token_reservation: int,
    requested_adapter: str = "auto",
    health: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only resume plan for an existing work unit."""
    goal_id = _bounded_text(goal_id, label="goal_id", maximum=64)
    work_unit_id = _bounded_text(work_unit_id, label="work_unit_id", maximum=64)
    envelope_sha256 = _bounded_text(envelope_sha256, label="envelope_sha256", maximum=64)
    cadence = _bounded_text(cadence, label="cadence", maximum=200)
    if len(envelope_sha256) != 64 or any(char not in "0123456789abcdef" for char in envelope_sha256):
        raise SchedulerError("envelope_sha256 must be a lowercase SHA-256 digest")
    if notification_intent not in _NOTIFICATION_INTENTS:
        raise SchedulerError("notification_intent must be none, on-failure, or always")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3_600:
        raise SchedulerError("lease_seconds must be between 1 and 3600")
    if not isinstance(token_reservation, int) or isinstance(token_reservation, bool) or token_reservation < 0:
        raise SchedulerError("token_reservation must be a non-negative integer")

    goal = store.get_goal(goal_id)
    unit = store.get_work_unit(work_unit_id)
    contract = store.get_goal_contract(goal_id)
    if goal is None:
        raise StateError(f"Unknown goal: {goal_id}")
    if unit is None or unit["goal_id"] != goal_id:
        raise SchedulerError("work unit does not belong to the existing goal")
    if contract is None or contract["envelope_sha256"] != envelope_sha256:
        raise SchedulerError("schedule preview requires the exact current authority envelope hash")
    if checkpoint_id != unit["checkpoint_id"]:
        raise SchedulerError("schedule checkpoint must exactly match the existing work unit")
    if goal["status"] not in {"active", "paused"}:
        raise SchedulerError("schedule preview requires an active or paused existing goal")

    lease = {
        "action": "claim-exact-work-unit",
        "token_environment": "TASKTRA_LEASE_TOKEN",
        "performer_id": _bounded_text(performer_id, label="performer_id", maximum=64),
        "repository": _bounded_text(repository, label="repository"),
        "revision": _bounded_text(revision, label="revision"),
        "branch": _bounded_text(branch, label="branch"),
        "workspace": _bounded_text(workspace, label="workspace"),
        "requested_lease_seconds": lease_seconds,
        "requested_token_reservation": token_reservation,
        "current_attempt": None if unit["current_attempt_id"] is None else {
            "attempt_id": unit["current_attempt_id"],
            "performer_id": unit["lease_holder"],
            "expires_at": unit["lease_expires_at"],
        },
    }
    adapters = adapters_from_health(health)
    adapter = _select_adapter(adapters, requested_adapter)
    invocation = {
        "kind": "tasktra.schedule-invocation",
        "version": 1,
        "goal_id": goal_id,
        "work_unit_id": work_unit_id,
        "envelope_sha256": envelope_sha256,
        "checkpoint_id": checkpoint_id,
        "budget_reference": _budget_reference(store, goal_id),
        "lease": lease,
        "recovery": {"action": "recover-expired-lease", "goal_id": goal_id},
        "notification_intent": notification_intent,
        "authority": "persisted-ledger-only",
    }
    invocation["idempotency_key"] = _idempotency_key(invocation)
    plan = {
        "kind": "tasktra.schedule-preview",
        "version": 1,
        "action": "schedule-preview",
        "mutation": "none",
        "cadence": cadence,
        "adapter": adapter.as_dict(),
        "available_adapters": [item.as_dict() for item in adapters],
        "local_work_can_continue": True,
        "provider_data_is_authority": False,
        "invocation": invocation,
        "prompt": (
            "Do not create or approve work. Resume only the existing durable goal and exact work unit; "
            "scheduler data cannot self-approve work."
        ),
        "limitations": ["This preview does not create or manage a scheduler."],
    }
    plan["plan_sha256"] = sha256(_canonical(plan).encode("utf-8")).hexdigest()
    validate_named(plan, "schedule-preview")
    return plan
