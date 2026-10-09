"""The preview/apply work-plan journey through the public command surface."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyStore
from tasktra.cli import main
from tests import test_stage3_autonomy as stage3


def invoke(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def example_plan():
    return {
        "kind": "tasktra.work-plan", "version": 1, "goal_id": "goal-1",
        "units": [
            {"id": "integration", "title": "Join", "scope": {"paths": ["tests"], "exclusions": []}, "prerequisite_ids": ["service", "interface"]},
            {"id": "service", "title": "Service", "scope": {"paths": ["src/service"], "exclusions": []}},
            {"id": "interface", "title": "Interface", "scope": {"paths": ["src/interface"], "exclusions": []}},
        ],
    }


def make_project(root):
    profile = root / ".tasktra/project.toml"
    profile.parent.mkdir()
    profile.write_text('[project]\nname="Plan"\nconfig_version=1\n[runtime]\ndatabase="state.sqlite"\n', encoding="utf-8")
    store = AutonomyStore(root / "state.sqlite")
    store.create_goal(goal_id="goal-1", title="Delivery", description="Delivery", acceptance=["Done."])
    store.define_goal_contract("goal-1", stage3.envelope(), actor_id="owner", at=stage3.NOW)
    return store


class WorkPlanCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = make_project(self.root)
        self.manifest = self.root / "plan.json"
        self.plan = example_plan()
        self.write(self.plan)
        self.command = ("work", "--root", str(self.root))

    def write(self, plan):
        self.manifest.write_text(json.dumps(plan), encoding="utf-8")

    def preview(self):
        code, response = invoke(*self.command, "plan-preview", str(self.manifest))
        self.assertEqual(code, 0, response)
        return response["plan"]

    def apply(self, digest):
        return invoke(*self.command, "plan-apply", str(self.manifest), "--preview-sha256", digest)

    def test_forward_references_preview_without_writes_then_apply_as_one_plan(self):
        before = self.store.path.read_bytes()
        first = self.preview()
        self.assertTrue(first["read_only"])
        self.assertEqual(first["counts"]["create"], 3)
        self.assertEqual(first["dependency_waves"], [["interface", "service"], ["integration"]])
        shuffled = copy.deepcopy(self.plan)
        shuffled["units"].reverse()
        shuffled["units"][-1]["prerequisite_ids"].reverse()
        self.write(shuffled)
        second = self.preview()
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(first["preview_sha256"], second["preview_sha256"])
        self.assertEqual(self.store.path.read_bytes(), before)
        code, response = self.apply(first["preview_sha256"])
        self.assertEqual(code, 0, response)
        self.assertEqual(response["action"], "work-plan-apply")
        self.assertFalse(response["plan"]["read_only"])
        self.assertEqual(self.store.get_goal("goal-1")["status"], "planned")
        graph = self.store.work_dependencies("goal-1", work_unit_id="integration")
        self.assertEqual({unit["id"] for unit in graph["units"][0]["prerequisites"]}, {"service", "interface"})
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_retry_requires_fresh_preview_and_unchanged_apply_writes_nothing(self):
        first = self.preview()
        self.assertEqual(self.apply(first["preview_sha256"])[0], 0)
        after = self.store.path.read_bytes()
        code, response = self.apply(first["preview_sha256"])
        self.assertEqual((code, response["error_code"]), (2, "stale_preview"))
        retry = self.preview()
        self.assertEqual((retry["counts"]["create"], retry["counts"]["unchanged"]), (0, 3))
        self.assertEqual(retry["manifest_sha256"], first["manifest_sha256"])
        self.assertEqual(self.apply(retry["preview_sha256"])[0], 0)
        self.assertEqual(self.store.path.read_bytes(), after)

    def test_tampered_ledger_returns_structured_errors_without_writing(self):
        digest = self.preview()["preview_sha256"]
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute("UPDATE goals SET title='Unattested change' WHERE id='goal-1'")
            connection.commit()
        finally:
            connection.close()
        before = self.store.path.read_bytes()
        for arguments in (
            ("plan-preview", str(self.manifest)),
            ("plan-apply", str(self.manifest), "--preview-sha256", digest),
        ):
            with self.subTest(command=arguments[0]):
                code, response = invoke(*self.command, *arguments)
                self.assertEqual(code, 2, response)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error_code"], "ledger_integrity")
                self.assertIsInstance(response["details"], dict)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_input_changed_after_preview_cannot_apply_old_digest(self):
        first = self.preview()
        changed = copy.deepcopy(self.plan)
        changed["units"][0]["title"] = "Changed integration scope of work"
        self.write(changed)
        before = self.store.path.read_bytes()
        code, response = self.apply(first["preview_sha256"])
        self.assertEqual((code, response["error_code"]), (2, "stale_preview"))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNone(self.store.get_work_unit("service"))

    def test_existing_conflict_returns_coded_details_without_partial_creation(self):
        self.store.create_work_unit(goal_id="goal-1", work_unit_id="service", title="A different definition", scope={"paths": ["src/service"], "exclusions": []})
        before = self.store.path.read_bytes()
        code, response = invoke(*self.command, "plan-preview", str(self.manifest))
        self.assertEqual((code, response["error_code"]), (2, "conflict"))
        self.assertIn("service", json.dumps(response["details"]))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNone(self.store.get_work_unit("interface"))
        self.assertIsNone(self.store.get_work_unit("integration"))

    def test_malformed_and_oversized_input_are_coded_and_read_only(self):
        before = self.store.path.read_bytes()
        invalid = copy.deepcopy(self.plan)
        invalid["actor"] = "not-authority"
        cases = ((json.dumps(invalid), "invalid_manifest"), ('{"kind":"tasktra.work-plan","kind":"tasktra.work-plan"}', "invalid_manifest"), (" " * 65_537, "input_too_large"))
        for payload, expected in cases:
            with self.subTest(expected=expected, length=len(payload)):
                self.manifest.write_text(payload, encoding="utf-8")
                code, response = invoke(*self.command, "plan-preview", str(self.manifest))
                self.assertEqual((code, response["error_code"]), (2, expected))
                self.assertIsInstance(response["details"], dict)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_preview_digest_cannot_be_reused_in_a_different_project(self):
        first = self.preview()
        with TemporaryDirectory() as directory:
            other_root = Path(directory)
            other = make_project(other_root)
            before = other.path.read_bytes()
            code, response = invoke("work", "--root", str(other_root), "plan-apply", str(self.manifest), "--preview-sha256", first["preview_sha256"])
            self.assertEqual((code, response["error_code"]), (2, "stale_preview"))
            self.assertEqual(other.path.read_bytes(), before)

    def test_apply_requires_an_explicit_preview_digest(self):
        before = self.store.path.read_bytes()
        with self.assertRaises(SystemExit):
            invoke(*self.command, "plan-apply", str(self.manifest))
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
