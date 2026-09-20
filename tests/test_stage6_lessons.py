import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tasktra.lessons import (
    MAX_LESSON_FILE_BYTES,
    LessonConflictError,
    LessonError,
    LessonProposal,
    LessonProposalStore,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def proposal_args(proposal_id="retry-lesson"):
    return {
        "proposal_id": proposal_id,
        "author": "lesson-author",
        "problem": "Retries after ambiguous writes can duplicate consequential effects.",
        "general_principle": "Require durable reconciliation evidence before retrying a consequential effect.",
        "evidence": [
            {"kind": "validation", "ref": "evidence/provider-retry.json", "sha256": SHA_B},
            {"kind": "audit-event", "ref": "audit/provider-effect-17", "sha256": SHA_A},
        ],
        "affected_contracts": [
            {
                "contract_id": "provider.retry-policy",
                "canonical_source": "catalog/policies/provider-retry.toml",
                "change": "Require provider-owned absence evidence before enabling retry.",
            },
            {
                "contract_id": "provider.result-schema",
                "canonical_source": "src/tasktra/schemas/provider-result.json",
                "change": "Retain typed reconciliation outcomes.",
            },
        ],
        "applicability": {
            "contexts": ["Consequential remote writes", "Non-idempotent provider operations"],
            "constraints": ["Local work must remain available"],
        },
        "risks": ["Overly broad absence claims may unlock duplicate writes"],
        "regression_checks": [
            {
                "id": "provider-retry-regression",
                "description": "Verify ambiguous effects remain locked against retry.",
                "argv": ["python", "-m", "unittest", "tests.test_stage4_provider_execution"],
            },
            {
                "id": "provider-schema-regression",
                "description": "Verify the provider result contract remains closed.",
                "argv": ["python", "-m", "unittest", "tests.test_contracts"],
            },
        ],
    }


class Stage6LessonTests(unittest.TestCase):
    def store(self, directory):
        return LessonProposalStore(directory, clock=lambda: "2026-09-20T12:00:00Z")

    def approve(self, store, proposal_id="retry-lesson"):
        draft = store.create(**proposal_args(proposal_id))
        reviewed = store.transition(
            draft.id, expected_version=draft.version, to_status="reviewed", actor="independent-reviewer"
        )
        return store.transition(
            reviewed.id, expected_version=reviewed.version, to_status="approved",
            actor="independent-reviewer", reason="Evidence and regression coverage are sufficient.",
        )

    def test_create_read_list_and_closed_bounded_schema(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            draft = store.create(**proposal_args())
            self.assertEqual((draft.status, draft.version, draft.schema_version), ("draft", 1, 1))
            self.assertEqual(store.read(draft.id), draft)
            self.assertEqual(store.list(), (draft,))
            self.assertTrue((Path(directory) / ".tasktra" / "lessons" / "retry-lesson.json").is_file())

            with self.assertRaisesRegex(LessonError, "not an allowed property"):
                LessonProposal.from_mapping({**draft.to_dict(), "authority": {"remote-write": True}})
            with self.assertRaisesRegex(LessonError, "longer than maxLength"):
                store.create(**{**proposal_args("oversized-problem"), "problem": "x" * 4097})
            malformed = draft.to_dict()
            malformed["evidence"][0]["credential"] = "secret"
            with self.assertRaisesRegex(LessonError, "not an allowed property"):
                LessonProposal.from_mapping(malformed)
            traversal = draft.to_dict()
            traversal["affected_contracts"][0]["canonical_source"] = "../AGENTS.md"
            with self.assertRaisesRegex(LessonError, "does not match pattern"):
                LessonProposal.from_mapping(traversal)
            with self.assertRaisesRegex(LessonError, "schema_version"):
                LessonProposal.from_mapping({**draft.to_dict(), "schema_version": 2})

    def test_storage_rejects_traversal_and_oversized_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            with self.assertRaisesRegex(LessonError, "lowercase slug"):
                store.create(**proposal_args("../escape"))
            self.assertFalse((root / "escape.json").exists())

            draft = store.create(**proposal_args("oversized-file"))
            path = store.directory / f"{draft.id}.json"
            path.write_bytes(b"{" + b"x" * MAX_LESSON_FILE_BYTES + b"}")
            with self.assertRaisesRegex(LessonError, "exceeds"):
                store.read(draft.id)

    def test_storage_rejects_symlinked_lesson_directory(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root, target = Path(directory), Path(outside)
            store = self.store(root)
            tasktra = root / ".tasktra"
            tasktra.mkdir(exist_ok=True)
            lessons = tasktra / "lessons"
            try:
                lessons.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            with self.assertRaisesRegex(LessonError, "link or reparse point"):
                store.create(**proposal_args("linked-lesson"))
            self.assertFalse((target / "linked-lesson.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_storage_rejects_windows_junction(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root, target = Path(directory), Path(outside)
            tasktra = root / ".tasktra"
            tasktra.mkdir()
            junction = tasktra / "lessons"
            command = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
            with self.assertRaisesRegex(LessonError, "reparse point"):
                self.store(root).create(**proposal_args("junction-escape"))
            self.assertFalse((target / "junction-escape.json").exists())

    def test_review_lifecycle_enforces_independence_and_exact_transitions(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            draft = store.create(**proposal_args())
            with self.assertRaisesRegex(LessonError, "author cannot"):
                store.transition(
                    draft.id, expected_version=1, to_status="reviewed", actor="lesson-author"
                )
            reviewed = store.transition(
                draft.id, expected_version=1, to_status="reviewed", actor="independent-reviewer"
            )
            self.assertEqual((reviewed.status, reviewed.reviewer, reviewed.version),
                             ("reviewed", "independent-reviewer", 2))
            with self.assertRaisesRegex(LessonConflictError, "version conflict"):
                store.transition(
                    reviewed.id, expected_version=1, to_status="approved",
                    actor="independent-reviewer", reason="stale",
                )
            with self.assertRaisesRegex(LessonError, "author cannot"):
                store.transition(
                    reviewed.id, expected_version=2, to_status="approved",
                    actor="lesson-author", reason="self approval",
                )
            with self.assertRaisesRegex(LessonError, "recorded independent reviewer"):
                store.transition(
                    reviewed.id, expected_version=2, to_status="approved",
                    actor="different-reviewer", reason="unassigned approval",
                )
            approved = store.transition(
                reviewed.id, expected_version=2, to_status="approved", actor="independent-reviewer",
                reason="Evidence is durable and checks are sufficient.",
            )
            self.assertEqual((approved.status, approved.version), ("approved", 3))
            with self.assertRaisesRegex(LessonError, "not allowed"):
                store.transition(
                    approved.id, expected_version=3, to_status="rejected", actor="independent-reviewer",
                    reason="too late",
                )

    def test_rejection_is_terminal_and_cannot_be_promoted(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            draft = store.create(**proposal_args("rejected-lesson"))
            reviewed = store.transition(
                draft.id, expected_version=1, to_status="reviewed", actor="independent-reviewer"
            )
            rejected = store.transition(
                reviewed.id, expected_version=2, to_status="rejected", actor="independent-reviewer",
                reason="Evidence does not establish a general lesson.",
            )
            self.assertEqual(rejected.status, "rejected")
            with self.assertRaisesRegex(LessonError, "requires approved"):
                store.promotion_plan(rejected.id)

    def test_promotion_is_deterministic_read_only_plan_for_canonical_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "catalog" / "policies" / "provider-retry.toml"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("# canonical source\n", encoding="utf-8")
            store = self.store(root)
            draft = store.create(**proposal_args())
            with self.assertRaisesRegex(LessonError, "requires approved"):
                store.plan_promotion(draft.id)
            reviewed = store.transition(
                draft.id, expected_version=1, to_status="reviewed", actor="independent-reviewer"
            )
            with self.assertRaisesRegex(LessonError, "requires approved"):
                store.promotion_plan(reviewed.id)
            approved = store.transition(
                reviewed.id, expected_version=2, to_status="approved", actor="independent-reviewer",
                reason="Approved for an explicit canonical-source implementation workflow.",
            )

            before = canonical.read_bytes()
            first = store.promotion_plan(approved.id)
            second = store.promotion_plan(approved.id)
            self.assertEqual(first, second)
            self.assertEqual(canonical.read_bytes(), before)
            self.assertTrue(first["applies_no_changes"])
            self.assertNotIn("authority", json.dumps(first).casefold())
            self.assertEqual(
                [item["canonical_source"] for item in first["canonical_changes"]],
                sorted(item["canonical_source"] for item in first["canonical_changes"]),
            )
            self.assertEqual(
                [item["id"] for item in first["regression_checks"]],
                sorted(item["id"] for item in first["regression_checks"]),
            )
            self.assertEqual(len(first["proposal_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
