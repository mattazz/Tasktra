"""Read-only, deterministic evidence audit for the Tasktra 1.0 release gate."""

from __future__ import annotations

import base64
import binascii
import configparser
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tomllib
from typing import Iterable
from urllib.parse import urlparse

from .state import SCHEMA_VERSION


@dataclass(frozen=True)
class ReleaseCheck:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ReleaseAudit:
    version: str
    checks: tuple[ReleaseCheck, ...]

    @property
    def ok(self) -> bool:
        return all(item.passed for item in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "version": self.version, "checks": [item.as_dict() for item in self.checks]}


def audit_release(root: Path | str, *, expected_version: str = "1.0.0") -> ReleaseAudit:
    """Inspect durable release artifacts without executing or mutating anything."""
    project = Path(root).resolve()
    checks = [
        _version_check(project, expected_version),
        _paths_check(project, "release-documentation", (
            "CHANGELOG.md", "LICENSE", "docs/OPERATIONS.md", "docs/RELEASE_POLICY.md", "docs/SELF_HOSTING.md",
        )),
        _paths_check(project, "generic-examples", tuple(
            f"examples/{name}" for name in ("application", "service-api", "web", "python", "typescript", "monorepo")
        )),
        _ci_check(project),
        _stage_check(project),
        _decision_check(project, expected_version),
    ]
    return ReleaseAudit(expected_version, tuple(checks))


