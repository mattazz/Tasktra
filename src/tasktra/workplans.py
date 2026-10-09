"""Strict, bounded loading for atomic Tasktra work-plan manifests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from .authority import AuthorityError, validate_project_scope
from .identifiers import IdentifierError, require_identifier, require_optional_identifier
from .state import StateError


WORK_PLAN_KIND = "tasktra.work-plan"
WORK_PLAN_VERSION = 1
MAX_WORK_PLAN_BYTES = 64 * 1024
MAX_WORK_PLAN_UNITS = 256
MAX_TITLE_CHARACTERS = 240
MAX_SCOPE_ENTRIES = 64
MAX_SCOPE_PATH_CHARACTERS = 255
MAX_SCOPE_BYTES = 4096
MAX_PREREQUISITES = 64
MAX_EDGES = 4096


class WorkPlanError(StateError):
    """A stable domain failure suitable for the work-plan command surface."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, **details: Any) -> None:
    raise WorkPlanError(code, message, details=details)


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_manifest", f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    _fail("invalid_manifest", f"non-finite JSON value is not permitted: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        _nonfinite(value)
    return result


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        _fail("invalid_manifest", str(error))


def _optional_identifier(value: Any, *, label: str) -> str | None:
    try:
        return require_optional_identifier(value, label=label)
    except IdentifierError as error:
        _fail("invalid_manifest", str(error))


def canonical_work_plan_json(manifest: Mapping[str, Any]) -> str:
    """Encode a normalized manifest in its digest-stable representation."""
    try:
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        _fail("invalid_manifest", f"manifest is not JSON serializable: {error}")


def work_plan_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256(canonical_work_plan_json(manifest).encode("utf-8")).hexdigest()


