from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.benchmark_efficiency import assess_scope, fixture


class EfficiencyBenchmarkScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "fixture"
        fixture(self.root)
        self.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        (self.root / "names.py").write_text("def unique_names(values):\n    return []\n", encoding="utf-8")

    def test_only_owned_file_is_allowed_and_generated_runtime_is_excluded(self):
        runtime = self.root / ".tasktra/runtime"
        runtime.mkdir()
        (runtime / "result.json").write_text("{}", encoding="utf-8")
        self.assertTrue(assess_scope(self.root, self.base)["scope_valid"])

    def test_extra_tracked_change_fails_scope(self):
        (self.root / ".tasktra/project.toml").write_text("changed", encoding="utf-8")
        result = assess_scope(self.root, self.base)
        self.assertFalse(result["scope_valid"])
        self.assertIn(".tasktra/project.toml", result["changed_paths"])

    def test_agent_commit_does_not_hide_extra_change(self):
        (self.root / ".gitignore").write_text("changed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Agent-created commit"], cwd=self.root, check=True, capture_output=True)
        result = assess_scope(self.root, self.base)
        self.assertFalse(result["scope_valid"])
        self.assertIn(".gitignore", result["changed_paths"])

    def test_nonignored_extra_file_fails_scope(self):
        (self.root / "extra.py").write_text("pass\n", encoding="utf-8")
        result = assess_scope(self.root, self.base)
        self.assertFalse(result["scope_valid"])
        self.assertEqual(result["untracked_paths"], ["extra.py"])
