"""Bounded, review-gated lesson proposals for improving canonical Tasktra sources.

Proposal files are data, never authority.  Approval permits creation of a
deterministic promotion *plan* only; this module never edits canonical sources,
runs regression commands, compiles projections, or grants provider authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator
from uuid import uuid4

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, require_identifier


LESSON_SCHEMA_VERSION = 1
MAX_LESSON_FILE_BYTES = 256 * 1024
MAX_VERSION = 2_147_483_647
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_STATUS_TRANSITIONS = {
    "draft": frozenset({"reviewed"}),
    "reviewed": frozenset({"approved", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}


class LessonError(ValueError):
    """A lesson proposal is invalid or cannot be stored safely."""


class LessonNotFoundError(LessonError):
    pass


class LessonExistsError(LessonError):
    pass


class LessonConflictError(LessonError):
    pass


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    ref: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "sha256": self.sha256}


@dataclass(frozen=True)
class AffectedContract:
    contract_id: str
    canonical_source: str
    change: str

    def to_dict(self) -> dict[str, str]:
        return {
            "contract_id": self.contract_id,
            "canonical_source": self.canonical_source,
            "change": self.change,
        }


@dataclass(frozen=True)
class Applicability:
    contexts: tuple[str, ...]
    constraints: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str]]:
        return {"contexts": list(self.contexts), "constraints": list(self.constraints)}


@dataclass(frozen=True)
class RegressionCheck:
    id: str
    description: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "argv": list(self.argv)}


@dataclass(frozen=True)
class LessonProposal:
    id: str
    version: int
    status: str
    author: str
    reviewer: str | None
    problem: str
    general_principle: str
    evidence: tuple[EvidenceRef, ...]
    affected_contracts: tuple[AffectedContract, ...]
    applicability: Applicability
    risks: tuple[str, ...]
    regression_checks: tuple[RegressionCheck, ...]
    decision_reason: str | None
    created_at: str
    updated_at: str
    reviewed_at: str | None
    decided_at: str | None
    schema_version: int = LESSON_SCHEMA_VERSION
    kind: str = "tasktra.lesson-proposal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "status": self.status,
            "author": self.author,
            "reviewer": self.reviewer,
            "problem": self.problem,
            "general_principle": self.general_principle,
            "evidence": [item.to_dict() for item in self.evidence],
            "affected_contracts": [item.to_dict() for item in self.affected_contracts],
            "applicability": self.applicability.to_dict(),
            "risks": list(self.risks),
            "regression_checks": [item.to_dict() for item in self.regression_checks],
            "decision_reason": self.decision_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LessonProposal":
        if not isinstance(value, Mapping):
            raise LessonError("lesson proposal must be an object")
        try:
            copied = json.loads(_canonical_json(dict(value)))
            validate_named(copied, "lesson-proposal.schema")
        except (ContractError, TypeError, ValueError) as error:
            raise LessonError(str(error)) from error

        _identifier(copied["id"], "lesson id")
        _identifier(copied["author"], "lesson author")
        if copied["reviewer"] is not None:
            _identifier(copied["reviewer"], "lesson reviewer")
        _bounded_visible(copied["problem"], "problem")
        _bounded_visible(copied["general_principle"], "general principle")
        for item in copied["affected_contracts"]:
            _bounded_visible(item["change"], "affected contract change")
        for item in (*copied["applicability"]["contexts"], *copied["applicability"]["constraints"], *copied["risks"]):
            _bounded_visible(item, "applicability or risk")
        for check in copied["regression_checks"]:
            _bounded_visible(check["description"], "regression check description")
            if any("\x00" in argument for argument in check["argv"]):
                raise LessonError("regression argv must not contain NUL bytes")

        status, author, reviewer = copied["status"], copied["author"], copied["reviewer"]
        reviewed_at, decided_at, reason = copied["reviewed_at"], copied["decided_at"], copied["decision_reason"]
        if status == "draft" and any(item is not None for item in (reviewer, reviewed_at, decided_at, reason)):
            raise LessonError("draft lessons cannot contain review or decision fields")
        if status == "reviewed" and (reviewer is None or reviewed_at is None or decided_at is not None or reason is not None):
            raise LessonError("reviewed lessons require a reviewer and review time but no decision")
        if status in {"approved", "rejected"} and (
            reviewer is None or reviewed_at is None or decided_at is None or reason is None
        ):
            raise LessonError("decided lessons require reviewer, review time, decision time, and reason")
        if reviewer == author:
            raise LessonError("lesson reviewer must be independent from the author")

        _unique(((item["kind"], item["ref"]) for item in copied["evidence"]), "evidence references")
        _unique(
            ((item["contract_id"], item["canonical_source"]) for item in copied["affected_contracts"]),
            "affected contracts",
        )
        _unique((item["id"] for item in copied["regression_checks"]), "regression check ids")

        return cls(
            id=copied["id"], version=copied["version"], status=status, author=author, reviewer=reviewer,
            problem=copied["problem"], general_principle=copied["general_principle"],
            evidence=tuple(EvidenceRef(**item) for item in copied["evidence"]),
            affected_contracts=tuple(AffectedContract(**item) for item in copied["affected_contracts"]),
            applicability=Applicability(
                tuple(copied["applicability"]["contexts"]), tuple(copied["applicability"]["constraints"])
            ),
            risks=tuple(copied["risks"]),
            regression_checks=tuple(
                RegressionCheck(item["id"], item["description"], tuple(item["argv"]))
                for item in copied["regression_checks"]
            ),
            decision_reason=reason, created_at=copied["created_at"], updated_at=copied["updated_at"],
            reviewed_at=reviewed_at, decided_at=decided_at, schema_version=copied["schema_version"],
            kind=copied["kind"],
        )


def _identifier(value: Any, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise LessonError(str(error)) from error


def _bounded_visible(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\x00" in value:
        raise LessonError(f"{label} must be non-empty canonical text")
    return value


def _unique(values: Iterator[Any], label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise LessonError(f"{label} must be unique")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise LessonError(f"lesson proposal must contain JSON-compatible values: {error}") from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class LessonProposalStore:
    """Link-safe project storage for ``.tasktra/lessons/*.json`` proposals."""

    def __init__(self, project_root: Path | str, *, clock: Callable[[], str] | None = None) -> None:
        supplied = Path(project_root)
        if not supplied.is_dir():
            raise LessonError(f"project root is not a directory: {supplied}")
        self.project_root = supplied.resolve(strict=True)
        self._tasktra_dir = self.project_root / ".tasktra"
        self.directory = self._tasktra_dir / "lessons"
        self._clock = clock or _now

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )

    def _assert_safe_path(self, path: Path, *, must_exist: bool = False) -> None:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as error:
            raise LessonError("lesson path escapes the project root") from error
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if self._is_reparse_point(current):
                raise LessonError(f"lesson path contains a link or reparse point: {current}")
        try:
            if path.exists() or path.is_symlink():
                resolved = path.resolve(strict=True)
            else:
                if must_exist:
                    raise LessonNotFoundError(f"lesson proposal does not exist: {path.stem}")
                resolved = path.parent.resolve(strict=True) / path.name
            resolved.relative_to(self.project_root)
        except LessonNotFoundError:
            raise
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise LessonError(f"lesson path escapes the project root: {path}") from error
        if self._is_reparse_point(path):
            raise LessonError(f"lesson path contains a link or reparse point: {path}")

    def _ensure_directory(self) -> None:
        for directory in (self._tasktra_dir, self.directory):
            self._assert_safe_path(directory)
            if directory.exists():
                if not directory.is_dir():
                    raise LessonError(f"lesson directory is not a directory: {directory}")
                continue
            directory.mkdir()
            self._assert_safe_path(directory, must_exist=True)

    def _existing_directory(self) -> bool:
        self._assert_safe_path(self.directory)
        if not self.directory.exists():
            return False
        if not self.directory.is_dir():
            raise LessonError(f"lesson directory is not a directory: {self.directory}")
        return True

    def _path_for(self, proposal_id: str) -> Path:
        proposal_id = _identifier(proposal_id, "lesson id")
        path = self.directory / f"{proposal_id}.json"
        self._assert_safe_path(path)
        return path

    @staticmethod
    def _render(proposal: LessonProposal) -> str:
        rendered = _canonical_json(proposal.to_dict()) + "\n"
        if len(rendered.encode("utf-8")) > MAX_LESSON_FILE_BYTES:
            raise LessonError(f"lesson proposal exceeds {MAX_LESSON_FILE_BYTES} bytes")
        return rendered

    def _write_new(self, path: Path, content: str) -> None:
        self._assert_safe_path(path)
        if path.exists() or path.is_symlink():
            raise LessonExistsError(f"lesson proposal already exists: {path.stem}")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(path)
            os.link(temporary, path)
        except FileExistsError as error:
            raise LessonExistsError(f"lesson proposal already exists: {path.stem}") from error
        except OSError as error:
            raise LessonError(f"unable to create lesson proposal: {path}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _update_lock(self, path: Path) -> Iterator[None]:
        lock = path.with_name(f".{path.name}.lock")
        self._assert_safe_path(lock)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise LessonConflictError(f"lesson proposal is already being updated: {path.stem}") from error
        try:
            os.close(descriptor)
            yield
        finally:
            self._assert_safe_path(lock)
            lock.unlink(missing_ok=True)

    def _replace(self, path: Path, content: str) -> None:
        self._assert_safe_path(path, must_exist=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._assert_safe_path(temporary)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(temporary, must_exist=True)
            self._assert_safe_path(path, must_exist=True)
            os.replace(temporary, path)
        except OSError as error:
            raise LessonError(f"unable to update lesson proposal: {path}") from error
        finally:
            self._assert_safe_path(temporary)
            temporary.unlink(missing_ok=True)

    def _read_path(self, path: Path, *, expected_id: str | None = None) -> LessonProposal:
        self._assert_safe_path(path, must_exist=True)
        if not path.is_file():
            raise LessonNotFoundError(f"lesson proposal does not exist: {path.stem}")
        try:
            with path.open("rb") as handle:
                payload = handle.read(MAX_LESSON_FILE_BYTES + 1)
        except OSError as error:
            raise LessonError(f"unable to read lesson proposal: {path}") from error
        if len(payload) > MAX_LESSON_FILE_BYTES:
            raise LessonError(f"lesson proposal exceeds {MAX_LESSON_FILE_BYTES} bytes: {path}")
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, LessonError) as error:
            raise LessonError(f"invalid lesson proposal JSON: {path}") from error
        proposal = LessonProposal.from_mapping(value)
        if proposal.id != path.stem or (expected_id is not None and proposal.id != expected_id):
            raise LessonError(f"lesson proposal id does not match its filename: {path}")
        return proposal

    def create(
        self, *, proposal_id: str, author: str, problem: str, general_principle: str,
        evidence: Sequence[Mapping[str, Any]], affected_contracts: Sequence[Mapping[str, Any]],
        applicability: Mapping[str, Any], risks: Sequence[str],
        regression_checks: Sequence[Mapping[str, Any]],
    ) -> LessonProposal:
        """Create a draft. Proposal content cannot confer authority."""
        self._ensure_directory()
        proposal_id = _identifier(proposal_id, "lesson id")
        author = _identifier(author, "lesson author")
        timestamp = self._clock()
        record = {
            "kind": "tasktra.lesson-proposal", "schema_version": LESSON_SCHEMA_VERSION,
            "id": proposal_id, "version": 1, "status": "draft", "author": author, "reviewer": None,
            "problem": problem, "general_principle": general_principle,
            "evidence": list(evidence), "affected_contracts": list(affected_contracts),
            "applicability": dict(applicability), "risks": list(risks),
            "regression_checks": list(regression_checks), "decision_reason": None,
            "created_at": timestamp, "updated_at": timestamp, "reviewed_at": None, "decided_at": None,
        }
        proposal = LessonProposal.from_mapping(record)
        self._write_new(self._path_for(proposal.id), self._render(proposal))
        return proposal

    def read(self, proposal_id: str) -> LessonProposal:
        if not self._existing_directory():
            raise LessonNotFoundError(f"lesson proposal does not exist: {proposal_id}")
        proposal_id = _identifier(proposal_id, "lesson id")
        return self._read_path(self._path_for(proposal_id), expected_id=proposal_id)

    def list(self) -> tuple[LessonProposal, ...]:
        if not self._existing_directory():
            return ()
        proposals = []
        for path in sorted(self.directory.glob("*.json"), key=lambda item: (item.name.casefold(), item.name)):
            self._assert_safe_path(path, must_exist=True)
            proposals.append(self._read_path(path))
        return tuple(proposals)

    def transition(
        self, proposal_id: str, *, expected_version: int, to_status: str, actor: str,
        reason: str | None = None,
    ) -> LessonProposal:
        """Apply the closed review lifecycle with optimistic versioning."""
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise LessonError("expected_version must be a positive integer")
        actor = _identifier(actor, "lesson transition actor")
        if to_status not in _STATUS_TRANSITIONS:
            raise LessonError(f"unsupported lesson status: {to_status!r}")
        if not self._existing_directory():
            raise LessonNotFoundError(f"lesson proposal does not exist: {proposal_id}")
        path = self._path_for(proposal_id)
        with self._update_lock(path):
            current = self._read_path(path, expected_id=proposal_id)
            if current.version != expected_version:
                raise LessonConflictError(
                    f"lesson version conflict for {proposal_id}: expected {expected_version}, found {current.version}"
                )
            if to_status not in _STATUS_TRANSITIONS[current.status]:
                raise LessonError(f"lesson transition {current.status!r} -> {to_status!r} is not allowed")
            if actor == current.author:
                raise LessonError("lesson author cannot review, approve, or reject their own proposal")
            timestamp = self._clock()
            record = current.to_dict()
            if to_status == "reviewed":
                if reason is not None:
                    raise LessonError("review transition does not accept a decision reason")
                record.update({"reviewer": actor, "reviewed_at": timestamp})
            else:
                if current.reviewer != actor:
                    raise LessonError("only the recorded independent reviewer may decide the proposal")
                record["decision_reason"] = _bounded_visible(reason, "decision reason")
                record["decided_at"] = timestamp
            record.update({"status": to_status, "updated_at": timestamp, "version": current.version + 1})
            updated = LessonProposal.from_mapping(record)
            self._replace(path, self._render(updated))
            return updated

    def promotion_plan(self, proposal_id: str) -> dict[str, Any]:
        """Return deterministic canonical-source work for an approved proposal.

        This is intentionally a pure read.  Applying edits, compiling generated
        projections, running checks, and acquiring authority are separate
        explicit workflows.
        """
        proposal = self.read(proposal_id)
        if proposal.status != "approved":
            raise LessonError("lesson promotion requires approved status")
        proposal_dict = proposal.to_dict()
        digest = sha256(_canonical_json(proposal_dict).encode("utf-8")).hexdigest()
        changes = sorted(
            (item.to_dict() for item in proposal.affected_contracts),
            key=lambda item: (item["canonical_source"], item["contract_id"], item["change"]),
        )
        checks = sorted(
            (item.to_dict() for item in proposal.regression_checks),
            key=lambda item: (item["id"], item["description"], tuple(item["argv"])),
        )
        evidence = sorted(
            (item.to_dict() for item in proposal.evidence),
            key=lambda item: (item["kind"], item["ref"], item["sha256"]),
        )
        return {
            "kind": "tasktra.lesson-promotion-plan",
            "version": 1,
            "proposal_id": proposal.id,
            "proposal_version": proposal.version,
            "proposal_sha256": digest,
            "canonical_changes": changes,
            "evidence": evidence,
            "regression_checks": checks,
            "requires_projection_regeneration": True,
            "applies_no_changes": True,
        }

    plan_promotion = promotion_plan


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LessonError(f"duplicate lesson proposal key: {key}")
        result[key] = value
    return result
