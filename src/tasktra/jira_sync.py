"""Optional, plan-only Jira status synchronization policy.

This module deliberately builds a closed provider request without contacting
Jira.  A configured host may later execute that request only through the
authority-bound provider-effect executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .identifiers import IdentifierError, require_identifier
from .providers import OperationDescriptor, ProviderError, ResourceScope


_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_ISSUE_KEY = re.compile(r"^([A-Z][A-Z0-9_]{0,31})-([1-9][0-9]*)$")
_EVENTS = {
    "claimed": "claim_transition",
    "review-ready": "review_transition",
    "completed": "complete_transition",
}


class JiraSyncError(ValueError):
    """The optional Jira synchronization policy is invalid or unavailable."""


def _visible_text(value: object, *, label: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise JiraSyncError(f"jira_sync.{label} must be non-empty visible text")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise JiraSyncError(f"jira_sync.{label} must be at most {maximum} visible characters")
    return value


@dataclass(frozen=True)
class JiraSyncPolicy:
    """A project's explicit, narrow mapping from Tasktra events to Jira states."""

    host: str
    project: str
    claim_transition: str
    complete_transition: str
    review_transition: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "JiraSyncPolicy":
        if not isinstance(value, Mapping):
            raise JiraSyncError("[jira_sync] must be a table")
        allowed = {"host", "project", "claim_transition", "review_transition", "complete_transition"}
        if set(value) - allowed:
            raise JiraSyncError("[jira_sync] permits only host, project, claim_transition, review_transition, and complete_transition")
        required = {"host", "project", "claim_transition", "complete_transition"}
        missing = required - set(value)
        if missing:
            raise JiraSyncError("[jira_sync] is missing required settings: " + ", ".join(sorted(missing)))
        host = _visible_text(value["host"], label="host", maximum=253)
        if host != host.casefold() or "://" in host or "/" in host or "@" in host:
            raise JiraSyncError("jira_sync.host must be a lowercase host name, not a URL")
        project = _visible_text(value["project"], label="project", maximum=32)
        if _PROJECT_KEY.fullmatch(project) is None:
            raise JiraSyncError("jira_sync.project must be an uppercase Jira project key")
        review = value.get("review_transition")
        return cls(
            host=host,
            project=project,
            claim_transition=_visible_text(value["claim_transition"], label="claim_transition"),
            review_transition=None if review is None else _visible_text(review, label="review_transition"),
            complete_transition=_visible_text(value["complete_transition"], label="complete_transition"),
        )

    def transition_for(self, event: str) -> str:
        setting = _EVENTS.get(event)
        if setting is None:
            raise JiraSyncError("jira sync event must be claimed, review-ready, or completed")
        transition = getattr(self, setting)
        if transition is None:
            raise JiraSyncError(f"jira_sync.{setting} is not configured; {event} will remain local")
        return transition


def build_sync_plan(*, policy: JiraSyncPolicy, event: str, issue: str, goal_id: str, work_unit_id: str) -> dict[str, Any]:
    """Build a deterministic, non-dispatching protocol-v2 Jira transition plan."""
    try:
        goal_id = require_identifier(goal_id, label="goal_id")
        work_unit_id = require_identifier(work_unit_id, label="work_unit_id")
    except IdentifierError as error:
        raise JiraSyncError(str(error)) from error
    if not isinstance(issue, str) or _ISSUE_KEY.fullmatch(issue) is None:
        raise JiraSyncError("issue must be an uppercase Jira issue key such as PROJ-123")
    if issue.split("-", 1)[0] != policy.project:
        raise JiraSyncError("issue must belong to jira_sync.project")
    transition = policy.transition_for(event)
    try:
        scope = ResourceScope(
            provider="jira", host=policy.host, container=policy.project,
            resource_kind="issue", resource=issue, ref=None,
        )
        descriptor = OperationDescriptor(
            "jira", "remote-mutation", capability="jira-transition",
            action=f"jira-sync-{event}", resource_scope=scope,
        )
    except ProviderError as error:
        raise JiraSyncError(str(error)) from error
    request = {"issue": issue, "transition": transition}
    key_material = json.dumps(
        {"event": event, "goal_id": goal_id, "issue": issue, "transition": transition, "work_unit_id": work_unit_id},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "event": event,
        "descriptor": descriptor.to_dict(),
        "request": request,
        "idempotency_key": "jira-sync-" + sha256(key_material).hexdigest()[:48],
        "dispatch": "requires-current-approval-and-host-executor",
        "mutation": "none",
    }
