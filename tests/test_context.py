from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.context import ContextCache, ContextError, build_stage_packet, render_stage_packet


class ContextTests(unittest.TestCase):
    def test_reuse_requires_current_content_and_returns_isolated_facts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "sample.py"
            file.write_text("def before():\n    return 1\n", encoding="utf-8")
            cache = ContextCache()
            first = cache.inspect(root, ["sample.py"])
            first["files"][0]["symbols"][0]["name"] = "poisoned"
            second = cache.inspect(root, ["sample.py"])
            self.assertEqual(second["reused_files"], 1)
            self.assertEqual(second["files"][0]["symbols"][0]["name"], "before")
            file.write_text("def after():\n    return 2\n", encoding="utf-8")
            third = cache.inspect(root, ["sample.py"])
            self.assertEqual(third["reused_files"], 0)
            self.assertNotEqual(second["sha256"], third["sha256"])

    def test_paths_bounds_missing_and_nonfiles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ContextCache()
            for path in ("../secret", "/secret", "C:/secret", "a/../secret", "a\\secret", "."):
                with self.subTest(path=path), self.assertRaises(ContextError):
                    cache.inspect(root, [path])
            self.assertEqual(cache.inspect(root, ["missing"])["files"], [{"path": "missing", "state": "missing"}])
            (root / "folder").mkdir()
            with self.assertRaises(ContextError):
                cache.inspect(root, ["folder"])
            (root / "large").write_bytes(b"x" * (256 * 1024 + 1))
            self.assertEqual(cache.inspect(root, ["large"])["files"][0]["state"], "omitted")

    def test_packet_keeps_required_constraints_and_discards_success_prose(self):
        arguments = dict(goal={"title": "Goal", "description": "Do work", "acceptance": ["Original criterion"]},
                         unit={"title": "Unit", "scope": {"paths": ["."], "exclusions": []}},
                         contract={"quality_requirements": ["Required check"], "prohibited_actions": ["deploy"]},
                         stage="reviewer", patch={"patch_sha256": "a" * 64, "changed_paths": ["a.py"]},
                         evidence={"files": [{"path": f"file-{i}.py", "symbols": ["x" * 1000]} for i in range(30)]},
                         validations=[{"status": "passed", "argv": ["check"]}],
                         prior_reports=[{"stage": "implementer", "summary": "success prose" * 500, "findings": []}],
                         max_bytes=1400)
        packet = build_stage_packet(**arguments)
        self.assertLessEqual(len(render_stage_packet(packet).encode()), 1400)
        self.assertGreater(packet["source_navigation_omitted"], 0)
        self.assertEqual(packet["required"]["quality_requirements"], ["Required check"])
        self.assertEqual(packet["required"]["prohibited_actions"], ["deploy"])
        self.assertNotIn("success prose", render_stage_packet(packet))
        arguments["contract"] = {"quality_requirements": ["must retain " * 1000]}
        with self.assertRaisesRegex(ContextError, "required acceptance"):
            build_stage_packet(**arguments)

    def test_unresolved_findings_are_not_summarized_away(self):
        packet = build_stage_packet(goal={"title": "G", "description": "D"},
            unit={"title": "U", "scope": {"paths": ["."], "exclusions": []}}, contract={}, stage="reviewer",
            patch={"patch_sha256": "a" * 64, "changed_paths": []}, evidence={}, validations=[],
            prior_reports=[{"stage": "tester", "findings": ["Broken edge case"]}])
        self.assertEqual(packet["required"]["unresolved_reports"][0]["findings"], ["Broken edge case"])

    def test_packet_preserves_every_envelope_boundary(self):
        boundaries = {"scope": {"paths": ["src"], "exclusions": ["src/private"]},
            "resource_scopes": [{"provider": "example", "container": "approved"}],
            "allowed_actions": ["inspect"], "allowed_effects": ["read-only"],
            "budgets": {"tokens": 500}, "dependencies": ["prerequisite"], "checkpoints": [{"id": "review"}]}
        packet = build_stage_packet(goal={"title": "G", "description": "D"},
            unit={"title": "U", "scope": {"paths": ["src"], "exclusions": []}, "checkpoint_id": "review"},
            contract=boundaries, stage="reviewer", patch={"patch_sha256": "a" * 64, "changed_paths": []},
            evidence={}, validations=[], prior_reports=[])
        self.assertEqual(packet["required"]["authority_boundaries"], boundaries)
        self.assertEqual(packet["required"]["work"]["checkpoint_id"], "review")
