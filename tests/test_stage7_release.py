"""Stage 7 deterministic release-gate tests."""

from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from hashlib import sha256
import json
import subprocess
import sys
import unittest

from tasktra.release import _decision_check, _version_check, audit_release


ROOT = Path(__file__).resolve().parents[1]
AUDITED_PATHS = (
    "pyproject.toml", "catalog/catalog.toml", "catalog/packs/core/pack.toml",
    "src/tasktra/__init__.py", ".github/workflows/ci.yml", "docs/stages/stage-7.md",
    ".tasktra/evidence/stage-7-release-decision.json",
)

TEST_RSA_MODULUS = int(
    "9fda6cd36aad06d22abc7dccab2492021c6a519e929427e7df09ecd0a7ff8e8c"
    "b85900eaea90d2becc94936a4da0bcad0abfe63a8949b955764093bfc966a993"
    "bbfaefbba620181a70bf2c7df57805fefbdd0d6938127b15bb331ca37db68967"
    "ad7527e557529ff372ee1f733c81e02a183372a9ea8bbd89c3dce9d60b2e7e0"
    "ef9b0e59a532f59247dd4ed0fcf90fe9457b7721c6614a6e04fdb3737af29af"
    "7deb0fd5035f6494176d40e3e9b821823d2dbece879a11564af1c9f66810e146"
    "b1dc74d4e6c2508f993235c8e62f0f9b42777384f454bf166bf7a4cdd9e00d7"
    "9c4250f5909daa07bb1014cab8455835693f3bcac4df1460fa99edc59d4694af001", 16,
)
TEST_RSA_PRIVATE_EXPONENT = int(
    "03f7ed39b66d433d1678857afe48b3234047576d636030396e6d15a6fc74b8ba"
    "3f9d5e0b76f54f761328211cc37e991086b2cae96b1d1c6fc5b6c6b43d30c24"
    "6fba4b82ce56be88d477d47827d0494c986c12f230c9450dff23ccb9a3775bfa"
    "58645e14ead434cdb43602c01b55fd80bd37bfb7dc267e3b5b6da16280d31034"
    "a1bd73d7abeb307f482ce24f602130be328b9d79bfede803f355ed6aef02266cc"
    "39011211973313469932fd71b3c2b58a06d7bf871dabe3df832f45c133f5b3fad"
    "c1671e41311c86da10eb8bda77e13f1095526cc1ad42421bca2b0fe9cfdf91bd"
    "c0cd3f2215a0327a428b5957bea9e8e11a46cbbbb90efecb91a8a5bbb4a9b11", 16,
)


def write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return sha256(path.read_bytes()).hexdigest()


def sign_decision(decision: dict[str, object], key_hash: str) -> None:
    decision.pop("signature", None)
    payload = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_hash = sha256(payload).hexdigest()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + bytes.fromhex(payload_hash)
    size = (TEST_RSA_MODULUS.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), TEST_RSA_PRIVATE_EXPONENT, TEST_RSA_MODULUS).to_bytes(size, "big")
    decision["signature"] = {
        "algorithm": "rsa-pkcs1v15-sha256", "key_id": "release-test",
        "public_key_path": ".tasktra/release-trust/release-test.json",
        "public_key_sha256": key_hash, "payload_sha256": payload_hash,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }


