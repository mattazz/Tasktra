"""Operator drill-down through the public work-impact command."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests import test_work_plan_cli as plans


class DependencyImpactCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = plans.make_project(self.root)
        manifest = plans.example_plan()
        manifest["units"].append({
            "id": "release", "title": "token=private-title", "scope": {"paths": ["src/private-path"], "exclusions": []},
            "prerequisite_ids": ["integration"],
        })
        preview = self.store.preview_work_plan(manifest)
        self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.command = ("work", "--root", str(self.root), "impact", "goal-1", "integration")

    def test_plan_to_impact_journey_preserves_summary_across_pages_and_directions(self):
        before = self.store.path.read_bytes()
        code, result = plans.invoke(*self.command)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["action"], "work-impact")
        report = result["impact"]
        self.assertTrue(report["read_only"])
        self.assertFalse(report["claimability_evaluated"])
        self.assertEqual(report["summary"]["incomplete_blockers_total"], 2)
        self.assertEqual(report["summary"]["all_dependents_total"], 1)
        self.assertEqual(report["summary"]["direct_prerequisite_gates_cleared_if_completed"], 1)
        self.assertEqual([row["work_unit_id"] for row in report["relations"]], ["interface", "service", "release"])
        for arguments, ids, total, next_offset in (
            (("--limit", "1", "--offset", "1"), ["service"], 3, 2),
            (("--direction", "dependents"), ["release"], 1, None),
            (("--direction", "prerequisites"), ["interface", "service"], 2, None),
            (("--offset", "10"), [], 3, None),
        ):
            with self.subTest(arguments=arguments):
                code, result = plans.invoke(*self.command, *arguments)
                self.assertEqual(code, 0, result)
                page = result["impact"]
                self.assertEqual(page["summary"], report["summary"])
                self.assertEqual([row["work_unit_id"] for row in page["relations"]], ids)
                self.assertEqual((page["total"], page["next_offset"]), (total, next_offset))
        self.assertNotIn("private-title", str(report))
        self.assertNotIn("private-path", str(report))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_bounds_and_unknown_anchor_fail_without_writes(self):
        before = self.store.path.read_bytes()
        for arguments in (("--limit", "0"), ("--limit", "101"), ("--offset", "-1"), ("--offset", "1000001")):
            with self.subTest(arguments=arguments):
                code, result = plans.invoke(*self.command, *arguments)
                self.assertEqual(code, 2, result)
                self.assertFalse(result["ok"])
        code, result = plans.invoke(*self.command[:-1], "missing")
        self.assertEqual(code, 2, result)
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_runtime_is_not_initialized_by_inspection(self):
        other = self.root / "empty-project"
        (other / ".tasktra").mkdir(parents=True)
        (other / ".tasktra/project.toml").write_text('[project]\nname="Empty"\nconfig_version=1\n[runtime]\ndatabase="state.sqlite"\n', encoding="utf-8")
        before = sorted(path.relative_to(other).as_posix() for path in other.rglob("*"))
        code, result = plans.invoke("work", "--root", str(other), "impact", "goal-1", "integration")
        self.assertEqual(code, 2, result)
        self.assertFalse(result["ok"])
        self.assertEqual(sorted(path.relative_to(other).as_posix() for path in other.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
