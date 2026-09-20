"""Fail-closed authority envelopes and protected-transition approvals.

The contracts in this module are deliberately standalone.  They can be
persisted, moved between sessions, and verified before the runtime mutates a
goal or executes an effect.  They do not decide whether an approval is still
current against a clock; callers make that policy decision after validating
the signed-by-content record here.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any
from unicodedata import category

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, require_identifier, require_optional_identifier


AUTHORITY_ENVELOPE_KIND = "tasktra.authority-envelope"
AUTHORITY_ENVELOPE_VERSION = 1
AUTHORITY_ENVELOPE_V2_VERSION = 2
TRANSITION_APPROVAL_KIND = "tasktra.transition-approval"
TRANSITION_APPROVAL_VERSION = 1
TRANSITION_APPROVAL_V2_VERSION = 2
TRANSITION_APPROVAL_V3_VERSION = 3

EFFECTS = frozenset(
    {
        "read-only",
        "local-reversible-write",
        "repository-history",
        "remote-mutation",
        "external-communication",
        "deployment",
        "merge",
        "destructive",
    }
)
APPROVER_KINDS = frozenset({"human", "steward"})
DECISIONS = frozenset({"approved", "rejected", "needs_human_review"})
APPROVAL_PROVENANCE_KINDS = frozenset({"local-human-ceremony"})
APPROVAL_EVIDENCE_KINDS = frozenset(
    {"acceptance-evidence", "workflow-evidence", "checkpoint-evidence"}
)

# Bound input before parsing it.  Schema and semantic bounds then limit the
# structure that remains in memory and the data that is persisted canonically.
MAX_AUTHORITY_PAYLOAD_BYTES = 64 * 1024
MAX_BUDGET = 2_147_483_647
MAX_RESOURCE_SCOPE_STRING_LENGTH = 255
CONSEQUENTIAL_EFFECTS = frozenset(
    {
        "repository-history",
        "remote-mutation",
        "external-communication",
        "deployment",
        "merge",
        "destructive",
    }
)
LOCAL_EFFECTS = frozenset({"read-only", "local-reversible-write"})
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:access[-_]?token|api[-_]?key|authorization|credential|password|"
    r"passwd|secret|token)\s*(?:=|:)",
    re.IGNORECASE,
)


class AuthorityError(ContractError):
    """Raised when authority data is malformed, unsafe, or internally inconsistent."""


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise AuthorityError(str(error)) from error


def _optional_identifier(value: Any, *, label: str) -> str | None:
    try:
        return require_optional_identifier(value, label=label)
    except IdentifierError as error:
        raise AuthorityError(str(error)) from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AuthorityError(f"Duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise AuthorityError(f"Non-finite JSON value is not permitted: {value}")


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise AuthorityError(f"Non-finite JSON value is not permitted: {value}")
    return result


def _ensure_payload_size(payload: str | bytes) -> None:
    if isinstance(payload, bytes):
        size = len(payload)
    else:
        try:
            size = len(payload.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise AuthorityError("Authority text must be valid UTF-8") from error
    if size > MAX_AUTHORITY_PAYLOAD_BYTES:
        raise AuthorityError(
            f"Authority payload exceeds the {MAX_AUTHORITY_PAYLOAD_BYTES}-byte limit"
        )


def _load_json(payload: str | bytes, *, label: str) -> dict[str, Any]:
    if isinstance(payload, bytes):
        _ensure_payload_size(payload)
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuthorityError(f"{label} bytes must be UTF-8") from error
    if not isinstance(payload, str):
        raise AuthorityError(f"{label} payload must be text or UTF-8 bytes")
    _ensure_payload_size(payload)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, AuthorityError):
            raise
        raise AuthorityError(f"Invalid {label} JSON: {error}") from error
    if not isinstance(value, dict):
        raise AuthorityError(f"{label} must be a JSON object")
    return value


def _safe_scope_path(value: str, *, label: str, allow_project_root: bool = False) -> None:
    """Require a portable, contained project-relative path.

    Empty, root-relative, traversal, control-character, and Windows-ambiguous
    forms are rejected so an envelope's scope cannot change interpretation when
    moved between operating systems.
    """
    if allow_project_root and value == ".":
        return
    if not isinstance(value, str) or not value:
        raise AuthorityError(f"{label} must be a non-empty relative path")
    if "\\" in value or ":" in value or any(ord(char) < 32 for char in value):
        raise AuthorityError(f"{label} must be a portable relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.endswith((".", " ")) for part in path.parts)
    ):
        raise AuthorityError(f"{label} escapes the project root")


def _canonical_json(value: Mapping[str, Any], *, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AuthorityError(f"{label} is not JSON serializable: {error}") from error


def _validate_scope(value: Mapping[str, Any], *, label: str = "scope") -> None:
    """Validate an explicit portable scope shared by envelopes and approvals."""
    seen_paths: set[str] = set()
    for field in ("paths", "exclusions"):
        for path in value[field]:
            _safe_scope_path(
                path,
                label=f"{label} {field} path",
                allow_project_root=field == "paths",
            )
            key = path.casefold()
            if key in seen_paths:
                raise AuthorityError(f"Duplicate {label} path: {path!r}")
            seen_paths.add(key)


def _validate_resource_scope_string(value: Any, *, label: str) -> str:
    """Require a small, non-secret-bearing scope component.

    Resource scopes deliberately identify remote resources by their parsed
    components, rather than accepting a URL.  That keeps credentials and
    endpoint query strings out of durable authority records.
    """
    if not isinstance(value, str) or not value:
        raise AuthorityError(f"{label} must be a non-empty string")
    if len(value) > MAX_RESOURCE_SCOPE_STRING_LENGTH:
        raise AuthorityError(
            f"{label} exceeds the {MAX_RESOURCE_SCOPE_STRING_LENGTH}-character limit"
        )
    if any(category(char).startswith("C") for char in value) or any(
        char.isspace() for char in value
    ):
        raise AuthorityError(f"{label} must not contain control characters or whitespace")
    if any(char in value for char in ("@", "?", "#", "\\")) or "://" in value:
        raise AuthorityError(f"{label} must not contain URL userinfo or query components")
    if "%" in value or _CREDENTIAL_ASSIGNMENT.search(value):
        raise AuthorityError(f"{label} must not contain credential-bearing values")
    return value


def _validate_resource_scope(value: Mapping[str, Any], *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise AuthorityError(f"{label} must be an object")
    required = ("provider", "host", "container", "resource_kind", "resource", "ref")
    if set(value) != set(required):
        raise AuthorityError(f"{label} must contain exactly the resource scope fields")
    for field in ("provider", "host", "container", "resource_kind"):
        _validate_resource_scope_string(value[field], label=f"{label}.{field}")
    for field in ("resource", "ref"):
        item = value[field]
        if item is not None:
            _validate_resource_scope_string(item, label=f"{label}.{field}")


def resource_scope_within(
    child: Mapping[str, Any], parent: Mapping[str, Any]
) -> bool:
    """Return whether a resource scope is contained by an enclosing scope.

    Provider, host, container, and resource kind are identity boundaries.  A
    null resource or ref on the parent is a bounded wildcard inside those
    boundaries; a null value on the child never satisfies a specific parent.
    Invalid values raise :class:`AuthorityError` rather than comparing loosely.
    """
    _validate_resource_scope(child, label="child resource_scope")
    _validate_resource_scope(parent, label="parent resource_scope")
    for field in ("provider", "host", "container", "resource_kind"):
        if child[field] != parent[field]:
            return False
    for field in ("resource", "ref"):
        if parent[field] is not None and child[field] != parent[field]:
            return False
    return True


def _validate_resource_scopes(value: list[Mapping[str, Any]], *, label: str) -> None:
    seen: set[tuple[Any, ...]] = set()
    fields = ("provider", "host", "container", "resource_kind", "resource", "ref")
    for index, resource_scope in enumerate(value):
        item_label = f"{label}[{index}]"
        _validate_resource_scope(resource_scope, label=item_label)
        key = tuple(resource_scope[field] for field in fields)
        if key in seen:
            raise AuthorityError(f"Duplicate {label}: {resource_scope!r}")
        seen.add(key)


def _validate_envelope_semantics(value: dict[str, Any]) -> None:
    _identifier(value["goal_id"], label="goal_id")
    _identifier(value["author_id"], label="author_id")

    criteria_ids: set[str] = set()
    for criterion in value["acceptance_criteria"]:
        identifier = _identifier(criterion["id"], label="acceptance criterion id")
        if identifier in criteria_ids:
            raise AuthorityError(f"Duplicate acceptance criterion id: {identifier!r}")
        criteria_ids.add(identifier)

    _validate_scope(value["scope"])

    allowed_actions = set(value["allowed_actions"])
    prohibited_actions = set(value["prohibited_actions"])
    for action in allowed_actions:
        _identifier(action, label="allowed action")
    for action in prohibited_actions:
        _identifier(action, label="prohibited action")
    overlap = allowed_actions & prohibited_actions
    if overlap:
        raise AuthorityError(
            "allowed_actions and prohibited_actions overlap: "
            + ", ".join(sorted(overlap))
        )
    if len(allowed_actions) != len(value["allowed_actions"]):
        raise AuthorityError("allowed_actions must be unique")
    if len(prohibited_actions) != len(value["prohibited_actions"]):
        raise AuthorityError("prohibited_actions must be unique")
    if len(set(value["allowed_effects"])) != len(value["allowed_effects"]):
        raise AuthorityError("allowed_effects must be unique")

    for name in ("dependencies", "checkpoints"):
        seen: set[str] = set()
        for item in value[name]:
            identifier = _identifier(item, label=f"{name} entry")
            if identifier in seen:
                raise AuthorityError(f"{name} must be unique")
            seen.add(identifier)
    for name in ("quality_requirements", "stop_conditions", "escalation_conditions"):
        if len(set(value[name])) != len(value[name]):
            raise AuthorityError(f"{name} must be unique")

    budgets = value["budgets"]
    for name in ("attempts", "elapsed_seconds", "concurrency"):
        budget = budgets[name]
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or not 1 <= budget <= MAX_BUDGET
        ):
            raise AuthorityError(f"budgets.{name} must be between 1 and {MAX_BUDGET}")
    tokens = budgets["tokens"]
    if tokens is not None and (
        not isinstance(tokens, int)
        or isinstance(tokens, bool)
        or not 0 <= tokens <= MAX_BUDGET
    ):
        raise AuthorityError(f"budgets.tokens must be null or between 0 and {MAX_BUDGET}")

    if value["version"] == AUTHORITY_ENVELOPE_VERSION and CONSEQUENTIAL_EFFECTS.intersection(value["allowed_effects"]):
        raise AuthorityError("Authority envelope v1 cannot authorize consequential effects")
    if value["version"] == AUTHORITY_ENVELOPE_V2_VERSION:
        _validate_resource_scopes(value["resource_scopes"], label="resource_scopes")
        has_consequential_effect = bool(
            CONSEQUENTIAL_EFFECTS.intersection(value["allowed_effects"])
        )
        if has_consequential_effect and not value["resource_scopes"]:
            raise AuthorityError(
                "Consequential allowed_effects require at least one resource scope"
            )
        if not has_consequential_effect and value["resource_scopes"]:
            raise AuthorityError(
                "resource_scopes require a consequential allowed effect"
            )


def _authority_envelope_schema(value: Mapping[str, Any]) -> str:
    version = value.get("version")
    if version == AUTHORITY_ENVELOPE_VERSION:
        return "authority-envelope"
    if version == AUTHORITY_ENVELOPE_V2_VERSION:
        return "authority-envelope-v2"
    raise AuthorityError(f"Unsupported authority envelope version: {version!r}")


def validate_authority_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy an authority envelope without normalizing its meaning."""
    if not isinstance(value, Mapping):
        raise AuthorityError("Authority envelope must be a JSON object")
    try:
        copied = copy.deepcopy(dict(value))
        validate_named(copied, _authority_envelope_schema(copied))
    except ContractError as error:
        raise AuthorityError(str(error)) from error
    _validate_envelope_semantics(copied)
    encoded = _canonical_json(copied, label="Authority envelope")
    _ensure_payload_size(encoded)
    return copied


