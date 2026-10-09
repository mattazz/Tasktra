"""Pure, bounded contracts for operator intervention requests and responses.

Runtime transitions validate these envelopes before binding them to a leased
attempt. Read projections are imported lazily to keep that dependency acyclic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .contracts import ContractError, validate_named
from .handoffs import HandoffError, validate_handoff
from .identifiers import IdentifierError, require_identifier


INTERVENTION_REQUEST_KIND = "tasktra.intervention-request"
INTERVENTION_RESPONSE_KIND = "tasktra.intervention-response"
INTERVENTION_VERSION = 1
MAX_INTERVENTION_CANONICAL_BYTES = 16 * 1024
_SECRET_FIELD_MARKERS = ("access", "api_key", "authorization", "bearer", "cookie", "credential", "password", "secret", "token")
_SECRET_VALUE = re.compile(r"(?i)(?:\bbearer\s+\S+|\b(?:authorization|access[-_ ]?token|api[-_ ]?key|token|password|cookie|credential|secret)\s*[:=]\s*\S+)")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![\w])[a-z]:[\\/]")
_UNIX_ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?![/\s])")
_FILE_OR_UNC_PATH = re.compile(r"(?i)\bfile:[\\/]+[^\\/\s]|(?<![\w\\])\\\\[^\\\s]+\\|(?<![\w:/])//[^/\s]+")


class InterventionError(ContractError):
    """Raised when intervention data cannot safely cross a durable boundary."""


__all__ = [
    "INTERVENTION_REQUEST_KIND",
    "INTERVENTION_RESPONSE_KIND",
    "INTERVENTION_VERSION",
    "MAX_INTERVENTION_CANONICAL_BYTES",
    "InterventionError",
    "intervention_inbox",
    "intervention_detail",
    "intervention_response_history",
    "canonical_intervention_request",
    "canonical_intervention_response",
    "intervention_request_sha256",
    "intervention_response_sha256",
    "load_intervention_request",
    "load_intervention_response",
    "request_from_handoff",
    "validate_intervention_request",
    "validate_intervention_response",
]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise InterventionError("Duplicate JSON object key")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise InterventionError("Non-finite JSON value is not permitted")


def _require_identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise InterventionError(str(error)) from error


def _secret_shaped(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        parsed = None
    return bool(_SECRET_VALUE.search(value)) or (
        parsed is not None and parsed.scheme in {"http", "https"}
        and (parsed.username is not None or parsed.password is not None or any(
            any(marker in key.casefold() for marker in _SECRET_FIELD_MARKERS)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ))
    )


def _require_safe_text(value: str, *, label: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise InterventionError(f"{label} must be text")
    if value != value.strip():
        raise InterventionError(f"{label} must not have leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise InterventionError(f"{label} must not contain control characters")
    if _secret_shaped(value):
        raise InterventionError(f"{label} must not contain credentials or secrets")
    if any(pattern.search(value) for pattern in (_WINDOWS_ABSOLUTE_PATH, _UNIX_ABSOLUTE_PATH, _FILE_OR_UNC_PATH)):
        raise InterventionError(f"{label} must not contain an absolute path")


def _require_safe_relative_locator(value: str, *, label: str) -> None:
    _require_safe_text(value, label=label)
    if "\\" in value or ":" in value or any(ord(character) < 32 for character in value):
        raise InterventionError(f"{label} must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise InterventionError(f"{label} escapes the project root")
    if any(part.endswith((".", " ")) for part in path.parts):
        raise InterventionError(f"{label} contains a Windows-ambiguous path component")


def _validate_evidence_refs(value: list[Any]) -> None:
    evidence_ids: set[str] = set()
    for evidence in value:
        evidence_id = _require_identifier(evidence["id"], label="evidence id")
        if evidence_id in evidence_ids:
            raise InterventionError("Evidence reference ids must be unique")
        evidence_ids.add(evidence_id)
        locator = evidence["locator"]
        _require_safe_text(evidence["summary"], label="evidence summary")
        if evidence["kind"] in {"file", "artifact"}:
            _require_safe_relative_locator(locator, label="evidence locator")
        elif evidence["kind"] == "url":
            _require_safe_text(locator, label="evidence locator")
            try:
                parsed = urlparse(locator)
            except ValueError as error:
                raise InterventionError("Evidence URL must be a safe HTTPS URL without query or fragment") from error
            if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or any(ord(char) < 32 for char in locator)):
                raise InterventionError("Evidence URL must be a safe HTTPS URL without query or fragment")
        else:
            _require_safe_text(locator, label="evidence locator")


def _canonical(value: Mapping[str, Any], *, label: str) -> str:
    try:
        canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded = canonical.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise InterventionError(f"{label} must be JSON-compatible") from error
    if len(encoded) > MAX_INTERVENTION_CANONICAL_BYTES:
        raise InterventionError(f"{label} canonical JSON exceeds the {MAX_INTERVENTION_CANONICAL_BYTES}-byte limit")
    return canonical


def _validate_request_semantics(value: dict[str, Any]) -> None:
    _require_identifier(value["request_id"], label="request_id")
    for name in ("goal_id", "work_unit_id", "attempt_id"):
        _require_identifier(value["source"][name], label=name)
    _require_identifier(value["producer"]["actor_id"], label="producer actor_id")
    for name in ("prompt", "rationale", "impact"):
        _require_safe_text(value[name], label=name)
    if value["requires_human_approval"] != (value["outcome_class"] == "approval-required"):
        raise InterventionError("requires_human_approval must match outcome_class")
    _validate_evidence_refs(value["evidence_refs"])


def _validate_response_semantics(value: dict[str, Any]) -> None:
    _require_identifier(value["response_id"], label="response_id")
    _require_identifier(value["request"]["request_id"], label="request_id")
    expected = value["expected_current_response"]
    if expected is not None:
        _require_identifier(expected["response_id"], label="expected response_id")
    _require_identifier(value["responder"]["actor_id"], label="responder actor_id")
    _require_safe_text(value["answer"], label="answer")
    _require_safe_text(value["rationale"], label="rationale", allow_empty=True)
    _validate_evidence_refs(value["evidence_refs"])


def _validate(value: Mapping[str, Any], *, schema: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InterventionError(f"{label} must be a JSON object")
    try:
        copied = copy.deepcopy(dict(value))
        validate_named(copied, schema)
    except ContractError as error:
        raise InterventionError(f"Invalid {label} contract") from error
    if schema == "intervention-request":
        _validate_request_semantics(copied)
    else:
        _validate_response_semantics(copied)
    _canonical(copied, label=label)
    return copied


def validate_intervention_request(value: Mapping[str, Any]) -> dict[str, Any]:
    return _validate(value, schema="intervention-request", label="intervention request")


def validate_intervention_response(value: Mapping[str, Any]) -> dict[str, Any]:
    return _validate(value, schema="intervention-response", label="intervention response")


def canonical_intervention_request(value: Mapping[str, Any]) -> str:
    return _canonical(validate_intervention_request(value), label="intervention request")


def canonical_intervention_response(value: Mapping[str, Any]) -> str:
    return _canonical(validate_intervention_response(value), label="intervention response")


def intervention_request_sha256(value: Mapping[str, Any]) -> str:
    return sha256(canonical_intervention_request(value).encode("utf-8")).hexdigest()


def intervention_response_sha256(value: Mapping[str, Any]) -> str:
    return sha256(canonical_intervention_response(value).encode("utf-8")).hexdigest()


def _load(payload: str | bytes, *, validator: Any, label: str) -> dict[str, Any]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_INTERVENTION_CANONICAL_BYTES:
            raise InterventionError(f"{label} payload exceeds the {MAX_INTERVENTION_CANONICAL_BYTES}-byte limit")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InterventionError(f"{label} bytes must be UTF-8") from error
    if not isinstance(payload, str):
        raise InterventionError(f"{label} payload must be text or UTF-8 bytes")
    try:
        if len(payload.encode("utf-8")) > MAX_INTERVENTION_CANONICAL_BYTES:
            raise InterventionError(f"{label} payload exceeds the {MAX_INTERVENTION_CANONICAL_BYTES}-byte limit")
    except UnicodeEncodeError as error:
        raise InterventionError(f"{label} text must be valid UTF-8") from error
    try:
        loaded = json.loads(payload, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, TypeError) as error:
        raise InterventionError(f"Invalid {label} JSON") from error
    return validator(loaded)


def load_intervention_request(payload: str | bytes) -> dict[str, Any]:
    return _load(payload, validator=validate_intervention_request, label="intervention request")


def load_intervention_response(payload: str | bytes) -> dict[str, Any]:
    return _load(payload, validator=validate_intervention_response, label="intervention response")


def request_from_handoff(
    handoff: Mapping[str, Any],
    *,
    request_id: str,
    attempt_id: str,
    blocker_id: str,
    action_id: str,
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one request draft from explicitly selected, validated handoff fields.

    The converter is intentionally pure: it neither reads handoff files nor
    consults live attempt state.  Yield remains the authoritative binding step.
    """
    try:
        accepted = validate_handoff(handoff)
    except HandoffError as error:
        raise InterventionError("Handoff is not valid for an intervention request") from error
    if accepted["status"]["state"] not in {"blocked", "paused"}:
        raise InterventionError("Handoff status must be blocked or paused")
    work_unit_id = accepted["source"]["work_unit_id"]
    if work_unit_id is None:
        raise InterventionError("Handoff source must include a work_unit_id")
    _require_identifier(request_id, label="request_id")
    _require_identifier(attempt_id, label="attempt_id")
    _require_identifier(blocker_id, label="blocker_id")
    _require_identifier(action_id, label="action_id")
    blockers = {item["id"]: item for item in accepted["blockers"]}
    actions = {item["id"]: item for item in accepted["requested_actions"]}
    if blocker_id not in blockers:
        raise InterventionError("Selected blocker is not present in the handoff")
    if action_id not in actions:
        raise InterventionError("Selected requested action is not present in the handoff")
    if isinstance(evidence_ids, (str, bytes)) or not isinstance(evidence_ids, Sequence):
        raise InterventionError("evidence_ids must be a sequence of selected identifiers")
    selected_ids = list(evidence_ids)
    for evidence_id in selected_ids:
        _require_identifier(evidence_id, label="evidence id")
    if len(selected_ids) > 8 or len(selected_ids) != len(set(selected_ids)):
        raise InterventionError("Selected evidence ids must be unique and contain at most eight items")
    available_evidence = {item["id"]: item for item in accepted["evidence_refs"]}
    selected: list[dict[str, Any]] = []
    for evidence_id in selected_ids:
        if evidence_id not in available_evidence:
            raise InterventionError("Selected evidence is not present in the handoff")
        selected.append(copy.deepcopy(available_evidence[evidence_id]))
    blocker, action = blockers[blocker_id], actions[action_id]
    impact = blocker["summary"]
    candidate = f"{impact} {blocker['next_action']}"
    if len(candidate) <= 500:
        impact = candidate
    draft = {
        "kind": INTERVENTION_REQUEST_KIND,
        "version": INTERVENTION_VERSION,
        "request_id": request_id,
        "source": {"goal_id": accepted["source"]["goal_id"], "work_unit_id": work_unit_id, "attempt_id": attempt_id},
        "producer": {"actor_id": accepted["producer"]["actor_id"]},
        "outcome_class": "approval-required" if action["requires_human_approval"] else "blocked",
        "prompt": action["action"],
        "rationale": action["rationale"],
        "impact": impact,
        "requires_human_approval": action["requires_human_approval"],
        "evidence_refs": selected,
    }
    return validate_intervention_request(draft)


def intervention_inbox(store: Any, *, goal_id: str | None = None, work_unit_id: str | None = None,
                       include_closed: bool = False, include_legacy: bool = True,
                       limit: int = 20, offset: int = 0, at: Any = None) -> dict[str, Any]:
    from .intervention_views import intervention_inbox as project
    return project(store, goal_id=goal_id, work_unit_id=work_unit_id, include_closed=include_closed,
                   include_legacy=include_legacy, limit=limit, offset=offset, at=at)


def intervention_detail(store: Any, request_id: str, *, at: Any = None) -> dict[str, Any]:
    from .intervention_views import intervention_detail as project
    return project(store, request_id, at=at)


def intervention_response_history(store: Any, request_id: str, *, after_revision: int = 0,
                                  limit: int = 20, at: Any = None) -> dict[str, Any]:
    from .intervention_views import intervention_response_history as project
    return project(store, request_id, after_revision=after_revision, limit=limit, at=at)
