"""Versioned, portable contracts for concise agent-to-agent handoffs."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import json
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, require_identifier, require_optional_identifier


HANDOFF_KIND = "tasktra.handoff"
HANDOFF_VERSION = 1
# Bound untrusted input before JSON decoding.  This is deliberately independent
# of the schema limits: parsers must not be asked to materialize an unbounded
# document just to discover that it is invalid.
MAX_HANDOFF_BYTES = 64 * 1024


class HandoffError(ContractError):
    """Raised when a handoff cannot safely cross an orchestration boundary."""


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise HandoffError(str(error)) from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HandoffError(f"Duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise HandoffError(f"Non-finite JSON value is not permitted: {value}")


def _require_safe_relative_path(value: str, *, label: str) -> None:
    """Accept a portable, contained path without platform-dependent aliases."""
    if not isinstance(value, str) or not value:
        raise HandoffError(f"{label} must be a non-empty relative path")
    if len(value) > 240 or "\\" in value or ":" in value:
        raise HandoffError(f"{label} must be a portable relative path")
    if any(ord(character) < 32 for character in value):
        raise HandoffError(f"{label} contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise HandoffError(f"{label} escapes the project root")
    if any(part.endswith((".", " ")) for part in path.parts):
        raise HandoffError(f"{label} contains a Windows-ambiguous path component")


def _validate_semantics(value: dict[str, Any]) -> None:
    collection_limits = {
        "verified_facts": 32,
        "inferences": 24,
        "changed_paths": 128,
        "validation_results": 32,
        "evidence_refs": 64,
        "blockers": 16,
        "requested_actions": 16,
    }
    for name, limit in collection_limits.items():
        if len(value[name]) > limit:
            raise HandoffError(f"{name} exceeds its {limit}-item limit")

    evidence_ids: set[str] = set()
    _identifier(value["handoff_id"], label="handoff_id")
    _identifier(value["source"]["goal_id"], label="goal_id")
    try:
        require_optional_identifier(value["source"]["work_unit_id"], label="work_unit_id")
    except IdentifierError as error:
        raise HandoffError(str(error)) from error
    _identifier(value["producer"]["role"], label="producer role")
    _identifier(value["producer"]["actor_id"], label="producer actor_id")
    for evidence in value["evidence_refs"]:
        evidence_id = evidence["id"]
        _identifier(evidence_id, label="evidence id")
        if evidence_id in evidence_ids:
            raise HandoffError(f"Duplicate evidence reference id: {evidence_id!r}")
        evidence_ids.add(evidence_id)
        kind = evidence["kind"]
        locator = evidence["locator"]
        if any(ord(character) < 32 for character in locator):
            raise HandoffError(f"Evidence {evidence_id!r} locator contains a control character")
        if kind in {"file", "artifact"}:
            _require_safe_relative_path(locator, label=f"evidence {evidence_id!r} locator")
        elif kind == "url":
            parsed = urlparse(locator)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise HandoffError(f"Evidence {evidence_id!r} must use a safe https URL")

    changed_paths: set[str] = set()
    for change in value["changed_paths"]:
        path = change["path"]
        _require_safe_relative_path(path, label="changed path")
        key = path.casefold()
        if key in changed_paths:
            raise HandoffError(f"Duplicate changed path: {path!r}")
        changed_paths.add(key)

    for category in ("verified_facts", "inferences", "validation_results"):
        for entry in value[category]:
            references = entry["evidence_ids"] if category != "inferences" else entry["basis"]
            if category == "verified_facts" and not references:
                raise HandoffError("Verified facts must cite at least one evidence reference")
            unknown = set(references) - evidence_ids
            if unknown:
                raise HandoffError(
                    f"{category} references unknown evidence ids: {', '.join(sorted(unknown))}"
                )

    blocker_ids = [blocker["id"] for blocker in value["blockers"]]
    for blocker_id in blocker_ids:
        _identifier(blocker_id, label="blocker id")
    if len(blocker_ids) != len(set(blocker_ids)):
        raise HandoffError("Blocker ids must be unique")
    action_ids = [action["id"] for action in value["requested_actions"]]
    for action_id in action_ids:
        _identifier(action_id, label="requested-action id")
    if len(action_ids) != len(set(action_ids)):
        raise HandoffError("Requested-action ids must be unique")

    if value["status"]["state"] == "completed":
        if not value["verified_facts"]:
            raise HandoffError(
                "Completed handoffs must include at least one evidence-backed verified fact"
            )
        disallowed = {
            result["outcome"]
            for result in value["validation_results"]
            if result["outcome"] in {"failed", "not-run"}
        }
        if disallowed:
            raise HandoffError(
                "Completed handoffs cannot report failed or not-run validation results"
            )
        if any(blocker["severity"] == "blocking" for blocker in value["blockers"]):
            raise HandoffError("Completed handoffs cannot contain blocking blockers")
        if any(action["requires_human_approval"] for action in value["requested_actions"]):
            raise HandoffError(
                "Completed handoffs cannot contain pending human-approval actions"
            )
        if value["producer"]["role"] in {"tester", "reviewer"}:
            passed_checks = [
                result
                for result in value["validation_results"]
                if result["outcome"] == "passed" and result["evidence_ids"]
            ]
            if not passed_checks:
                raise HandoffError(
                    f"Completed {value['producer']['role']} handoffs must provide at least one evidenced passed check"
                )


def _ensure_payload_size(payload: str | bytes) -> None:
    if isinstance(payload, bytes):
        size = len(payload)
    else:
        try:
            size = len(payload.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise HandoffError("Handoff text must be valid UTF-8") from error
    if size > MAX_HANDOFF_BYTES:
        raise HandoffError(
            f"Handoff payload exceeds the {MAX_HANDOFF_BYTES}-byte limit"
        )


def validate_handoff(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return an independent, JSON-compatible handoff envelope."""
    if not isinstance(value, Mapping):
        raise HandoffError("Handoff must be a JSON object")
    try:
        copied = copy.deepcopy(dict(value))
        validate_named(copied, "handoff")
    except ContractError as error:
        raise HandoffError(str(error)) from error
    _validate_semantics(copied)
    # Keep in-memory and serialized contracts aligned: a valid handoff must be
    # transferable through the bounded loader.
    try:
        encoded = json.dumps(copied, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise HandoffError(f"Handoff is not JSON serializable: {error}") from error
    _ensure_payload_size(encoded)
    return copied


def serialize_handoff(value: Mapping[str, Any]) -> str:
    """Produce canonical JSON suitable for durable, byte-stable handoff files."""
    validated = validate_handoff(value)
    try:
        return json.dumps(validated, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as error:
        raise HandoffError(f"Handoff is not JSON serializable: {error}") from error


def load_handoff(payload: str | bytes) -> dict[str, Any]:
    """Load untrusted JSON, rejecting duplicate keys and non-finite values."""
    if isinstance(payload, bytes):
        _ensure_payload_size(payload)
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HandoffError("Handoff bytes must be UTF-8") from error
    if not isinstance(payload, str):
        raise HandoffError("Handoff payload must be text or UTF-8 bytes")
    _ensure_payload_size(payload)
    try:
        loaded = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise HandoffError(f"Invalid handoff JSON: {error}") from error
    return validate_handoff(loaded)
