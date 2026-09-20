"""Deterministic, model-free selection of the smallest requested core role."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any
from urllib.parse import urlparse

from .contracts import ContractError, validate_named
from .compiler import Catalog
from .ecosystem import route_specialists as _route_specialists
from .handoffs import HandoffError, _require_safe_relative_path, validate_handoff
from .identifiers import IdentifierError, require_identifier, require_optional_identifier


CORE_ROLES = frozenset({"scout", "writer", "implementer", "tester", "reviewer", "goal-steward", "escalation"})
_SIGNAL_ROLES = {
    "inspect": "scout",
    "write": "writer",
    "change": "implementer",
    "validate": "tester",
    "review": "reviewer",
    "authority": "goal-steward",
    "escalate": "escalation",
}


class RoutingError(ContractError):
    """Raised for ambiguous, invalid, or unbounded routing input."""


def route_specialists(
    catalog: Catalog,
    enabled_packs: tuple[str, ...] | list[str],
    signals: tuple[str, ...] | list[str],
    *,
    require_implementation_validation: bool = False,
    require_independent_review: bool = False,
) -> dict[str, Any]:
    """Route explicit domain signals through the active pack profile."""
    return _route_specialists(
        catalog,
        enabled_packs,
        signals,
        require_implementation_validation=require_implementation_validation,
        require_independent_review=require_independent_review,
    )


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise RoutingError(str(error)) from error


def _validated(value: Mapping[str, Any], schema: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RoutingError(f"{schema} must be an object")
    copied = copy.deepcopy(dict(value))
    try:
        validate_named(copied, schema)
    except ContractError as error:
        raise RoutingError(str(error)) from error
    return copied


def _validate_request_evidence(request: dict[str, Any]) -> None:
    _identifier(request["task_id"], label="task_id")
    _identifier(request["source"]["goal_id"], label="goal_id")
    try:
        require_optional_identifier(request["source"]["work_unit_id"], label="work_unit_id")
    except IdentifierError as error:
        raise RoutingError(str(error)) from error
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in request["evidence_refs"]:
        evidence_id = evidence["id"]
        _identifier(evidence_id, label="evidence id")
        if evidence_id in evidence_by_id:
            raise RoutingError(f"Duplicate routing evidence id: {evidence_id!r}")
        _validate_evidence_locator(evidence)
        evidence_by_id[evidence_id] = evidence
    for fact in request["verified_facts"]:
        unknown = set(fact["evidence_ids"]) - set(evidence_by_id)
        if unknown:
            raise RoutingError(
                "Routing facts reference unknown evidence ids: "
                + ", ".join(sorted(unknown))
            )


def _validate_evidence_locator(evidence: dict[str, Any]) -> None:
    """Apply the handoff evidence locator boundary to routing input too."""
    evidence_id = evidence["id"]
    locator = evidence["locator"]
    if any(ord(character) < 32 for character in locator):
        raise RoutingError(f"Evidence {evidence_id!r} locator contains a control character")
    if evidence["kind"] in {"file", "artifact"}:
        try:
            _require_safe_relative_path(locator, label=f"evidence {evidence_id!r} locator")
        except HandoffError as error:
            raise RoutingError(str(error)) from error
    elif evidence["kind"] == "url":
        parsed = urlparse(locator)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise RoutingError(f"Evidence {evidence_id!r} must use a safe https URL")


def route_task(request: Mapping[str, Any]) -> dict[str, Any]:
    """Route by an explicit primary signal; no natural-language inference occurs."""
    validated = _validated(request, "routing-request")
    _validate_request_evidence(validated)
    signal = validated["primary_signal"]
    if signal not in validated["signals"]:
        raise RoutingError("primary_signal must also be present in signals")
    role = _SIGNAL_ROLES[signal]
    decision = {
        "kind": "tasktra.routing-decision",
        "version": 1,
        "task_id": validated["task_id"],
        "role": role,
        "primary_signal": signal,
        "rationale": f"Explicit primary signal '{signal}' selects the smallest matching core role '{role}'.",
    }
    _validated(decision, "routing-decision")
    return copy.deepcopy(decision)


def _compact_unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            result.append(value)
            seen.add(key)
        if len(result) == limit:
            break
    return result


def _compact_facts(
    values: list[dict[str, Any]], limit: int, *, evidence_limit: int
) -> list[dict[str, Any]]:
    """Select whole facts only, preserving every citation within a brief budget."""
    facts: list[dict[str, Any]] = []
    by_statement: dict[str, dict[str, Any]] = {}
    selected_evidence: set[str] = set()
    for value in values:
        key = value["statement"].casefold()
        existing = by_statement.get(key)
        if existing is None:
            if len(facts) == limit:
                continue
            evidence_ids = list(value["evidence_ids"])
            if len(selected_evidence | set(evidence_ids)) > evidence_limit:
                continue
            existing = {
                "statement": value["statement"],
                "evidence_ids": evidence_ids,
            }
            facts.append(existing)
            by_statement[key] = existing
            selected_evidence.update(evidence_ids)
            continue
        # The first selected copy is authoritative. Merging another duplicate's
        # citations could exceed the brief's evidence budget or make evidence
        # selection dependent on a partial fact.
    return facts


def build_brief(
    request: Mapping[str, Any], decision: Mapping[str, Any], *, handoff: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build a bounded brief using only explicitly supplied, verified context."""
    routed = _validated(request, "routing-request")
    selected = _validated(decision, "routing-decision")
    if routed["task_id"] != selected["task_id"]:
        raise RoutingError("routing decision belongs to a different task")
    expected = route_task(routed)
    if selected != expected:
        raise RoutingError("routing decision is not the deterministic decision for this request")

    facts = copy.deepcopy(routed["verified_facts"])
    constraints = list(routed["constraints"])
    next_steps: list[str] = []
    handoff_id: str | None = None
    evidence_by_id = {item["id"]: copy.deepcopy(item) for item in routed["evidence_refs"]}
    if handoff is not None:
        try:
            accepted = validate_handoff(handoff)
        except HandoffError as error:
            raise RoutingError(str(error)) from error
        if accepted["source"] != routed["source"]:
            raise RoutingError("handoff source does not match routing request")
        handoff_id = accepted["handoff_id"]
        facts.extend(copy.deepcopy(accepted["verified_facts"]))
        constraints.extend(accepted["downstream_brief"]["constraints"])
        next_steps.extend(accepted["downstream_brief"]["recommended_next_steps"])

        for evidence in accepted["evidence_refs"]:
            current = evidence_by_id.get(evidence["id"])
            if current is not None and current != evidence:
                raise RoutingError(
                    f"Handoff evidence id conflicts with routing evidence: {evidence['id']!r}"
                )
            evidence_by_id[evidence["id"]] = copy.deepcopy(evidence)

    compact_facts = _compact_facts(facts, 8, evidence_limit=8)
    referenced_evidence_ids = [
        evidence_id
        for fact in compact_facts
        for evidence_id in fact["evidence_ids"]
    ]
    evidence_refs = []
    seen_evidence: set[str] = set()
    for evidence_id in referenced_evidence_ids:
        if evidence_id in seen_evidence:
            continue
        try:
            evidence_refs.append(copy.deepcopy(evidence_by_id[evidence_id]))
        except KeyError as error:  # Defensive: handoff/request validators should prevent this.
            raise RoutingError(f"Brief references unknown evidence id: {evidence_id!r}") from error
        seen_evidence.add(evidence_id)

    brief = {
        "kind": "tasktra.workflow-brief",
        "version": 1,
        "source": copy.deepcopy(routed["source"]),
        "target_role": selected["role"],
        "objective": routed["objective"],
        "verified_facts": compact_facts,
        "evidence_refs": evidence_refs,
        "constraints": _compact_unique(constraints, 12),
        "recommended_next_steps": _compact_unique(next_steps, 8),
        "source_handoff_id": handoff_id,
    }
    _validated(brief, "workflow-brief")
    return copy.deepcopy(brief)