def _normalize_scope(value: Any, *, index: int) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        _fail("invalid_manifest", f"units[{index}].scope must contain exactly paths and exclusions")
    value = dict(value)
    if set(value) != {"paths", "exclusions"}:
        _fail("invalid_manifest", f"units[{index}].scope must contain exactly paths and exclusions")
    paths, exclusions = value["paths"], value["exclusions"]
    if not isinstance(paths, list) or not isinstance(exclusions, list) or not paths:
        _fail("invalid_manifest", f"units[{index}].scope must have nonempty paths and a list of exclusions")
    if len(paths) + len(exclusions) > MAX_SCOPE_ENTRIES:
        _fail("limit_exceeded", "work-plan scope has too many entries", maximum=MAX_SCOPE_ENTRIES)
    if not all(isinstance(path, str) and len(path) <= MAX_SCOPE_PATH_CHARACTERS for path in [*paths, *exclusions]):
        _fail("limit_exceeded", f"work-plan scope paths must be strings of at most {MAX_SCOPE_PATH_CHARACTERS} characters")
    normalized = {"paths": sorted(paths, key=str.casefold), "exclusions": sorted(exclusions, key=str.casefold)}
    try:
        validate_project_scope(normalized, label=f"units[{index}].scope")
    except AuthorityError as error:
        _fail("invalid_manifest", str(error))
    if len(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_SCOPE_BYTES:
        _fail("limit_exceeded", f"work-plan scope exceeds {MAX_SCOPE_BYTES} bytes")
    return normalized


def _normalize_manifest(value: Any) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail("invalid_manifest", "work-plan manifest must be a JSON object")
    value = dict(value)
    if set(value) != {"kind", "version", "goal_id", "units"}:
        _fail("invalid_manifest", "work-plan manifest must contain exactly kind, version, goal_id, and units")
    if value["kind"] != WORK_PLAN_KIND:
        _fail("invalid_manifest", f"unsupported work-plan kind: {value['kind']!r}")
    if isinstance(value["version"], bool) or not isinstance(value["version"], int) or value["version"] != WORK_PLAN_VERSION:
        _fail("unsupported_version", f"unsupported work-plan version: {value['version']!r}")
    goal_id = _identifier(value["goal_id"], label="goal_id")
    units = value["units"]
    if not isinstance(units, list) or not 1 <= len(units) <= MAX_WORK_PLAN_UNITS:
        _fail("limit_exceeded", f"work-plan units must contain 1 through {MAX_WORK_PLAN_UNITS} entries")
    normalized_units: list[dict[str, object]] = []
    identifiers: set[str] = set()
    edges = 0
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            _fail("invalid_manifest", f"units[{index}] must contain id, title, scope, checkpoint_id, and prerequisite_ids only")
        unit = dict(unit)
        if set(unit) - {"id", "title", "scope", "checkpoint_id", "prerequisite_ids"} or not {"id", "title", "scope"} <= set(unit):
            _fail("invalid_manifest", f"units[{index}] must contain id, title, scope, checkpoint_id, and prerequisite_ids only")
        identifier = _identifier(unit["id"], label=f"units[{index}].id")
        if identifier in identifiers:
            _fail("duplicate_unit", f"duplicate work-plan unit id: {identifier}", unit_id=identifier)
        identifiers.add(identifier)
        title = unit["title"]
        if not isinstance(title, str) or not title or len(title) > MAX_TITLE_CHARACTERS or title != title.strip():
            _fail("invalid_manifest", f"units[{index}].title must be a trimmed nonempty string of at most {MAX_TITLE_CHARACTERS} characters")
        checkpoint_id = _optional_identifier(unit.get("checkpoint_id"), label=f"units[{index}].checkpoint_id")
        prerequisite_ids = unit.get("prerequisite_ids", [])
        if not isinstance(prerequisite_ids, list) or len(prerequisite_ids) > MAX_PREREQUISITES:
            _fail("limit_exceeded", f"units[{index}].prerequisite_ids must contain at most {MAX_PREREQUISITES} entries")
        prerequisites = [_identifier(item, label=f"units[{index}].prerequisite_ids") for item in prerequisite_ids]
        if len(set(prerequisites)) != len(prerequisites):
            _fail("duplicate_prerequisite", f"units[{index}] contains duplicate prerequisite ids")
        if identifier in prerequisites:
            _fail("cycle", f"work-plan unit {identifier} cannot depend on itself", unit_id=identifier)
        edges += len(prerequisites)
        normalized_units.append({
            "id": identifier, "title": title, "scope": _normalize_scope(unit["scope"], index=index),
            "checkpoint_id": checkpoint_id, "prerequisite_ids": sorted(prerequisites),
        })
    if edges > MAX_EDGES:
        _fail("limit_exceeded", f"work-plan has more than {MAX_EDGES} dependency edges", maximum=MAX_EDGES)
    normalized = {"kind": WORK_PLAN_KIND, "version": WORK_PLAN_VERSION, "goal_id": goal_id, "units": sorted(normalized_units, key=lambda unit: str(unit["id"]))}
    if len(canonical_work_plan_json(normalized).encode("utf-8")) > MAX_WORK_PLAN_BYTES:
        _fail("input_too_large", f"work-plan payload exceeds {MAX_WORK_PLAN_BYTES} bytes")
    return normalized


def load_work_plan(payload: bytes | str) -> dict[str, object]:
    """Parse, validate, and canonicalize one closed v1 work-plan manifest."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_WORK_PLAN_BYTES:
            _fail("input_too_large", f"work-plan payload exceeds {MAX_WORK_PLAN_BYTES} bytes")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            _fail("invalid_manifest", "work-plan bytes must be valid UTF-8")
    if not isinstance(payload, str):
        _fail("invalid_manifest", "work-plan payload must be text or UTF-8 bytes")
    try:
        if len(payload.encode("utf-8")) > MAX_WORK_PLAN_BYTES:
            _fail("input_too_large", f"work-plan payload exceeds {MAX_WORK_PLAN_BYTES} bytes")
    except UnicodeEncodeError:
        _fail("invalid_manifest", "work-plan text must be valid UTF-8")
    try:
        value = json.loads(payload, object_pairs_hook=_duplicate_keys, parse_constant=_nonfinite, parse_float=_finite_float)
    except WorkPlanError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
        _fail("invalid_manifest", f"invalid work-plan JSON: {error}")
    return _normalize_manifest(value)


def normalize_work_plan(manifest: Mapping[str, object]) -> dict[str, object]:
    """Defensively normalize mapping input accepted by the StateStore API."""
    return _normalize_manifest(manifest)