def _versions(project: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        result["package"] = str(tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
        result["catalog"] = str(tomllib.loads((project / "catalog/catalog.toml").read_text(encoding="utf-8"))["catalog"]["version"])
        for pack_path in sorted((project / "catalog/packs").glob("*/pack.toml")):
            result[f"pack:{pack_path.parent.name}"] = str(
                tomllib.loads(pack_path.read_text(encoding="utf-8"))["pack"]["version"]
            )
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', (project / "src/tasktra/__init__.py").read_text(encoding="utf-8"), re.MULTILINE)
        if match is not None:
            result["runtime"] = match.group(1)
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return result
    return result


def _version_check(project: Path, expected_version: str) -> ReleaseCheck:
    """Bind source versions to compiled projections and the live runtime."""
    versions = _versions(project)
    pack_directories = {
        path.name for path in (project / "catalog/packs").iterdir() if path.is_dir()
    } if (project / "catalog/packs").is_dir() else set()
    source_packs = {name.removeprefix("pack:") for name in versions if name.startswith("pack:")}
    required_surfaces = {"package", "catalog", "runtime"}
    problems = [
        f"{name}={value}" for name, value in sorted(versions.items())
        if value != expected_version
    ]
    if not pack_directories or source_packs != pack_directories or not required_surfaces.issubset(versions):
        problems.append("source version surfaces are incomplete")
    try:
        lock_bytes = (project / ".tasktra/tasktra.lock").read_bytes()
        lock = json.loads(lock_bytes)
        manifest_path = project / ".tasktra/generated/manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if lock.get("tasktra_version") != expected_version:
            problems.append(f"lock.tasktra_version={lock.get('tasktra_version')}")
        if lock.get("catalog_version") != expected_version:
            problems.append(f"lock.catalog_version={lock.get('catalog_version')}")
        lock_pack_versions = lock.get("pack_versions")
        lock_packs = lock.get("packs")
        pack_contracts = lock.get("pack_contracts")
        selected_packs = set(lock_packs) if isinstance(lock_packs, list) else set()
        if (
            not isinstance(lock_pack_versions, dict)
            or set(lock_pack_versions) != selected_packs
            or not lock_pack_versions
            or any(value != expected_version for value in lock_pack_versions.values())
            or not isinstance(lock_packs, list)
            or not selected_packs
            or not selected_packs.issubset(source_packs)
            or len(lock_packs) != len(selected_packs)
            or not isinstance(pack_contracts, dict)
            or set(pack_contracts) != selected_packs
            or any(not isinstance(contract, dict) or contract.get("version") != expected_version for contract in pack_contracts.values())
        ):
            problems.append("lock pack versions are inconsistent")
        if lock.get("schema_versions", {}).get("runtime") != SCHEMA_VERSION:
            problems.append(f"lock.runtime_schema={lock.get('schema_versions', {}).get('runtime')}")
        manifest_packs = manifest.get("packs")
        manifest_files = manifest.get("files")
        if (
            manifest.get("tasktra_version") != expected_version
            or manifest.get("catalog_version") != expected_version
            or not isinstance(manifest_packs, list)
            or set(manifest_packs) != selected_packs
            or len(manifest_packs) != len(selected_packs)
            or not isinstance(manifest_files, list)
            or not manifest_files
        ):
            problems.append("generated manifest version is inconsistent")
        if lock.get("generated_manifest_sha256") != sha256(manifest_bytes).hexdigest():
            problems.append("generated manifest hash does not match lock")
        manifest_paths: set[str] = set()
        for entry in manifest_files if isinstance(manifest_files, list) else []:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
                problems.append("generated manifest contains an invalid file entry")
                continue
            if entry["path"] in manifest_paths:
                problems.append(f"generated manifest contains a duplicate path: {entry['path']}")
                continue
            manifest_paths.add(entry["path"])
            target = (project / entry["path"]).resolve()
            if project not in target.parents or not target.is_file():
                problems.append(f"generated file missing or unsafe: {entry.get('path')}")
                continue
            if sha256(target.read_bytes()).hexdigest() != entry.get("sha256"):
                problems.append(f"generated file drift: {entry.get('path')}")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        problems.append("lock or generated manifest is missing or invalid")
    try:
        database = project / ".tasktra/runtime/tasktra.sqlite"
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            runtime_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        if runtime_schema != SCHEMA_VERSION:
            problems.append(f"live.runtime_schema={runtime_schema}")
    except (OSError, sqlite3.Error):
        problems.append("live runtime schema is unavailable")
    detail = "complete" if not problems else "; ".join(problems)
    return ReleaseCheck("version-consistency", not problems, detail)


def _paths_check(project: Path, name: str, paths: Iterable[str]) -> ReleaseCheck:
    missing = [path for path in paths if not (project / path).exists()]
    return ReleaseCheck(name, not missing, "complete" if not missing else f"missing: {', '.join(missing)}")


def _ci_check(project: Path) -> ReleaseCheck:
    try:
        content = (project / ".github/workflows/ci.yml").read_text(encoding="utf-8").casefold()
    except OSError:
        return ReleaseCheck("cross-platform-ci-definition", False, "CI workflow is missing")
    required = ("ubuntu-latest", "windows-latest", "macos-latest", '"3.11"', '"3.12"', '"3.13"')
    missing = [item for item in required if item not in content]
    return ReleaseCheck("cross-platform-ci-definition", not missing, "complete" if not missing else f"missing: {', '.join(missing)}")


def _stage_check(project: Path) -> ReleaseCheck:
    try:
        content = (project / "docs/stages/stage-7.md").read_text(encoding="utf-8")
    except OSError:
        return ReleaseCheck("stage-seven-checklist", False, "checklist is missing")
    remaining = content.count("- [ ]")
    return ReleaseCheck("stage-seven-checklist", remaining == 0, f"{remaining} unchecked criteria")


def _decision_check(project: Path, expected_version: str) -> ReleaseCheck:
    path = project / ".tasktra/evidence/stage-7-release-decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ReleaseCheck("release-decision", False, "signed release decision is missing or invalid")
    if not isinstance(value, dict):
        return ReleaseCheck("release-decision", False, "release decision is incomplete")
    expected_decision_keys = {
        "kind", "schema_version", "version", "decision", "source_revision", "repository",
        "release_artifact_sha256", "ci_evidence", "independent_review", "signature",
        "known_limitations", "compatibility_promises", "recovery_expectations",
        "post_1_0_upgrade_policy",
    }
    if set(value) != expected_decision_keys:
        return ReleaseCheck("release-decision", False, "release decision uses an incomplete or open schema")
    source_revision = value.get("source_revision")
    repository = value.get("repository")
    artifact_hash = value.get("release_artifact_sha256")
    local_revision, local_repository = _local_repository_binding(project)

    ci_reference = value.get("ci_evidence")
    ci, ci_hash = _load_bound_json(project, ci_reference, reviewer=False)
    review_reference = value.get("independent_review")
    review, review_hash = _load_bound_json(project, review_reference, reviewer=True)

    workflow_path = ".github/workflows/ci.yml"
    try:
        (project / workflow_path).read_bytes()
    except OSError:
        committed_workflow = None
    else:
        committed_workflow = _committed_bytes(project, source_revision, workflow_path)
    workflow_hash = sha256(committed_workflow).hexdigest() if committed_workflow is not None else None

    expected_matrix = {
        (platform, python)
        for platform in ("windows", "macos", "linux")
        for python in ("3.11", "3.12", "3.13")
    }
    runs = ci.get("runs") if isinstance(ci, dict) else None
    expected_ci_keys = {
        "kind", "schema_version", "provider", "repository", "source_revision", "workflow_path",
        "workflow_sha256", "release_artifact_sha256", "runs",
    }
    ci_valid = (
        isinstance(ci, dict)
        and set(ci) == expected_ci_keys
        and ci.get("kind") == "tasktra.ci-evidence"
        and ci.get("schema_version") == 1
        and ci.get("provider") == "github-actions"
        and ci.get("repository") == repository
        and ci.get("source_revision") == source_revision
        and ci.get("workflow_path") == workflow_path
        and ci.get("workflow_sha256") == workflow_hash
        and ci.get("release_artifact_sha256") == artifact_hash
    )
    observed_matrix: set[tuple[str, str]] = set()
    observed_jobs: set[tuple[str, str]] = set()
    runs_valid = ci_valid and isinstance(runs, list) and len(runs) == len(expected_matrix)
    if runs_valid:
        for run in runs:
            expected_run_keys = {
                "platform", "python", "status", "run_id", "job_id", "run_url", "commit_sha",
                "artifact_sha256", "workflow",
            }
            if not isinstance(run, dict) or set(run) != expected_run_keys:
                runs_valid = False
                break
            pair = (run.get("platform"), run.get("python"))
            revision = run.get("commit_sha")
            run_id = run.get("run_id")
            job_id = run.get("job_id")
            expected_url = f"https://github.com/{repository}/actions/runs/{run_id}/job/{job_id}"
            if (
                pair in observed_matrix
                or pair not in expected_matrix
                or run.get("status") != "passed"
                or not isinstance(run_id, str)
                or re.fullmatch(r"[1-9][0-9]*", run_id) is None
                or not isinstance(job_id, str)
                or re.fullmatch(r"[1-9][0-9]*", job_id) is None
                or (run_id, job_id) in observed_jobs
                or run.get("run_url") != expected_url
                or not isinstance(revision, str)
                or revision != source_revision
                or run.get("artifact_sha256") != artifact_hash
                or run.get("workflow") != workflow_path
            ):
                runs_valid = False
                break
            observed_matrix.add(pair)
            observed_jobs.add((run_id, job_id))
    runs_valid = runs_valid and observed_matrix == expected_matrix

    expected_review_keys = {
        "kind", "schema_version", "version", "status", "reviewer_id", "reviewed_at",
        "source_revision", "repository", "release_artifact_sha256", "ci_evidence_sha256",
        "findings", "blockers",
    }
    reviewer_id = review_reference.get("reviewer_id") if isinstance(review_reference, dict) else None
    review_valid = (
        isinstance(review, dict)
        and set(review) == expected_review_keys
        and review.get("kind") == "tasktra.independent-release-review"
        and review.get("schema_version") == 1
        and review.get("version") == expected_version
        and review.get("status") == "GO"
        and isinstance(reviewer_id, str)
        and bool(reviewer_id.strip())
        and review.get("reviewer_id") == reviewer_id
        and _timestamp(review.get("reviewed_at"))
        and review.get("source_revision") == source_revision
        and review.get("repository") == repository
        and review.get("release_artifact_sha256") == artifact_hash
        and review.get("ci_evidence_sha256") == ci_hash
        and isinstance(review.get("findings"), list)
        and isinstance(review.get("blockers"), list)
        and not review["blockers"]
    )

    signature_valid = _verify_decision_signature(project, value)
    passed = (
        value.get("kind") == "tasktra.release-decision"
        and value.get("schema_version") == 1
        and
        value.get("version") == expected_version
        and value.get("decision") == "approved"
        and re.fullmatch(r"[0-9a-f]{40}", str(source_revision or "")) is not None
        and source_revision == local_revision
        and repository == local_repository
        and re.fullmatch(r"[0-9a-f]{64}", str(artifact_hash or "")) is not None
        and ci_hash is not None
        and review_hash is not None
        and _statements(value.get("known_limitations"))
        and _statements(value.get("compatibility_promises"))
        and _statements(value.get("recovery_expectations"))
        and _statements(value.get("post_1_0_upgrade_policy"))
        and runs_valid
        and review_valid
        and signature_valid
    )
    return ReleaseCheck(
        "release-decision", passed,
        "approved with bound CI and review evidence" if passed else "release decision lacks authoritative bound evidence",
    )


def _load_bound_json(project: Path, reference: object, *, reviewer: bool) -> tuple[dict[str, object], str | None]:
    expected_keys = {"path", "sha256", "reviewer_id"} if reviewer else {"path", "sha256"}
    if not isinstance(reference, dict) or set(reference) != expected_keys:
        return {}, None
    path = reference.get("path")
    digest = reference.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return {}, None
    project_root = project.resolve()
    target = (project_root / path).resolve()
    try:
        if project_root not in target.parents or not target.is_file():
            return {}, None
        payload = target.read_bytes()
        if sha256(payload).hexdigest() != digest:
            return {}, None
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return {}, None
    return (value, digest) if isinstance(value, dict) else ({}, None)


def _local_repository_binding(project: Path) -> tuple[str | None, str | None]:
    git = project / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            reference = head[5:]
            if not reference.startswith("refs/") or ".." in reference:
                return None, None
            ref_path = (git / reference).resolve()
            if git.resolve() not in ref_path.parents:
                return None, None
            if ref_path.is_file():
                revision = ref_path.read_text(encoding="ascii").strip()
            else:
                revision = next((line.split(" ", 1)[0] for line in (git / "packed-refs").read_text(encoding="ascii").splitlines() if line.endswith(f" {reference}")), "")
        else:
            revision = head
        parser = configparser.RawConfigParser()
        parser.read(git / "config", encoding="utf-8")
        origin = parser.get('remote "origin"', "url")
    except (OSError, configparser.Error, StopIteration):
        return None, None
    parsed = urlparse(origin)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", parsed.path)
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None or parsed.scheme != "https" or parsed.netloc.casefold() != "github.com" or match is None:
        return None, None
    return revision, f"{match.group(1)}/{match.group(2)}"


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _statements(value: object) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= 32
        and all(isinstance(item, str) and bool(item.strip()) and len(item) <= 2000 for item in value)
    )


def _verify_decision_signature(project: Path, decision: dict[str, object]) -> bool:
    signature = decision.get("signature")
    expected_signature_keys = {
        "algorithm", "key_id", "public_key_path", "public_key_sha256", "payload_sha256", "signature_b64",
    }
    if not isinstance(signature, dict) or set(signature) != expected_signature_keys:
        return False
    key_id = signature.get("key_id")
    key_path = signature.get("public_key_path")
    if (
        signature.get("algorithm") != "rsa-pkcs1v15-sha256"
        or not isinstance(key_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key_id) is None
        or key_path != f".tasktra/release-trust/{key_id}.json"
    ):
        return False
    payload = dict(decision)
    payload.pop("signature")
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_digest = sha256(payload_bytes).hexdigest()
    project_root = project.resolve()
    key_file = (project_root / str(key_path)).resolve()
    key_bytes = _committed_bytes(project, decision.get("source_revision"), str(key_path))
    try:
        if key_bytes is None:
            return False
        key = json.loads(key_bytes)
        signature_bytes = base64.b64decode(str(signature.get("signature_b64", "")), validate=True)
    except (OSError, json.JSONDecodeError, binascii.Error, ValueError):
        return False
    if (
        project_root not in key_file.parents
        or sha256(key_bytes).hexdigest() != signature.get("public_key_sha256")
        or payload_digest != signature.get("payload_sha256")
        or not isinstance(key, dict)
        or set(key) != {"kind", "schema_version", "key_id", "algorithm", "modulus_hex", "exponent"}
        or key.get("kind") != "tasktra.release-public-key"
        or key.get("schema_version") != 1
        or key.get("key_id") != key_id
        or key.get("algorithm") != "rsa-pkcs1v15-sha256"
    ):
        return False
    try:
        modulus = int(str(key["modulus_hex"]), 16)
        exponent = int(key["exponent"])
    except (KeyError, TypeError, ValueError):
        return False
    if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0:
        return False
    size = (modulus.bit_length() + 7) // 8
    if len(signature_bytes) != size:
        return False
    decoded = pow(int.from_bytes(signature_bytes, "big"), exponent, modulus).to_bytes(size, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + bytes.fromhex(payload_digest)
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return decoded == expected


def _committed_bytes(project: Path, revision: object, relative_path: str) -> bytes | None:
    if re.fullmatch(r"[0-9a-f]{40}", str(revision or "")) is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "show", f"{revision}:{relative_path}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None
