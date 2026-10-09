"""Closed, privacy-preserving contracts for recorded Codex host runs.

The host bridge owns messages and dispatch.  This module only validates the
small receipt vocabulary which the durable ledger may retain.
"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any, Mapping

from .state import StateError, _encode

OUTCOMES = frozenset({"completed", "failed", "interrupted", "needs-attention"})
RESULT_STATUSES = frozenset({"observed", "unavailable"})
USAGE_STATUSES = frozenset({"measured", "unavailable"})
ACCOUNTING_SOURCES = frozenset({"legacy-unspecified", "pending", "host-measured", "caller-declared", "unavailable"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CodexRunError(StateError):
    """A receipt cannot be represented by the closed durable contract."""


def sha256_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise CodexRunError("receipt input must be an object")
    return sha256(_encode(dict(value)).encode("utf-8")).hexdigest()


def require_digest(value: str | None, *, label: str, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CodexRunError(f"{label} must be a lowercase SHA-256")
    return value


def validate_finish(*, outcome: str, result_status: str, result_sha256: str | None,
                    usage_status: str, input_tokens: int | None, output_tokens: int | None) -> None:
    if outcome not in OUTCOMES:
        raise CodexRunError("invalid Codex run outcome")
    if result_status not in RESULT_STATUSES:
        raise CodexRunError("invalid Codex result status")
    require_digest(result_sha256, label="result_sha256", required=result_status == "observed")
    if result_status == "unavailable" and result_sha256 is not None:
        raise CodexRunError("unavailable result must not have a digest")
    if outcome == "completed" and result_status != "observed":
        raise CodexRunError("completed Codex run requires an observed result")
    if usage_status not in USAGE_STATUSES:
        raise CodexRunError("invalid Codex usage status")
    measured = usage_status == "measured"
    if measured and (input_tokens is None or output_tokens is None):
        raise CodexRunError("measured usage requires both token counts; unavailable usage requires neither")
    if not measured and (input_tokens is not None or output_tokens is not None):
        raise CodexRunError("measured usage requires both token counts; unavailable usage requires neither")
    for label, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000_000_000):
            raise CodexRunError(f"{label} must be a non-negative integer")


def task_name(*, attempt_id: str, run_no: int, requested_role: str) -> str:
    """A deterministic, collaboration-compatible request name (not host identity)."""
    digest = sha256(attempt_id.encode("utf-8")).hexdigest()[:16]
    stem = f"codex_{digest}_{run_no}_{requested_role}".lower()
    value = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    if not value or len(value) > 120:
        raise CodexRunError("requested task name is invalid")
    return value


def contains_secret(value: Any, secret: str) -> bool:
    """Reject only an exact supplied lease token, never echo it into errors."""
    if not secret:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(contains_secret(key, secret) or contains_secret(item, secret) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(contains_secret(item, secret) for item in value)
    return False
