"""Bounded, deterministic repository evidence and separate worker prompt views.

Evidence describes bytes; it is never authorization or a substitute for review.
The cache is coordinator-owned memory, not a model-authored persistent memory.
"""
from __future__ import annotations

import ast
import copy
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping, Sequence

from .config import _is_linklike

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_PACKET_BYTES = 64 * 1024


class ContextError(ValueError):
    """Context cannot be collected safely or without dropping required facts."""


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > 240
            or "\\" in value or ":" in value or any(ord(c) < 32 for c in value)):
        raise ContextError("evidence path must be a bounded project-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ContextError("evidence path must stay inside the project")
    return path


def _safe_path(root: Path, value: str) -> Path:
    relative = _relative(value)
    current = root
    for part in relative.parts:
        current = current / part
        if _is_linklike(current):
            raise ContextError("evidence path crosses a link or reparse point")
    if not current.resolve(strict=False).is_relative_to(root):
        raise ContextError("evidence path escapes the project")
    return current


class ContextCache:
    """Reuse parsed facts only after hashing the current source bytes again."""

    def __init__(self) -> None:
        self._facts: dict[tuple[str, str], dict[str, Any]] = {}

    def inspect(self, root: Path | str, paths: Sequence[str]) -> dict[str, Any]:
        project = Path(root).resolve(strict=True)
        if (not isinstance(paths, (tuple, list)) or len(paths) > MAX_FILES
                or not all(isinstance(p, str) for p in paths) or len(set(paths)) != len(paths)):
            raise ContextError("evidence paths must be a distinct bounded list")
        files, used, reused = [], 0, 0
        for name in sorted(paths):
            target = _safe_path(project, name)
            try:
                before = target.stat(follow_symlinks=False)
            except FileNotFoundError:
                files.append({"path": name, "state": "missing"})
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ContextError("evidence source must be a regular file")
            if before.st_size > MAX_FILE_BYTES or used + before.st_size > MAX_READ_BYTES:
                files.append({"path": name, "state": "omitted", "reason": "byte-limit"})
                continue
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as source:
                opened = os.fstat(source.fileno())
                if not os.path.samestat(before, opened) or not stat.S_ISREG(opened.st_mode):
                    raise ContextError("evidence source changed while opening")
                data = source.read(MAX_FILE_BYTES + 1)
                after_read = os.fstat(source.fileno())
            after_path = _safe_path(project, name).stat(follow_symlinks=False)
            if (len(data) > MAX_FILE_BYTES or not os.path.samestat(opened, after_path)
                    or opened.st_size != after_read.st_size or opened.st_mtime_ns != after_read.st_mtime_ns
                    or after_path.st_size != len(data) or after_path.st_mtime_ns != after_read.st_mtime_ns):
                raise ContextError("evidence source changed while reading")
            used += len(data)
            digest = sha256(data).hexdigest()
            key = (name, digest)
            if key in self._facts:
                fact = copy.deepcopy(self._facts[key]); reused += 1
            else:
                fact = {"path": name, "state": "observed", "sha256": digest, "bytes": len(data)}
                if name.endswith(".py"):
                    try:
                        tree = ast.parse(data)
                        symbols = [{"name": node.name, "line": node.lineno}
                                   for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                        fact["symbols"] = symbols[:20]
                        fact["symbols_omitted"] = max(0, len(symbols) - 20)
                    except (SyntaxError, UnicodeError, ValueError):
                        fact["parse_status"] = "unavailable"
                # Keep memory bounded even for callers reusing the cache across runs.
                if len(self._facts) >= MAX_FILES * 2:
                    self._facts.clear()
                self._facts[key] = copy.deepcopy(fact)
            files.append(fact)
        body = {"kind": "tasktra.repository-evidence", "version": 1, "files": files}
        return {**body, "sha256": sha256(_encoded(body)).hexdigest(), "read_bytes": used,
                "reused_files": reused, "authority": "none"}


def build_stage_packet(*, goal: Mapping[str, Any], unit: Mapping[str, Any],
                       contract: Mapping[str, Any], stage: str, patch: Mapping[str, Any],
                       evidence: Mapping[str, Any], validations: Sequence[Mapping[str, Any]],
                       prior_reports: Sequence[Mapping[str, Any]], max_bytes: int = MAX_PACKET_BYTES) -> dict[str, Any]:
    """Preserve all acceptance/constraints; bound only optional source navigation."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1024 <= max_bytes <= MAX_PACKET_BYTES:
        raise ContextError("packet byte limit must be between 1024 and 65536")
    required = {
        "stage": stage,
        "goal": {"title": goal["title"], "description": goal["description"], "acceptance": goal.get("acceptance", [])},
        "work": {"title": unit["title"], "scope": unit["scope"], "checkpoint_id": unit.get("checkpoint_id")},
        "authority_boundaries": {key: copy.deepcopy(contract[key]) for key in
            ("scope", "resource_scopes", "allowed_actions", "allowed_effects", "budgets", "dependencies", "checkpoints")
            if key in contract},
        "acceptance_criteria": contract.get("acceptance_criteria", []),
        "quality_requirements": contract.get("quality_requirements", []),
        "prohibited_actions": contract.get("prohibited_actions", []),
        "stop_conditions": contract.get("stop_conditions", []),
        "escalation_conditions": contract.get("escalation_conditions", []),
        "patch": {"sha256": patch["patch_sha256"], "changed_paths": patch["changed_paths"]},
        "validation": list(validations),
        "unresolved_reports": [{"stage": r["stage"], "findings": r.get("findings", [])}
                               for r in prior_reports if r.get("findings")],
    }
    packet = {"kind": "tasktra.stage-context", "version": 1, "authority": "none",
              "required": copy.deepcopy(required), "source_navigation": copy.deepcopy(evidence.get("files", [])),
              "source_navigation_omitted": 0,
              "guidance": "Source navigation describes observed bytes, not correctness. Inspect the actual changes and relevant dependencies independently."}
    while len(_encoded(packet)) > max_bytes and packet["source_navigation"]:
        packet["source_navigation"].pop()
        packet["source_navigation_omitted"] += 1
    if len(_encoded(packet)) > max_bytes:
        raise ContextError("required acceptance and constraints exceed the context limit; split the work unit")
    return packet


def render_stage_packet(packet: Mapping[str, Any]) -> str:
    return _encoded(packet).decode("utf-8")
