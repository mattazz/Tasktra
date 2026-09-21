"""The planning pack is opt-in and hands approved plans to Tasktra goals."""

from pathlib import Path, PurePosixPath
import unittest

from tasktra.compiler import compile_catalog, load_catalog


ROOT = Path(__file__).resolve().parents[1]


class DiscoverySkillTests(unittest.TestCase):
    def test_planning_skill_is_projected_only_when_the_optional_pack_is_enabled(self):
        catalog = load_catalog(ROOT / "catalog")
        core = compile_catalog(catalog, ("core",))
        planning = compile_catalog(catalog, ("planning",))
        skill = PurePosixPath(".agents/skills/tasktra-discovery/SKILL.md")
        self.assertNotIn(skill, core.files)
        self.assertIn(skill, planning.files)
        self.assertIn(PurePosixPath(".claude/skills/tasktra-discovery/SKILL.md"), planning.files)
        rendered = planning.files[skill]
        self.assertIn("docs/plans/<plan-id>.md", rendered)
        self.assertIn("tasktra-goal", rendered)
        self.assertIn("does not create a goal", rendered)