def serialize_authority_envelope(value: Mapping[str, Any]) -> str:
    """Return canonical, byte-stable JSON for a validated authority envelope."""
    return _canonical_json(
        validate_authority_envelope(value), label="Authority envelope"
    )


def authority_envelope_sha256(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 of canonical authority-envelope JSON."""
    return sha256(serialize_authority_envelope(value).encode("utf-8")).hexdigest()


def load_authority_envelope(payload: str | bytes) -> dict[str, Any]:
    """Load an untrusted authority envelope with strict JSON parser safeguards."""
    return validate_authority_envelope(_load_json(payload, label="authority envelope"))


def _parse_timestamp(value: str, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuthorityError(f"{label} must be an RFC 3339 UTC timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise AuthorityError(f"{label} must be an RFC 3339 UTC timestamp") from error


def _sha256_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise AuthorityError(f"{label} must be a lowercase SHA-256 digest")
    return value


def transition_approval_subject_sha256(value: Mapping[str, Any]) -> str:
    """Return the digest bound by a local human ceremony.

    Provenance is omitted to avoid a circular digest. This binds the ceremony
    to the exact approval subject but intentionally does not authenticate the
    claimed human identity.
    """
    if not isinstance(value, Mapping):
        raise AuthorityError("Transition approval subject must be a JSON object")
    subject = copy.deepcopy(dict(value))
    subject.pop("provenance", None)
    return sha256(
        _canonical_json(subject, label="Transition approval subject").encode("utf-8")
    ).hexdigest()


def _validate_transition_semantics(value: dict[str, Any]) -> None:
    _identifier(value["approval_id"], label="approval_id")
    _identifier(value["goal_id"], label="goal_id")
    _optional_identifier(value["work_unit_id"], label="work_unit_id")
    _identifier(value["action"], label="action")
    _identifier(value["performer_id"], label="performer_id")
    _validate_scope(value["scope"], label="approval scope")
    approver = value["approver"]
    if approver["kind"] not in APPROVER_KINDS:
        raise AuthorityError("approver.kind must be human or steward")
    approver_id = _identifier(approver["id"], label="approver.id")
    if approver_id == value["performer_id"]:
        raise AuthorityError("An approver cannot approve their own transition")
    if value["decision"] not in DECISIONS:
        raise AuthorityError("decision is not supported")
    if value["effect"] not in EFFECTS:
        raise AuthorityError("effect is not supported")
    if value["version"] == TRANSITION_APPROVAL_VERSION and value["effect"] in CONSEQUENTIAL_EFFECTS:
        raise AuthorityError("Transition approval v1 cannot authorize consequential effects")
    if value["version"] in {
        TRANSITION_APPROVAL_V2_VERSION,
        TRANSITION_APPROVAL_V3_VERSION,
    }:
        resource_scope = value["resource_scope"]
        if value["effect"] in CONSEQUENTIAL_EFFECTS:
            if resource_scope is None:
                raise AuthorityError("Consequential effects require a non-null resource_scope")
            _validate_resource_scope(resource_scope, label="resource_scope")
        elif value["effect"] in LOCAL_EFFECTS:
            if resource_scope is not None:
                raise AuthorityError(
                    "read-only and local-reversible-write effects require null resource_scope"
                )
    _sha256_digest(value["envelope_sha256"], label="envelope_sha256")
    if value["version"] == TRANSITION_APPROVAL_V3_VERSION:
        evidence_keys: set[tuple[str, str]] = set()
        for evidence in value["evidence"]:
            if not isinstance(evidence, Mapping) or set(evidence) != {
                "kind", "id", "sha256"
            }:
                raise AuthorityError(
                    "approval evidence entries must contain kind, id, and sha256"
                )
            if evidence["kind"] not in APPROVAL_EVIDENCE_KINDS:
                raise AuthorityError("approval evidence kind is not supported")
            evidence_id = _identifier(evidence["id"], label="approval evidence id")
            _sha256_digest(evidence["sha256"], label="approval evidence sha256")
            key = (evidence["kind"], evidence_id)
            if key in evidence_keys:
                raise AuthorityError("approval evidence bindings must be unique")
            evidence_keys.add(key)
        provenance = value["provenance"]
        if not isinstance(provenance, Mapping) or set(provenance) != {
            "kind", "attester_id", "subject_sha256", "attested_at"
        }:
            raise AuthorityError(
                "approval provenance must contain kind, attester_id, subject_sha256, and attested_at"
            )
        if provenance["kind"] not in APPROVAL_PROVENANCE_KINDS:
            raise AuthorityError("approval provenance kind is not supported")
        if approver["kind"] != "human":
            raise AuthorityError("local human ceremony provenance requires a human approver")
        if _identifier(provenance["attester_id"], label="provenance.attester_id") != approver_id:
            raise AuthorityError("local human ceremony attester must match approver.id")
        _sha256_digest(provenance["subject_sha256"], label="provenance.subject_sha256")
        _parse_timestamp(provenance["attested_at"], label="provenance.attested_at")
        if provenance["subject_sha256"] != transition_approval_subject_sha256(value):
            raise AuthorityError("approval provenance does not bind the approval subject")
    else:
        if len(set(value["evidence"])) != len(value["evidence"]):
            raise AuthorityError("evidence must be unique")
        for evidence_id in value["evidence"]:
            _identifier(evidence_id, label="evidence entry")

    valid_until = value["valid_until"]
    revoked_at = value["revoked_at"]
    if value["decision"] == "approved" and valid_until is None:
        raise AuthorityError("Approved transitions require valid_until")
    if value["decision"] != "approved" and (valid_until is not None or revoked_at is not None):
        raise AuthorityError("Only approved transitions may have validity timestamps")
    if valid_until is not None:
        _parse_timestamp(valid_until, label="valid_until")
    if revoked_at is not None:
        _parse_timestamp(revoked_at, label="revoked_at")


def _transition_approval_schema(value: Mapping[str, Any]) -> str:
    version = value.get("version")
    if version == TRANSITION_APPROVAL_VERSION:
        return "transition-approval"
    if version == TRANSITION_APPROVAL_V2_VERSION:
        return "transition-approval-v2"
    if version == TRANSITION_APPROVAL_V3_VERSION:
        return "transition-approval-v3"
    raise AuthorityError(f"Unsupported transition approval version: {version!r}")


def validate_transition_approval(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return an independent protected-transition approval record."""
    if not isinstance(value, Mapping):
        raise AuthorityError("Transition approval must be a JSON object")
    try:
        copied = copy.deepcopy(dict(value))
        validate_named(copied, _transition_approval_schema(copied))
    except ContractError as error:
        raise AuthorityError(str(error)) from error
    _validate_transition_semantics(copied)
    encoded = _canonical_json(copied, label="Transition approval")
    _ensure_payload_size(encoded)
    return copied


def serialize_transition_approval(value: Mapping[str, Any]) -> str:
    """Return canonical, byte-stable JSON for a transition approval."""
    return _canonical_json(
        validate_transition_approval(value), label="Transition approval"
    )


def transition_approval_sha256(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 of canonical transition-approval JSON."""
    return sha256(serialize_transition_approval(value).encode("utf-8")).hexdigest()


def load_transition_approval(payload: str | bytes) -> dict[str, Any]:
    """Load an untrusted transition approval with strict JSON parser safeguards."""
    return validate_transition_approval(_load_json(payload, label="transition approval"))
