import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tasktra.contracts import validate_named
from tasktra.handoffs import HANDOFF_KIND, HANDOFF_VERSION
from tasktra.workflow import accept_handoff, new_workflow
from tasktra.workitems import (
    WorkItemConflictError,
    WorkItemError,
    WorkItemExistsError,
    WorkItemStore,
    WorkItemSummary,
    MAX_BODY_CHARS,
    MAX_METADATA_DEPTH,
    MAX_METADATA_LIST_ITEMS,
    MAX_TITLE_CHARS,
    MAX_WORK_ITEM_FILE_BYTES,
)


def completed_workflow(goal_id="goal-1", work_unit_id="one"):
    source = {"goal_id": goal_id, "work_unit_id": work_unit_id}

    def handoff(identifier, role, actor):
        return {
            "kind": HANDOFF_KIND, "version": HANDOFF_VERSION, "handoff_id": identifier,
            "source": source, "producer": {"role": role, "actor_id": actor},
            "human_summary": f"{role} completed bounded work.",
            "status": {"state": "completed", "summary": "Ready to advance."},
            "verified_facts": [{"statement": "Focused check passed.", "evidence_ids": ["check"]}],
            "inferences": [], "changed_paths": [],
            "validation_results": [{"name": "check", "outcome": "passed", "detail": "Passed.", "evidence_ids": ["check"]}],
            "evidence_refs": [{"id": "check", "kind": "command", "locator": "python -m unittest", "summary": "Focused check."}],
            "blockers": [],
            "downstream_brief": {"objective": "Advance safely.", "context": [], "constraints": [], "recommended_next_steps": []},
            "requested_actions": [],
        }

    state = new_workflow(source)
    state = accept_handoff(state, handoff("implementation-result", "implementer", "implementer-one"))
    state = accept_handoff(state, handoff("test-result", "tester", "tester-one"))
    return accept_handoff(state, handoff("review-result", "reviewer", "reviewer-one"))


