"""Minimal local service contract."""


def health() -> dict[str, str]:
    return {"status": "ok"}