def signed_decision_tree(root: Path) -> tuple[Path, dict[str, object]]:
    artifact_hash = "c" * 64
    repository = "acme/tasktra"
    workflow_path = root / ".github/workflows/ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_bytes(b"name: ci\n")
    workflow_hash = sha256(workflow_path.read_bytes()).hexdigest()
    key = {
        "kind": "tasktra.release-public-key", "schema_version": 1, "key_id": "release-test",
        "algorithm": "rsa-pkcs1v15-sha256", "modulus_hex": format(TEST_RSA_MODULUS, "x"),
        "exponent": 65537,
    }
    key_path = root / ".tasktra/release-trust/release-test.json"
    key_hash = write_json(key_path, key)
    for args in (
        ["git", "init", str(root)],
        ["git", "-C", str(root), "remote", "add", "origin", "https://github.com/acme/tasktra.git"],
        ["git", "-C", str(root), "add", ".github/workflows/ci.yml", ".tasktra/release-trust/release-test.json"],
        ["git", "-C", str(root), "-c", "user.name=Tasktra Test", "-c", "user.email=test@example.test", "commit", "-m", "fixture"],
    ):
        completed = subprocess.run(args, text=True, capture_output=True, shell=False, check=False)
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True,
        shell=False, check=True,
    ).stdout.strip()

    runs = [
        {
            "platform": platform, "python": python, "status": "passed",
            "run_id": "100", "job_id": str(1000 + index),
            "run_url": f"https://github.com/{repository}/actions/runs/100/job/{1000 + index}",
            "commit_sha": revision, "artifact_sha256": artifact_hash,
            "workflow": ".github/workflows/ci.yml",
        }
        for index, (platform, python) in enumerate(
            (platform, python)
            for platform in ("windows", "macos", "linux")
            for python in ("3.11", "3.12", "3.13")
        )
    ]
    ci = {
        "kind": "tasktra.ci-evidence", "schema_version": 1, "provider": "github-actions",
        "repository": repository, "source_revision": revision,
        "workflow_path": ".github/workflows/ci.yml", "workflow_sha256": workflow_hash,
        "release_artifact_sha256": artifact_hash, "runs": runs,
    }
    evidence = root / ".tasktra/evidence"
    ci_path = evidence / "stage-7-ci-runs.json"
    ci_hash = write_json(ci_path, ci)
    review = {
        "kind": "tasktra.independent-release-review", "schema_version": 1,
        "version": "1.0.0", "status": "GO", "reviewer_id": "independent-reviewer",
        "reviewed_at": "2026-09-20T18:00:00Z", "source_revision": revision,
        "repository": repository, "release_artifact_sha256": artifact_hash,
        "ci_evidence_sha256": ci_hash, "findings": [], "blockers": [],
    }
    review_path = evidence / "independent-review.json"
    review_hash = write_json(review_path, review)
    decision: dict[str, object] = {
        "kind": "tasktra.release-decision", "schema_version": 1, "version": "1.0.0",
        "decision": "approved", "source_revision": revision, "repository": repository,
        "release_artifact_sha256": artifact_hash,
        "known_limitations": ["Remote providers remain optional and unavailable providers cannot block eligible local work."],
        "compatibility_promises": ["The 1.x line preserves published contracts except for explicitly additive versioned fields."],
        "recovery_expectations": ["Runtime schema recovery requires the verified pre-migration database backup and explicit human authority."],
        "post_1_0_upgrade_policy": ["Every upgrade remains preview-first, checksum-bound, validated, and recoverable."],
        "ci_evidence": {"path": ".tasktra/evidence/stage-7-ci-runs.json", "sha256": ci_hash},
        "independent_review": {
            "path": ".tasktra/evidence/independent-review.json", "sha256": review_hash,
            "reviewer_id": "independent-reviewer",
        },
    }
    sign_decision(decision, key_hash)
    decision_path = evidence / "stage-7-release-decision.json"
    write_json(decision_path, decision)
    return decision_path, decision


def audit_snapshot() -> dict[str, str | None]:
    return {
        name: sha256((ROOT / name).read_bytes()).hexdigest() if (ROOT / name).is_file() else None
        for name in AUDITED_PATHS
    }


