"""Test-only builders for content-bound local human approval records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tasktra.authority import transition_approval_subject_sha256


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def v3_approval_kwargs(
    *,
    approval_id: str,
    goal_id: str,
    work_unit_id: str | None,
    action: str,
    effect: str,
    scope: dict[str, Any],
    resource_scope: dict[str, Any] | None,
    envelope_sha256: str,
    approver_id: str,
    performer_id: str,
    valid_until: datetime,
    attested_at: datetime,
    authority_clause: str = "human approval",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return kwargs whose provenance binds the exact approval subject."""
    bound_evidence = evidence or []
    subject = {
        "kind": "tasktra.transition-approval",
        "version": 3,
        "approval_id": approval_id,
        "goal_id": goal_id,
        "work_unit_id": work_unit_id,
        "action": action,
        "effect": effect,
        "scope": scope,
        "resource_scope": resource_scope,
        "envelope_sha256": envelope_sha256,
        "decision": "approved",
        "approver": {"kind": "human", "id": approver_id},
        "performer_id": performer_id,
        "authority_clause": authority_clause,
        "evidence": bound_evidence,
        "valid_until": _timestamp(valid_until),
        "revoked_at": None,
    }
    return {
        "approval_id": approval_id,
        "authority_clause": authority_clause,
        "evidence": bound_evidence,
        "provenance": {
            "kind": "local-human-ceremony",
            "attester_id": approver_id,
            "subject_sha256": transition_approval_subject_sha256(subject),
            "attested_at": _timestamp(attested_at),
        },
    }