class WorkItemStoreTests(unittest.TestCase):
    def test_create_read_list_and_safe_update(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            first = store.create(
                item_id="first-item", title=" First item ", body="# Plan\n", goal_id="goal-1",
                labels=["Local", "workflow"], metadata={"estimate": 2},
            )
            store.create(item_id="another-item", title="Another")
            self.assertEqual(first.title, "First item")
            self.assertEqual(first.version, 1)
            self.assertEqual(first.schema_version, 1)
            summaries = store.list()
            self.assertEqual([item.id for item in summaries], ["another-item", "first-item"])
            self.assertTrue(all(isinstance(item, WorkItemSummary) for item in summaries))
            self.assertFalse(hasattr(summaries[0], "body"))
            self.assertNotIn("body", summaries[0].as_dict())
            self.assertEqual(store.read("first-item").as_dict(), first.as_dict())
            validate_named(first.as_dict(), "work-item")

            updated = store.update("first-item", expected_version=1, status="in-review", body="# Revised\n")
            self.assertEqual(updated.version, 2)
            self.assertEqual(updated.status, "in-review")
            self.assertEqual(store.read("first-item").body, "# Revised\n")
            with self.assertRaises(WorkItemConflictError):
                store.update("first-item", expected_version=1, title="stale")

    def test_create_never_overwrites_and_frontmatter_is_deterministic(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            store.create(item_id="one", title="One", metadata={"z": 1, "a": True})
            path = Path(directory) / ".tasktra" / "work-items" / "one.md"
            original = path.read_text(encoding="utf-8")
            self.assertTrue(original.startswith('---\n{"created_at":'))
            self.assertIn('"metadata":{"a":true,"z":1}', original)
            with self.assertRaises(WorkItemExistsError):
                store.create(item_id="one", title="Replacement")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_rejects_traversal_and_invalid_ids(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            for item_id in ("../escape", "nested/item", "CAPS", "two--dash", ""):
                with self.subTest(item_id=item_id):
                    with self.assertRaises(WorkItemError):
                        store.create(item_id=item_id, title="Unsafe")
            self.assertFalse((Path(directory) / "escape.md").exists())

    def test_work_item_and_goal_identifiers_reject_uppercase_and_sixty_five_char_values(self):
        invalid_ids = ("Uppercase", "a" * 65)
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            for identifier in invalid_ids:
                with self.subTest(item_id=identifier):
                    with self.assertRaisesRegex(WorkItemError, "lowercase slug"):
                        store.create(item_id=identifier, title="Unsafe")
                with self.subTest(goal_id=identifier):
                    with self.assertRaisesRegex(WorkItemError, "lowercase slug"):
                        store.create(item_id="valid-item", title="Unsafe", goal_id=identifier)

    def test_rejects_symlinked_work_item_directory_and_file(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            target = Path(outside)
            tasktra = root / ".tasktra"
            tasktra.mkdir()
            link = tasktra / "work-items"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            with self.assertRaisesRegex(WorkItemError, "symbolic link"):
                WorkItemStore(root).create(item_id="escape", title="Unsafe")
            self.assertFalse((target / "escape.md").exists())

            link.unlink()
            store = WorkItemStore(root)
            store.create(item_id="safe", title="Safe")
            item_path = store.directory / "safe.md"
            outside_file = target / "outside.md"
            outside_file.write_text(item_path.read_text(encoding="utf-8"), encoding="utf-8")
            item_path.unlink()
            try:
                item_path.symlink_to(outside_file)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            with self.assertRaisesRegex(WorkItemError, "symbolic link"):
                store.read("safe")

    def test_rejects_internal_target_work_item_symlink(self):
        """A managed path cannot be redirected, even to another project path."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "internal-target"
            target.mkdir()
            tasktra = root / ".tasktra"
            tasktra.mkdir()
            link = tasktra / "work-items"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            with self.assertRaisesRegex(WorkItemError, "symbolic link"):
                WorkItemStore(root).create(item_id="internal-redirect", title="Unsafe")
            self.assertFalse((target / "internal-redirect.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_rejects_windows_junction_without_requiring_symlink_privilege(self):
        """Junctions are ordinary user operations and must not escape the root."""
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            target = Path(outside)
            tasktra = root / ".tasktra"
            tasktra.mkdir()
            junction = tasktra / "work-items"
            command = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
            with self.assertRaisesRegex(WorkItemError, "reparse point"):
                WorkItemStore(root).create(item_id="escape", title="Unsafe")
            self.assertFalse((target / "escape.md").exists())

    def test_malformed_or_mismatched_documents_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            store.create(item_id="valid", title="Valid")
            path = store.directory / "valid.md"
            path.write_text("---\n{}\n---\n", encoding="utf-8")
            with self.assertRaises(WorkItemError):
                store.read("valid")
            # Directly write a valid-looking but filename-mismatched record.
            path.write_text(
                "---\n{\"created_at\":\"x\",\"goal_id\":null,\"id\":\"other\",\"labels\":[],\"metadata\":{},\"schema_version\":1,\"status\":\"planned\",\"title\":\"x\",\"updated_at\":\"x\",\"version\":1}\n---\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkItemError, "filename"):
                store.read("valid")

    def test_update_requires_explicit_current_version(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            store.create(item_id="one", title="One")
            with self.assertRaises(WorkItemError):
                store.update("one", expected_version=0, title="Two")

    def test_done_requires_matching_completed_workflow(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            with self.assertRaisesRegex(WorkItemError, "cannot be created as done"):
                store.create(item_id="finished", title="Finished", status="done")
            store.create(item_id="one", title="One", goal_id="goal-1")
            with self.assertRaisesRegex(WorkItemError, "completed workflow"):
                store.update("one", expected_version=1, status="done")
            with self.assertRaisesRegex(WorkItemError, "does not belong"):
                store.update(
                    "one", expected_version=1, status="done",
                    completion_workflow=completed_workflow(work_unit_id="other"),
                )
            updated = store.update(
                "one", expected_version=1, status="done",
                completion_workflow=completed_workflow(),
            )
            self.assertEqual(updated.status, "done")
            self.assertEqual(updated.metadata["workflow_completion"]["source"]["work_unit_id"], "one")

    def test_rejects_duplicate_and_non_finite_frontmatter_values(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            store.create(item_id="one", title="One")
            path = store.directory / "one.md"
            path.write_text('---\n{"id":"one","id":"other"}\n---\n', encoding="utf-8")
            with self.assertRaisesRegex(WorkItemError, "frontmatter JSON"):
                store.read("one")
            with self.assertRaises(WorkItemError):
                store.update("one", expected_version=1, metadata={"score": float("nan")})

    def test_rejects_strict_field_metadata_and_file_size_bounds(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            with self.assertRaises(WorkItemError):
                store.create(item_id="one", title="x" * (MAX_TITLE_CHARS + 1))
            with self.assertRaises(WorkItemError):
                store.create(item_id="one", title="One", body="x" * (MAX_BODY_CHARS + 1))
            with self.assertRaisesRegex(WorkItemError, "exceeds"):
                store.create(item_id="one", title="One", body="🙂" * MAX_BODY_CHARS)
            with self.assertRaises(WorkItemError):
                store.create(item_id="one", title="One", metadata={"items": list(range(MAX_METADATA_LIST_ITEMS + 1))})
            too_deep: object = "leaf"
            for _ in range(MAX_METADATA_DEPTH + 1):
                too_deep = {"next": too_deep}
            with self.assertRaises(WorkItemError):
                store.create(item_id="one", title="One", metadata={"nested": too_deep})
            store.create(item_id="one", title="One")
            path = store.directory / "one.md"
            path.write_bytes(b"x" * (MAX_WORK_ITEM_FILE_BYTES + 1))
            with self.assertRaisesRegex(WorkItemError, "exceeds"):
                store.read("one")

    def test_rejects_non_finite_exponent_and_nested_duplicate_frontmatter(self):
        with TemporaryDirectory() as directory:
            store = WorkItemStore(directory)
            store.create(item_id="one", title="One")
            path = store.directory / "one.md"
            prefix = (
                '{"created_at":"x","goal_id":null,"id":"one","labels":[],"metadata":'
            )
            suffix = (
                ',"schema_version":1,"status":"planned","title":"One","updated_at":"x","version":1}'
            )
            path.write_text(f"---\n{prefix}{{\"score\":1e999999}}{suffix}\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkItemError, "frontmatter JSON"):
                store.read("one")
            path.write_text(
                f"---\n{prefix}{{\"nested\":{{\"x\":1,\"x\":2}}}}{suffix}\n---\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkItemError, "frontmatter JSON"):
                store.read("one")
