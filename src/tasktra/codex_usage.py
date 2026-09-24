"""Read bounded, provenance-safe usage observations from Codex rollout JSONL.

The parser deliberately retains only accounting and attribution fields.  It has
no database or command-line integration so callers decide how observations are
stored or displayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

# These limits bound work and memory when a caller supplies an arbitrary
# rollout path.  A normal rollout is far below both limits.
MAX_ROLLOUT_BYTES = 64 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_JSONL_EVENTS = 100_000
MAX_SAFE_AGENT_PATH_CHARS = 512


class RolloutUsageError(ValueError):
    """A rollout cannot produce a trustworthy usage observation."""


@dataclass(frozen=True)
class RolloutObservation:
    """Safe accounting data observed for one Codex rollout thread or turn."""

    thread_id: str
    turn_id: str | None
    agent_id: str | None
    model: str | None
    effort: str | None
    usage: dict[str, int] | None
    schema: str
    response_count: int
    source_sha256: str
    response_fingerprints: tuple[str, ...] = ()
    source_bytes: int = 0


def _response_fingerprint(turn_id: str | None, response_id: str, usage: dict[str, int]) -> str:
    """Return a non-reversible receipt identity without retaining response IDs."""
    payload = json.dumps(
        {"turn_id": turn_id, "response_id": response_id, "usage": usage},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _required_identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RolloutUsageError(f"{name} is required")
    return value


def _optional_turn_identifier(value: str | None) -> str | None:
    if value is not None:
        _required_identifier(value, "turn_id")
    return value


def _usage_from_object(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RolloutUsageError("usage record is malformed")
    usage: dict[str, int] = {}
    for field in USAGE_FIELDS:
        counter = value.get(field)
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            raise RolloutUsageError(f"usage record has invalid {field}")
        usage[field] = counter
    _validate_usage(usage)
    return usage


def _validate_usage(usage: dict[str, int]) -> None:
    """Validate aggregate and per-response counter relationships."""
    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]
    total_tokens = usage["total_tokens"]
    if total_tokens != input_tokens + output_tokens:
        raise RolloutUsageError("total_tokens must equal input_tokens + output_tokens")
    if usage["cached_input_tokens"] > input_tokens:
        raise RolloutUsageError("cached_input_tokens cannot exceed input_tokens")
    if usage["cache_write_input_tokens"] > input_tokens:
        raise RolloutUsageError("cache_write_input_tokens cannot exceed input_tokens")
    if usage["reasoning_output_tokens"] > output_tokens:
        raise RolloutUsageError("reasoning_output_tokens cannot exceed output_tokens")


def _safe_agent_path(payload: dict[str, Any]) -> str | None:
    """Return only the documented nested agent path, when it is safe to expose."""
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return None
    candidate = thread_spawn.get("agent_path")
    if not isinstance(candidate, str):
        return None
    if not candidate.strip() or len(candidate) > MAX_SAFE_AGENT_PATH_CHARS:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None
    return candidate


def _context_from_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
    model, effort = payload.get("model"), payload.get("effort")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(effort, str) or not effort.strip():
        return None
    return model, effort


def _read_rollout(
    source: Path,
    thread_id: str,
    requested_turn: str | None,
) -> tuple[
    set[str],
    str | None,
    dict[str, tuple[str, str]],
    dict[tuple[str | None, str], dict[str, int]],
    list[dict[str, int]],
    bool,
    str,
    int,
]:
    """Scan a bounded file without retaining event, prompt, or message data."""
    try:
        if not source.is_file():
            raise RolloutUsageError(f"rollout source is not a file: {source}")
        if source.stat().st_size > MAX_ROLLOUT_BYTES:
            raise RolloutUsageError(f"rollout source exceeds {MAX_ROLLOUT_BYTES} bytes")
    except OSError as exc:
        raise RolloutUsageError(f"cannot read rollout source: {source}") from exc

    metadata_ids: set[str] = set()
    safe_agent_id: str | None = None
    contexts: dict[str, tuple[str, str]] = {}
    responses: dict[tuple[str | None, str], dict[str, int]] = {}
    cumulative: list[dict[str, int]] = []
    saw_response_record = False
    bytes_read = 0
    digest = sha256()

    try:
        with source.open("rb") as handle:
            for line_number in range(1, MAX_JSONL_EVENTS + 2):
                raw_line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
                if not raw_line:
                    break
                bytes_read += len(raw_line)
                if bytes_read > MAX_ROLLOUT_BYTES:
                    raise RolloutUsageError(f"rollout source exceeds {MAX_ROLLOUT_BYTES} bytes")
                if len(raw_line) > MAX_JSONL_LINE_BYTES:
                    raise RolloutUsageError(
                        f"rollout JSONL line {line_number} exceeds {MAX_JSONL_LINE_BYTES} bytes"
                    )
                if line_number > MAX_JSONL_EVENTS:
                    raise RolloutUsageError(f"rollout source exceeds {MAX_JSONL_EVENTS} events")
                digest.update(raw_line)
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RolloutUsageError(f"malformed JSONL at line {line_number}") from exc
                if not isinstance(event, dict):
                    raise RolloutUsageError(f"malformed event at line {line_number}")

                event_type, payload = event.get("type"), event.get("payload")
                if not isinstance(payload, dict):
                    if event_type in {
                        "session_meta",
                        "turn_context",
                        "token_usage_record",
                        "event_msg",
                    }:
                        raise RolloutUsageError(
                            f"malformed {event_type} event at line {line_number}"
                        )
                    continue
                if event_type == "session_meta":
                    observed_id = payload.get("id")
                    if isinstance(observed_id, str):
                        metadata_ids.add(observed_id)
                    agent_id = _safe_agent_path(payload)
                    if safe_agent_id and agent_id and safe_agent_id != agent_id:
                        raise RolloutUsageError("conflicting agent paths in rollout metadata")
                    safe_agent_id = safe_agent_id or agent_id
                elif event_type == "turn_context":
                    observed_turn = payload.get("turn_id")
                    context = _context_from_payload(payload)
                    if isinstance(observed_turn, str) and observed_turn and context:
                        existing = contexts.get(observed_turn)
                        if existing and existing != context:
                            raise RolloutUsageError(
                                f"conflicting model context for turn {observed_turn!r}"
                            )
                        contexts[observed_turn] = context
                elif event_type == "token_usage_record":
                    saw_response_record = True
                    record_thread = payload.get("thread_id")
                    if not isinstance(record_thread, str) or not record_thread:
                        raise RolloutUsageError("token_usage_record is missing thread_id")
                    record_turn = payload.get("turn_id")
                    if record_turn is not None and (not isinstance(record_turn, str) or not record_turn):
                        raise RolloutUsageError("token_usage_record has invalid turn_id")
                    if record_thread != thread_id or (
                        requested_turn is not None and record_turn != requested_turn
                    ):
                        continue
                    response_id = payload.get("response_id")
                    if not isinstance(response_id, str) or not response_id:
                        raise RolloutUsageError("token_usage_record is missing response_id")
                    usage = _usage_from_object(payload.get("usage"))
                    key = (record_turn, response_id)
                    existing = responses.get(key)
                    if existing is not None and existing != usage:
                        raise RolloutUsageError(
                            f"conflicting duplicate response {response_id!r} for turn {record_turn!r}"
                        )
                    responses[key] = usage
                elif event_type == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and "total_token_usage" in info:
                        cumulative.append(_usage_from_object(info["total_token_usage"]))
    except OSError as exc:
        raise RolloutUsageError(f"cannot read rollout source: {source}") from exc

    return (
        metadata_ids,
        safe_agent_id,
        contexts,
        responses,
        cumulative,
        saw_response_record,
        digest.hexdigest(),
        bytes_read,
    )


def scan_rollout(
    path: Path,
    thread_id: str,
    turn_id: str | None = None,
) -> RolloutObservation:
    """Return a safe usage observation for exactly one Codex rollout thread.

    ``session_meta.payload.id`` establishes the thread identity.  Per-response
    usage records use ``payload.thread_id``; their ``session_id`` is never used
    to select child rollout usage.
    """
    thread_id = _required_identifier(thread_id, "thread_id")
    turn_id = _optional_turn_identifier(turn_id)
    source = Path(path)
    (
        metadata_ids,
        agent_id,
        contexts,
        responses,
        cumulative,
        saw_response_record,
        source_sha256,
        source_bytes,
    ) = _read_rollout(source, thread_id, turn_id)

    if metadata_ids != {thread_id}:
        observed = ", ".join(sorted(metadata_ids)) or "none"
        raise RolloutUsageError(
            f"rollout thread does not exactly match {thread_id!r} (observed: {observed})"
        )

    if responses:
        selected_turns = {record_turn for record_turn, _ in responses}
        observed_contexts = {
            contexts[record_turn]
            for record_turn in selected_turns
            if record_turn is not None and record_turn in contexts
        }
        if turn_id is None and len(observed_contexts) > 1:
            raise RolloutUsageError("mixed rollout model contexts require turn_id scoping")
        model: str | None = None
        effort: str | None = None
        if (
            len(observed_contexts) == 1
            and None not in selected_turns
            and selected_turns.issubset(contexts)
        ):
            model, effort = next(iter(observed_contexts))
        usage = {
            field: sum(record[field] for record in responses.values())
            for field in USAGE_FIELDS
        }
        _validate_usage(usage)
        return RolloutObservation(
            thread_id=thread_id,
            turn_id=turn_id,
            agent_id=agent_id,
            model=model,
            effort=effort,
            usage=usage,
            schema="token_usage_record",
            response_count=len(responses),
            source_sha256=source_sha256,
            response_fingerprints=tuple(sorted(
                _response_fingerprint(record_turn, response_id, usage)
                for (record_turn, response_id), usage in responses.items()
            )),
            source_bytes=source_bytes,
        )

    if saw_response_record:
        raise RolloutUsageError("no token_usage_record events match the requested rollout thread and turn")
    if turn_id is not None:
        raise RolloutUsageError("turn_id requires token_usage_record events")
    if cumulative:
        previous: dict[str, int] | None = None
        for snapshot in cumulative:
            if previous is not None and any(
                snapshot[field] < previous[field] for field in USAGE_FIELDS
            ):
                raise RolloutUsageError("cumulative token_count counters decrease")
            previous = snapshot
        return RolloutObservation(
            thread_id=thread_id,
            turn_id=None,
            agent_id=agent_id,
            model=None,
            effort=None,
            usage=cumulative[-1],
            schema="event_msg/token_count",
            response_count=1,
            source_sha256=source_sha256,
            response_fingerprints=(),
            source_bytes=source_bytes,
        )

    return RolloutObservation(
        thread_id=thread_id,
        turn_id=turn_id,
        agent_id=agent_id,
        model=None,
        effort=None,
        usage=None,
        schema="none",
        response_count=0,
        source_sha256=source_sha256,
        response_fingerprints=(),
        source_bytes=source_bytes,
    )
