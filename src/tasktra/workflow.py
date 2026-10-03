"""Pure, validated implementation-to-test-to-review workflow transitions."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import hmac
import json
from typing import Any

from .contracts import ContractError, validate_named
from .handoffs import HandoffError, validate_handoff
from .identifiers import IdentifierError, require_identifier, require_optional_identifier


_POLICIES: dict[str, tuple[str, ...]] = {
    # The published Stage 2 contract remains the default for old work units.
    "implementation-review": ("implementer", "tester", "reviewer"),
    "research-review": ("author", "reviewer"),
    "documentation-review": ("author", "reviewer"),
    # This is intentionally a separately selected and authority-gated policy;
    # it is never inferred from a missing workflow.
    "deterministic-direct": (),
}
_NEXT_ROLE = {role: roles[index + 1] if index + 1 < len(roles) else None
              for roles in _POLICIES.values() for index, role in enumerate(roles)}
_WORKFLOW_COMPLETE = _POLICIES["implementation-review"]
MAX_WORKFLOW_BYTES = 256 * 1024


class WorkflowError(ContractError):
    """Raised when a handoff cannot safely advance a workflow."""


def policy_roles(verification_policy: str) -> tuple[str, ...]:
    """Return the closed host stage sequence for an authorized policy."""
    try:
        return _POLICIES[verification_policy]
    except KeyError as error:
        raise WorkflowError("workflow verification policy is not supported") from error


def _validate_source_identifiers(source: Mapping[str, Any]) -> None:
    try:
        require_identifier(source["goal_id"], label="goal_id")
        require_optional_identifier(source["work_unit_id"], label="work_unit_id")
    except (KeyError, IdentifierError) as error:
        raise WorkflowError(f"Invalid workflow source identifier: {error}") from error


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validate_workflow(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowError("workflow state must be an object")
    copied = copy.deepcopy(dict(value))
    try:
        schema = "workflow-state" if copied.get("version") == 1 else "workflow-state-v2"
        validate_named(copied, schema)
        _validate_source_identifiers(copied["source"])
        for handoff in copied["accepted_handoffs"]:
            validate_handoff(handoff)
    except (ContractError, HandoffError) as error:
        raise WorkflowError(str(error)) from error
    if len(copied["transitions"]) != len(copied["accepted_handoffs"]):
        raise WorkflowError("workflow transitions and accepted handoffs must have equal length")
    policy = "implementation-review" if copied["version"] == 1 else copied["verification_policy"]
    roles = _POLICIES.get(policy)
    if roles is None:
        raise WorkflowError("workflow verification policy is not supported")
    if not roles:
        # Direct verification has no delegated handoffs.  Its authority is
        # checked by the durable work-unit completion path, not by omission.
        if copied["transitions"] or copied["accepted_handoffs"]:
            raise WorkflowError("deterministic direct workflow cannot contain handoffs")
        if copied["status"] != "completed" or copied["current_role"] is not None:
            raise WorkflowError("deterministic direct workflow must be completed")
        return copied
    expected_role: str | None = roles[0]
    terminal_status: str | None = None
    handoff_ids: set[str] = set()
    for transition, handoff in zip(copied["transitions"], copied["accepted_handoffs"]):
        if terminal_status is not None or expected_role is None:
            raise WorkflowError("a terminal workflow cannot have later transitions")
        if handoff["source"] != copied["source"]:
            raise WorkflowError("accepted handoff source does not match workflow source")
        if handoff["producer"]["role"] != expected_role:
            raise WorkflowError("accepted handoff producer does not match the required role")
        if expected_role == "reviewer":
            prior_actors = [item["producer"]["actor_id"] for item in copied["accepted_handoffs"][:-1]]
            reviewer_actor = handoff["producer"]["actor_id"]
            if reviewer_actor in prior_actors:
                if policy == "implementation-review":
                    if reviewer_actor == prior_actors[0]:
                        raise WorkflowError("reviewer actor must differ from the implementation actor")
                    raise WorkflowError("reviewer actor must differ from the tester actor")
                raise WorkflowError("reviewer actor must differ from every prior workflow actor")
        handoff_id = handoff["handoff_id"]
        if handoff_id in handoff_ids:
            raise WorkflowError(f"workflow contains duplicate handoff id: {handoff_id}")
        handoff_ids.add(handoff_id)
        if transition["from_role"] != expected_role:
            raise WorkflowError("workflow transition role does not match the required sequence")
        if transition["handoff_id"] != handoff_id or transition["handoff_status"] != handoff["status"]["state"]:
            raise WorkflowError("workflow transition does not match its accepted handoff")
        index = roles.index(expected_role)
        expected_target = roles[index + 1] if handoff["status"]["state"] == "completed" and index + 1 < len(roles) else None
        if transition["to_role"] != expected_target:
            raise WorkflowError("workflow transition target is invalid for the handoff status")
        expected_role = expected_target
        if expected_target is None:
            terminal_status = handoff["status"]["state"]

    if not copied["transitions"]:
        expected_status, expected_role = "ready", roles[0]
    elif terminal_status == "completed":
        expected_status, expected_role = "completed", None
    elif terminal_status is not None:
        expected_status, expected_role = terminal_status, None
    else:
        expected_status = "ready"
    if copied["status"] != expected_status or copied["current_role"] != expected_role:
        raise WorkflowError("workflow status does not match its transition history")
    return copied


def new_workflow(source: Mapping[str, Any], *, verification_policy: str = "implementation-review") -> dict[str, Any]:
    """Create a pure initial state. Persistence belongs to a later runtime layer."""
    if verification_policy not in _POLICIES:
        raise WorkflowError("workflow verification policy is not supported")
    if verification_policy == "implementation-review":
        state = {
        "kind": "tasktra.implementation-workflow",
        "version": 1,
        "source": copy.deepcopy(dict(source)),
        "current_role": "implementer",
        "status": "ready",
        "transitions": [],
        "accepted_handoffs": [],
        }
    else:
        direct = verification_policy == "deterministic-direct"
        state = {
            "kind": "tasktra.implementation-workflow", "version": 2,
            "source": copy.deepcopy(dict(source)), "verification_policy": verification_policy,
            "current_role": None if direct else _POLICIES[verification_policy][0],
            "status": "completed" if direct else "ready", "transitions": [], "accepted_handoffs": [],
        }
    return _validate_workflow(state)


def accept_handoff(state: Mapping[str, Any], handoff: Mapping[str, Any]) -> dict[str, Any]:
    """Accept one role result only after its full handoff contract validates.

    Only a completed result advances to the next role. Partial, blocked, paused,
    and failed results remain in the history with their evidence and status, and
    leave the workflow non-ready for an explicit later recovery decision.
    """
    current = _validate_workflow(state)
    if current["status"] != "ready":
        raise WorkflowError(f"workflow is not ready to accept a handoff: {current['status']}")
    role = current["current_role"]
    if role not in _NEXT_ROLE:
        raise WorkflowError("workflow has no eligible current role")
    try:
        accepted = validate_handoff(handoff)
    except HandoffError as error:
        raise WorkflowError(str(error)) from error
    if accepted["source"] != current["source"]:
        raise WorkflowError("handoff source does not match workflow source")
    if accepted["producer"]["role"] != role:
        raise WorkflowError(f"handoff producer must have role {role}")

    handoff_status = accepted["status"]["state"]
    policy = "implementation-review" if current["version"] == 1 else current["verification_policy"]
    roles = _POLICIES[policy]
    index = roles.index(role)
    target = roles[index + 1] if handoff_status == "completed" and index + 1 < len(roles) else None
    next_state = copy.deepcopy(current)
    next_state["accepted_handoffs"].append(accepted)
    next_state["transitions"].append({
        "from_role": role,
        "to_role": target,
        "handoff_id": accepted["handoff_id"],
        "handoff_status": handoff_status,
    })
    next_state["current_role"] = target
    next_state["status"] = "ready" if target is not None else handoff_status
    return _validate_workflow(next_state)


def is_workflow_complete(state: Mapping[str, Any]) -> bool:
    """Return false for malformed, incomplete, or failed workflow state.

    This is intentionally a predicate rather than a status-field shortcut so a
    caller cannot mark a work item done from a forged terminal status.
    """
    try:
        validated = _validate_workflow(state)
    except WorkflowError:
        return False
    return (
        validated["status"] == "completed"
        and validated["current_role"] is None
        and (
            (validated["version"] == 2 and validated["verification_policy"] == "deterministic-direct" and not validated["transitions"])
            or tuple(item["from_role"] for item in validated["transitions"])
            == _POLICIES["implementation-review" if validated["version"] == 1 else validated["verification_policy"]]
        )
        and all(item["status"]["state"] == "completed" for item in validated["accepted_handoffs"])
    )


def workflow_completion_token(state: Mapping[str, Any]) -> dict[str, Any]:
    """Issue a content-bound token only for a fully verified terminal workflow."""
    validated = _validate_workflow(state)
    if not is_workflow_complete(validated):
        raise WorkflowError("workflow is not eligible for completion")
    policy = "implementation-review" if validated["version"] == 1 else validated["verification_policy"]
    # Direct verification has deliberately no agent handoff.  Keep the token
    # shape stable and bind this synthetic terminal marker to the source and
    # full workflow digest below, rather than indexing an empty handoff list.
    terminal_handoff_id = (
        "deterministic-direct"
        if policy == "deterministic-direct"
        else validated["accepted_handoffs"][-1]["handoff_id"]
    )
    token = {
        "kind": "tasktra.workflow-completion-token",
        "version": 1,
        "source": copy.deepcopy(validated["source"]),
        "terminal_handoff_id": terminal_handoff_id,
        "workflow_sha256": hashlib.sha256(
            _canonical_json(validated).encode("utf-8")
        ).hexdigest(),
    }
    try:
        validate_named(token, "workflow-completion-token")
    except ContractError as error:
        raise WorkflowError(str(error)) from error
    return copy.deepcopy(token)


def validate_workflow_completion_token(
    state: Mapping[str, Any], token: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify a token is bound to this exact eligible workflow state."""
    if not isinstance(token, Mapping):
        raise WorkflowError("workflow completion token must be an object")
    try:
        supplied = copy.deepcopy(dict(token))
        validate_named(supplied, "workflow-completion-token")
    except ContractError as error:
        raise WorkflowError(str(error)) from error
    expected = workflow_completion_token(state)
    supplied_json = _canonical_json(supplied)
    expected_json = _canonical_json(expected)
    if not hmac.compare_digest(supplied_json, expected_json):
        raise WorkflowError("workflow completion token does not match this workflow")
    return copy.deepcopy(expected)


def serialize_workflow(state: Mapping[str, Any]) -> str:
    """Return a canonical, deterministic representation of validated workflow state."""
    return _canonical_json(_validate_workflow(state))


def load_workflow(payload: str | bytes) -> dict[str, Any]:
    """Load a bounded untrusted workflow document with duplicate-key rejection."""
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WorkflowError("workflow text must be valid UTF-8") from error
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise WorkflowError("workflow payload must be text or UTF-8 bytes")
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowError(f"workflow payload exceeds the {MAX_WORKFLOW_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8")
        loaded = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"invalid workflow JSON: {error}") from error
    return _validate_workflow(loaded)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError(f"duplicate workflow JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise WorkflowError(f"non-finite workflow value is not permitted: {value}")
