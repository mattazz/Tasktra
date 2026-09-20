from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
import unittest

from tasktra.adoption import inventory_existing_instructions, preview_initialization
from tasktra.cli import main


class AdoptionTests(unittest.TestCase):
    def test_inventory_hashes_only_instruction_files_in_stable_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("project instructions", encoding="utf-8")
            (root / ".codex/skills/demo").mkdir(parents=True)
            (root / ".codex/skills/demo/SKILL.md").write_text("keep this", encoding="utf-8")
            (root / "unrelated.txt").write_text("not instructions", encoding="utf-8")
            inventory = inventory_existing_instructions(root)
            self.assertEqual([item.path for item in inventory], [".codex/skills/demo/SKILL.md", "AGENTS.md"])
            self.assertNotIn("project instructions", str(inventory))

    def test_preview_never_writes_or_replaces_existing_instructions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "AGENTS.md"
            agents.write_text("existing rules", encoding="utf-8")
            preview = preview_initialization(root, name="Example")
            self.assertTrue(preview.can_initialize)
            self.assertEqual(preview.as_dict()["writes"], [])
            self.assertFalse((root / ".tasktra").exists())
            self.assertEqual(agents.read_text(encoding="utf-8"), "existing rules")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["init", "--root", str(root), "--preview"]), 0)
            self.assertFalse((root / ".tasktra").exists())
            self.assertEqual(agents.read_text(encoding="utf-8"), "existing rules")

    def test_preview_preserves_existing_profile(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tasktra").mkdir()
            (root / ".tasktra/project.toml").write_text("[project]\nname = 'Existing'\n", encoding="utf-8")
            preview = preview_initialization(root)
            self.assertFalse(preview.can_initialize)
            self.assertIsNone(preview.config_content)
            self.assertEqual(preview.as_dict()["config"]["action"], "preserve")
