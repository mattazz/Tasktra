"""One portable identifier contract for durable Tasktra references."""

from __future__ import annotations

import re
from typing import Any


MAX_IDENTIFIER_CHARS = 64
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)


class IdentifierError(ValueError):
    """Raised when a value cannot safely identify a durable Tasktra record."""


def is_identifier(value: object) -> bool:
    """Return whether *value* is a bounded lowercase slug identifier."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_IDENTIFIER_CHARS
        and _IDENTIFIER.fullmatch(value) is not None
    )


def require_identifier(value: Any, *, label: str = "identifier") -> str:
    """Return a valid identifier without normalizing or rewriting input."""
    if not is_identifier(value):
        raise IdentifierError(
            f"{label} must be a lowercase slug of at most {MAX_IDENTIFIER_CHARS} characters"
        )
    return value


def require_optional_identifier(value: Any, *, label: str = "identifier") -> str | None:
    if value is None:
        return None
    return require_identifier(value, label=label)
