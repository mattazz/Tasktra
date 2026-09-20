"""Portable end-to-end checks for the documented example fixtures."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.config import load_project_config


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
CATALOG = ROOT / "catalog"
FIXTURES = {
    "application": (("generic",), "src/main.py", "ready-to-use"),
    "service-api": (("python",), "src/api.py", "customized"),
    "web": (("typescript-web",), "public/index.html", "ready-to-use"),
    "python": (("python",), "src/library.py", "customized"),
    "typescript": (("typescript-web",), "src/index.ts", "ready-to-use"),
    "monorepo": (("python", "typescript-web", "monorepo"), "pnpm-workspace.yaml", "customized"),
}


def invoke(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class StageSevenExampleTests(unittest.TestCase):
    def test_fixture_inventory_profiles_and_local_markers_are_complete(self) -> None:
        self.assertEqual(
            {path.name for path in EXAMPLES.iterdir() if path.is_dir()},
            set(FIXTURES),
        )
        for name, (packs, marker, profile) in FIXTURES.items():
            with self.subTest(name=name):
                fixture = EXAMPLES / name
                config = load_project_config(fixture)
                self.assertEqual(config.enabled_packs, packs)
                self.assertEqual(len(config.validation_commands), 1)
                self.assertEqual(config.validation_commands[0][0], "python")
                self.assertTrue((fixture / marker).is_file())
                self.assertIn(profile, (fixture / ".tasktra" / "project.toml").read_text(encoding="utf-8"))
                self.assertTrue((fixture / "README.md").is_file())

    def test_fixtures_compile_then_validate_in_an_isolated_copy(self) -> None:
        for name in FIXTURES:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                fixture = Path(directory) / name
                shutil.copytree(EXAMPLES / name, fixture)

                code, stdout, stderr = invoke(
                    "compile", "--root", str(fixture), "--catalog", str(CATALOG), "--trust-catalog",
                )
                self.assertEqual(code, 0, stderr or stdout)
                code, stdout, stderr = invoke(
                    "compile", "--root", str(fixture), "--catalog", str(CATALOG), "--trust-catalog", "--check",
                )
                self.assertEqual(code, 0, stderr or stdout)
                code, stdout, stderr = invoke("validate", "--root", str(fixture), "--run")
                self.assertEqual(code, 0, stderr or stdout)
                self.assertIn('"status": "passed"', stdout)

    def test_offline_guidance_and_capability_report_keep_local_work_available(self) -> None:
        guidance = (EXAMPLES / "optional-capabilities.md").read_text(encoding="utf-8").lower()
        for word in ("github", "jira", "research", "scheduling", "local_work_can_continue"):
            self.assertIn(word, guidance)
        self.assertNotIn("http://", guidance)
        self.assertNotIn("https://", guidance)

        code, stdout, stderr = invoke("capabilities", "--root", str(EXAMPLES / "application"))
        self.assertEqual(code, 0, stderr or stdout)
        self.assertIn('"local_work_can_continue": true', stdout)
        self.assertIn('"provider_health_source": "offline-default"', stdout)
        for capability in ("github-cli-adapter", "jira-connector-adapter", "research-adapter", "scheduler-host"):
            self.assertIn(f'"id": "{capability}"', stdout)
        self.assertGreaterEqual(stdout.count('"state": "unavailable"'), 4)

    def test_examples_use_generic_sample_vocabulary(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in EXAMPLES.rglob("*")
            if path.is_file()
        ).lower()
        for word in ("sample", "local", "offline", "fixture"):
            self.assertIn(word, content)

    def test_ci_covers_supported_platforms_versions_and_example_checks(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for value in (
            "ubuntu-latest", "windows-latest", "macos-latest",
            '"3.11"', '"3.12"', '"3.13"',
            "Install Tasktra", "Build wheel", "Run tests",
            "Check generated projections", "Run portable examples",
        ):
            self.assertIn(value, workflow)


if __name__ == "__main__":
    unittest.main()
