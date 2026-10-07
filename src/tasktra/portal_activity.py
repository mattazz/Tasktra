"""Read-only, privacy-safe activity for one registered Codex execution receipt."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from collections import OrderedDict
from threading import RLock
from typing import Any
from urllib.parse import quote

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_FILES = 512
_MAX_DIRECTORIES = 1024
_MAX_ENTRIES = 4096
_MAX_BYTES_PER_READ = 512 * 1024
_MAX_INITIAL_BYTES = 8 * 1024 * 1024
_MAX_LINE_BYTES = 256 * 1024
_MAX_EVENTS = 100
_MAX_TEXT = 2_000
_MAX_USAGE_BYTES = 2 * 1024 * 1024
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

# Cache positions are a performance optimization only. They are never emitted
# and timestamps remain evidence from JSONL events, never filesystem metadata.
_CACHE: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
_CACHE_LOCK = RLock()
_MAX_CACHE = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty(work_id: str, state: str = "unknown", reason: str | None = "unavailable") -> dict[str, Any]:
    return {"schema_version": 1, "work_id": work_id, "generated_at": _now(), "available": False,
            "source": "none", "state": state, "phase": "finished" if state in _TERMINAL else "unknown",
            "last_activity_at": None, "events": [], "usage": None,
            "coverage": {"partial": reason is not None, "reason": reason}}


def _safe_time(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        item = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if item.tzinfo is None:
        return None
    return item.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or any(ord(char) < 32 for char in value):
        return None
    return value[:_MAX_TEXT - 1] + " [truncated]" if len(value) > _MAX_TEXT else value


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return None
    return value


def _one_of(value: Any, allowed: tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in allowed


def _is_link(path: Path) -> bool:
    try:
        item = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(item.st_mode) or bool(getattr(item, "st_file_attributes", 0) & 0x400)


def _receipt(root: Path, work_id: str) -> dict[str, Any] | None:
    ledger = root / ".tasktra" / "runtime" / "agent-execution.sqlite"
    try:
        if _is_link(ledger) or not ledger.is_file():
            return None
        uri = "file:" + quote(ledger.as_posix(), safe="/:") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(execution)")}
            required = {"work_id", "state", "provider", "thread_id", "turn_id", "source_path_sha256"}
            if not required.issubset(columns):
                return None
            row = connection.execute("SELECT work_id,state,provider,thread_id,turn_id,source_path_sha256,usage_provenance FROM execution WHERE work_id=?", (work_id,)).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None


def _default_sessions_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home).expanduser() / "sessions" if home else Path.home() / ".codex" / "sessions"


def _metadata_digest(path: Path) -> str | None:
    """Digest the first bounded session metadata record for cache revalidation."""
    total = 0
    try:
        with path.open("rb") as handle:
            for _ in range(64):
                raw = handle.readline(_MAX_LINE_BYTES + 1)
                if not raw: return None
                total += len(raw)
                if len(raw) > _MAX_LINE_BYTES or total > _MAX_BYTES_PER_READ: return None
                try: event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError): continue
                if isinstance(event, dict) and event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                    return sha256(raw).hexdigest()
    except OSError:
        return None
    return None


def _header_digest(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            return sha256(handle.read(64 * 1024)).hexdigest()
    except OSError:
        return None


def _safe_child(base: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return False
    current = base
    if _is_link(current):
        return False
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            return False
    return True


def _find_source(root: Path, expected_hash: str, sessions_root: Path) -> Path | None:
    try:
        if _is_link(sessions_root):
            return None
        base = sessions_root.resolve(strict=True)
        if _is_link(base) or not base.is_dir():
            return None
    except OSError:
        return None
    directories = [base]
    files = entries = 0
    while directories:
        directory = directories.pop()
        if len(directories) > _MAX_DIRECTORIES:
            return None
        try:
            with os.scandir(directory) as items:
                for item in items:
                    entries += 1
                    if entries > _MAX_ENTRIES:
                        return None
                    path = Path(item.path)
                    if _is_link(path):
                        continue
                    if item.is_dir(follow_symlinks=False):
                        directories.append(path)
                    elif item.is_file(follow_symlinks=False) and path.name.startswith("rollout-") and path.suffix == ".jsonl":
                        files += 1
                        if files > _MAX_FILES:
                            return None
                        try:
                            resolved = path.resolve(strict=True)
                        except OSError:
                            continue
                        if _safe_child(base, path) and sha256(str(resolved).encode("utf-8")).hexdigest() == expected_hash:
                            return resolved
        except OSError:
            return None
    return None


def _assistant_text(item: dict[str, Any]) -> str | None:
    channel = item.get("channel", item.get("phase"))
    if item.get("type") != "message" or item.get("role") != "assistant" or not _one_of(channel, ("commentary", "final", "final_answer")):
        return None
    content = item.get("content")
    if isinstance(content, str):
        return _safe_text(content)
    if not isinstance(content, list):
        return None
    parts = []
    for part in content:
        if not isinstance(part, dict) or not _one_of(part.get("type"), ("output_text", "text")):
            continue
        text = _safe_text(part.get("text"))
        if text is not None:
            parts.append(text)
    return _safe_text(" ".join(parts)) if parts else None


def _allowed_event(event: dict[str, Any], sequence: int) -> dict[str, Any] | None:
    timestamp = _safe_time(event.get("timestamp"))
    event_type, payload = event.get("type"), event.get("payload")
    if event_type == "response_item" and isinstance(payload, dict):
        item = payload.get("item", payload)
        if isinstance(item, dict):
            text = _assistant_text(item)
            if text is not None:
                return {"id": f"event-{sequence}", "timestamp": timestamp, "kind": "final" if _one_of(item.get("channel", item.get("phase")), ("final", "final_answer")) else "progress", "text": text}
            if _one_of(item.get("type"), ("function_call", "custom_tool_call")):
                name, state = _safe_name(item.get("name")), item.get("status", "in_progress")
                if name is not None and isinstance(state, str) and state in {"in_progress", "completed", "failed"}:
                    result = {"id": f"event-{sequence}", "timestamp": timestamp, "kind": "tool", "text": name, "tool_name": name, "state": state}
                    call_id = _safe_name(item.get("call_id"))
                    if call_id is not None: result["_call_id"] = call_id
                    return result
    if event_type == "event_msg" and isinstance(payload, dict):
        native_item = payload.get("item")
        if isinstance(native_item, dict) and _one_of(native_item.get("type"), ("McpToolCall", "CommandExecution")):
            name = _safe_name(native_item.get("tool")) or ("command_execution" if native_item.get("type") == "CommandExecution" else None)
            status = native_item.get("status")
            if name is not None and isinstance(status, str) and status in {"in_progress", "completed", "failed"}:
                result = {"id": f"event-{sequence}", "timestamp": timestamp, "kind": "tool", "text": name, "tool_name": name, "state": status}
                call_id = _safe_name(native_item.get("id"))
                if call_id is not None: result["_call_id"] = call_id
                return result
        label = payload.get("type")
        if _one_of(label, ("tool_started", "tool_completed", "tool_failed")):
            name = _safe_name(payload.get("tool_name"))
            if name is not None:
                state = {"tool_started": "in_progress", "tool_completed": "completed", "tool_failed": "failed"}[label]
                result = {"id": f"event-{sequence}", "timestamp": timestamp, "kind": "tool", "text": name, "tool_name": name, "state": state}
                call_id = _safe_name(payload.get("call_id"))
                if call_id is not None: result["_call_id"] = call_id
                return result
        if _one_of(label, ("task_started", "task_completed", "task_failed")):
            return {"id": f"event-{sequence}", "timestamp": timestamp, "kind": "status", "text": label, "state": label.removeprefix("task_")}
    return None


def agent_activity(root: Path | str, work_id: str, *, sessions_root: Path | str | None = None) -> dict[str, Any]:
    """Return bounded public activity for exactly one registered receipt.

    This never writes a database or exposes an unregistered rollout path.
    """
    if not isinstance(work_id, str) or not _IDENTIFIER.fullmatch(work_id):
        return _empty(str(work_id), reason="unavailable")
    try:
        project = Path(root).resolve(strict=True)
    except OSError:
        return _empty(work_id)
    receipt = _receipt(project, work_id)
    state = str(receipt.get("state")) if receipt and receipt.get("state") in _TERMINAL | {"started"} else "unknown"
    if receipt is None or receipt.get("provider") != "codex" or not isinstance(receipt.get("thread_id"), str) or not isinstance(receipt.get("turn_id"), str) or not receipt.get("source_path_sha256"):
        return _empty(work_id, state, "unverified-rollout")
    source_root = Path(sessions_root) if sessions_root is not None else _default_sessions_root()
    if _is_link(source_root):
        return _empty(work_id, state, "unverified-rollout")
    key = (str(project), work_id, str(source_root))
    receipt_key = (str(receipt["source_path_sha256"]), str(receipt["thread_id"]), str(receipt.get("turn_id")))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.get("receipt_key") != receipt_key:
            cached = None
        if cached is not None:
            _CACHE.move_to_end(key)
            cached = {**cached, "events": list(cached.get("events", [])), "tool_calls": set(cached.get("tool_calls", set()))}
    source = Path(cached["source"]) if cached and Path(cached["source"]).exists() else _find_source(project, str(receipt["source_path_sha256"]), source_root)
    if source is None or _is_link(source):
        return _empty(work_id, state, "unverified-rollout")
    try:
        details = source.stat()
        identity = (getattr(details, "st_dev", 0), getattr(details, "st_ino", 0), details.st_size, details.st_mtime_ns)
    except OSError:
        return _empty(work_id, state, "unavailable")
    try: safe_root = source_root.resolve(strict=True)
    except OSError: return _empty(work_id, state, "unverified-rollout")
    header = _header_digest(source)
    metadata = _metadata_digest(source)
    if header is None or metadata is None or sha256(str(source.resolve()).encode("utf-8")).hexdigest() != receipt["source_path_sha256"] or not _safe_child(safe_root, source):
        return _empty(work_id, state, "unverified-rollout")
    prior = cached.get("identity") if cached else None
    stable_file_id = prior is not None and prior[:2] == identity[:2] and prior[:2] != (0, 0)
    same_file = (prior == identity and (cached is None or cached.get("metadata") == metadata)) or (stable_file_id and prior[2] < identity[2] and cached.get("header") == header and cached.get("metadata") == metadata)
    cache = cached if cached and cached.get("source") == str(source) and same_file else {"source": str(source), "receipt_key": receipt_key, "identity": identity, "header": header, "metadata": metadata, "offset": 0, "events": [], "verified": False, "sequence": 0, "active_turn": None, "skip_line": False, "partial": False, "reason": None, "dropped": False, "tool_calls": set(), "invalid_session": False}
    partial = bool(cache.get("partial")); reason: str | None = cache.get("reason"); read = 0
    read_limit = _MAX_INITIAL_BYTES if int(cache["offset"]) == 0 else _MAX_BYTES_PER_READ
    at_eof = False
    try:
        with source.open("rb") as handle:
            handle.seek(int(cache["offset"]))
            while read < read_limit:
                raw = handle.readline(_MAX_LINE_BYTES + 1)
                if not raw:
                    at_eof = True
                    break
                read += len(raw)
                if cache.get("skip_line"):
                    cache["offset"] = handle.tell()
                    if raw.endswith(b"\n"): cache["skip_line"] = False
                    partial = True; reason = "oversized-rollout-event"; cache["active_turn"] = None
                    continue
                if len(raw) > _MAX_LINE_BYTES:
                    cache["skip_line"] = True; cache["offset"] = handle.tell()
                    partial = True; reason = "oversized-rollout-event"; cache["active_turn"] = None; continue
                if read > read_limit:
                    partial = True; reason = "catching-up"; break
                if not raw.endswith(b"\n"):
                    partial = True; reason = "growing-rollout"; break
                cache["offset"] = handle.tell(); cache["sequence"] += 1
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    partial = True; reason = "unavailable"; cache["active_turn"] = None; continue
                if not isinstance(event, dict):
                    cache["active_turn"] = None
                    continue
                if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                    payload = event["payload"]
                    try: cwd_matches = Path(str(payload.get("cwd"))).resolve(strict=True) == project
                    except OSError: cwd_matches = False
                    matches = payload.get("id") == receipt["thread_id"] and cwd_matches
                    if not matches: cache["invalid_session"] = True
                    cache["verified"] = matches and not cache.get("invalid_session", False)
                payload = event.get("payload")
                if event.get("type") == "turn_context":
                    cache["active_turn"] = None
                if event.get("type") == "turn_context" and isinstance(payload, dict) and isinstance(payload.get("turn_id"), str):
                    cache["active_turn"] = payload["turn_id"]
                    if payload["turn_id"] == receipt.get("turn_id") and "cwd" in payload:
                        try: turn_cwd_matches = Path(str(payload["cwd"])).resolve(strict=True) == project
                        except OSError: turn_cwd_matches = False
                        if not turn_cwd_matches: cache["invalid_session"] = True; cache["verified"] = False
                if event.get("type") == "event_msg" and isinstance(payload, dict) and _one_of(payload.get("type"), ("task_started", "task_complete", "task_failed")):
                    cache["active_turn"] = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
                explicit_turn = payload.get("turn_id") if isinstance(payload, dict) else None
                turn_matches = receipt.get("turn_id") is not None and (explicit_turn == receipt.get("turn_id") or (explicit_turn is None and cache.get("active_turn") == receipt.get("turn_id")))
                if cache["verified"] and event.get("type") != "session_meta" and turn_matches:
                    item = _allowed_event(event, int(cache["sequence"]))
                    direct_item = payload.get("item", payload) if isinstance(payload, dict) else None
                    if item is None and isinstance(direct_item, dict) and _one_of(direct_item.get("type"), ("function_call_output", "custom_tool_call_output")):
                        call_id = _safe_name(direct_item.get("call_id"))
                        if call_id is not None: cache["tool_calls"].discard(call_id)
                    if item is not None:
                        call_id = item.pop("_call_id", None)
                        if item["kind"] == "tool" and call_id is not None:
                            if item.get("state") == "in_progress":
                                if len(cache["tool_calls"]) < _MAX_EVENTS: cache["tool_calls"].add(call_id)
                                else: partial = True; reason = reason or "limit-reached"
                            elif call_id in cache["tool_calls"]: cache["tool_calls"].discard(call_id)
                        if item.get("text", "").endswith(" [truncated]"): partial = True; reason = reason or "limit-reached"
                        if len(cache["events"]) >= _MAX_EVENTS: cache["dropped"] = True
                        cache["events"] = (cache["events"] + [item])[-_MAX_EVENTS:]
    except OSError:
        return _empty(work_id, state, "unavailable")
    cache["identity"] = identity; cache["header"] = header; cache["metadata"] = metadata; cache["at_eof"] = at_eof; cache["partial"] = partial; cache["reason"] = reason
    with _CACHE_LOCK:
        _CACHE[key] = cache; _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_CACHE: _CACHE.popitem(last=False)
    if not cache["verified"]:
        return _empty(work_id, state, "unverified-rollout")
    events = list(cache["events"])
    latest = next((item["timestamp"] for item in reversed(events) if item["timestamp"] is not None), None)
    phase = "finished" if state in _TERMINAL else "unknown"
    if phase != "finished" and not cache.get("at_eof", False):
        phase = "unknown"
    elif phase != "finished" and cache.get("tool_calls"):
        phase = "tool_running"
    elif phase != "finished" and events and events[-1]["kind"] == "progress":
        phase = "working"
    usage = None
    if identity[2] <= _MAX_USAGE_BYTES:
        try:
            from .codex_usage import scan_rollout
            observation = scan_rollout(source, receipt["thread_id"], receipt.get("turn_id"))
            if observation.usage is not None:
                observed_at = observation.last_observed_at
                usage = {"observed_at": observed_at, "input_tokens": observation.usage["input_tokens"], "cached_input_tokens": observation.usage["cached_input_tokens"], "output_tokens": observation.usage["output_tokens"], "total_tokens": observation.usage["total_tokens"]}
        except Exception:
            partial = True; reason = reason or "unavailable"
    elif reason is None:
        partial = True; reason = "limit-reached"
    return {"schema_version": 1, "work_id": work_id, "generated_at": _now(), "available": True, "source": "verified-rollout", "state": state, "phase": phase, "last_activity_at": latest, "events": events, "usage": usage, "coverage": {"partial": partial or bool(cache.get("dropped")), "reason": reason or ("limit-reached" if cache.get("dropped") else None)}}
