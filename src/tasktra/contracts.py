"""Portable JSON contract validation without a runtime dependency."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


def _schema_root() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    filename = name if name.endswith(".json") else f"{name}.json"
    if Path(filename).name != filename:
        raise ValueError("schema name must not contain a path")
    try:
        value = json.loads((_schema_root() / filename).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(f"Unknown bundled schema: {name}") from None
    if not isinstance(value, dict):
        raise ContractError(f"Schema {name} is not an object")
    return value


def _resolve(reference: str, root: dict[str, Any]) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ContractError(f"Only local schema references are supported: {reference}")
    value: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(value, dict) or part not in value:
            raise ContractError(f"Unresolvable schema reference: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise ContractError(f"Schema reference does not resolve to an object: {reference}")
    return value


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool), "null": value is None,
    }.get(expected, False)


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> list[ValidationIssue]:
    if "$ref" in schema:
        return _validate(value, _resolve(schema["$ref"], root), root, path)
    if "oneOf" in schema:
        matches = sum(not _validate(value, branch, root, path) for branch in schema["oneOf"])
        return [] if matches == 1 else [ValidationIssue(path, "must match exactly one oneOf branch")]
    issues: list[ValidationIssue] = []
    if "const" in schema and value != schema["const"]:
        issues.append(ValidationIssue(path, f"must equal {schema['const']!r}"))
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, item) for item in options):
            return [ValidationIssue(path, f"must be of type {', '.join(options)}")]
    if "enum" in schema and value not in schema["enum"]:
        issues.append(ValidationIssue(path, f"must be one of {schema['enum']!r}"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            issues.append(ValidationIssue(path, "is shorter than minLength"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            issues.append(ValidationIssue(path, "is longer than maxLength"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            issues.append(ValidationIssue(path, "does not match pattern"))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema and value < schema["minimum"]:
        issues.append(ValidationIssue(path, "is below minimum"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            issues.append(ValidationIssue(path, "has fewer than minItems"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            issues.append(ValidationIssue(path, "has more than maxItems"))
        if "items" in schema:
            for index, item in enumerate(value):
                issues.extend(_validate(item, schema["items"], root, f"{path}[{index}]"))
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                issues.append(ValidationIssue(path, "must contain unique items"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                issues.append(ValidationIssue(path, f"missing required property {key!r}"))
        additional = schema.get("additionalProperties")
        if additional is False:
            for key in value:
                if key not in properties:
                    issues.append(ValidationIssue(f"{path}.{key}", "is not an allowed property"))
        elif isinstance(additional, dict):
            for key in value:
                if key not in properties:
                    issues.extend(_validate(value[key], additional, root, f"{path}.{key}"))
        for key, child in properties.items():
            if key in value:
                issues.extend(_validate(value[key], child, root, f"{path}.{key}"))
    return issues


def validate(value: Any, schema: dict[str, Any]) -> list[ValidationIssue]:
    return _validate(value, schema, schema, "$")


def require_valid(value: Any, schema: dict[str, Any], *, label: str = "contract") -> None:
    issues = validate(value, schema)
    if issues:
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise ContractError(f"Invalid {label}: {detail}")


def validate_named(value: Any, name: str) -> None:
    require_valid(value, load_schema(name), label=name)