class ReleaseAuditTests(unittest.TestCase):
    def test_repository_audit_is_read_only_and_reports_each_gate(self):
        before = audit_snapshot()
        report = audit_release(ROOT)
        after = audit_snapshot()
        self.assertEqual(after, before)
        self.assertEqual(report.version, "1.0.0")
        self.assertEqual({item.name for item in report.checks}, {
            "version-consistency", "release-documentation", "generic-examples",
            "cross-platform-ci-definition", "stage-seven-checklist", "release-decision",
        })

    def test_missing_repository_never_looks_release_ready(self):
        with TemporaryDirectory() as directory:
            report = audit_release(Path(directory))
            self.assertFalse(report.ok)
            self.assertTrue(any(not item.passed for item in report.checks))

    def test_cli_release_audit_is_read_only_and_fails_closed(self):
        before = audit_snapshot()
        completed = subprocess.run(
            [sys.executable, "-m", "tasktra", "release", "--root", str(ROOT), "audit"],
            cwd=ROOT, text=True, capture_output=True, shell=False, check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["action"], "release-audit")
        self.assertEqual(value["mutation"], "none")
        self.assertFalse(value["ok"])
        after = audit_snapshot()
        self.assertEqual(after, before)

    def test_legacy_self_asserted_decision_is_not_authoritative_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / ".tasktra/evidence"
            evidence.mkdir(parents=True)
            (evidence / "stage-7-release-decision.json").write_text(json.dumps({
                "version": "1.0.0", "decision": "approved",
                "all_supported_platform_ci": "passed", "independent_review": "GO",
            }), encoding="utf-8")
            result = _decision_check(root, "1.0.0")
            self.assertFalse(result.passed)
            self.assertIn("schema", result.detail)

    def test_decision_requires_committed_github_matrix_review_and_signature(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path, decision = signed_decision_tree(root)
            self.assertTrue(_decision_check(root, "1.0.0").passed)

            decision["repository"] = "other/repository"
            write_json(path, decision)
            self.assertFalse(_decision_check(root, "1.0.0").passed)

    def test_resigned_decision_rejects_untrusted_run_url_and_artifact_mismatch(self):
        for mutation in ("run-url", "artifact"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                path, decision = signed_decision_tree(root)
                key_hash = decision["signature"]["public_key_sha256"]  # type: ignore[index]
                ci_path = root / ".tasktra/evidence/stage-7-ci-runs.json"
                ci = json.loads(ci_path.read_text(encoding="utf-8"))
                if mutation == "run-url":
                    ci["runs"][0]["run_url"] = "https://ci.example.test/synthetic"
                else:
                    ci["runs"][0]["artifact_sha256"] = "b" * 64
                ci_hash = write_json(ci_path, ci)
                decision["ci_evidence"]["sha256"] = ci_hash  # type: ignore[index]
                review_path = root / ".tasktra/evidence/independent-review.json"
                review = json.loads(review_path.read_text(encoding="utf-8"))
                review["ci_evidence_sha256"] = ci_hash
                review_hash = write_json(review_path, review)
                decision["independent_review"]["sha256"] = review_hash  # type: ignore[index]
                sign_decision(decision, str(key_hash))
                write_json(path, decision)
                self.assertFalse(_decision_check(root, "1.0.0").passed)

    def test_resigned_decision_requires_each_nonempty_policy_statement_set(self):
        for field, remove in (("known_limitations", True), ("compatibility_promises", False),
                              ("recovery_expectations", False), ("post_1_0_upgrade_policy", False)):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                path, decision = signed_decision_tree(root)
                key_hash = decision["signature"]["public_key_sha256"]  # type: ignore[index]
                if remove:
                    decision.pop(field)
                else:
                    decision[field] = []
                sign_decision(decision, str(key_hash))
                write_json(path, decision)
                self.assertFalse(_decision_check(root, "1.0.0").passed)

    def test_version_check_rejects_empty_component_collections(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog/packs").mkdir(parents=True)
            result = _version_check(root, "1.0.0")
            self.assertFalse(result.passed)
            self.assertIn("source version surfaces are incomplete", result.detail)


if __name__ == "__main__":
    unittest.main()
