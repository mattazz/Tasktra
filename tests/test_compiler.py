import tempfile
import unittest
from pathlib import Path, PurePosixPath
import os
import shutil
import subprocess

from tasktra.compiler import (
    Catalog,
    CatalogError,
    Pack,
    Projection,
    ProjectionError,
    check_drift,
    compile_catalog,
    load_catalog,
    resolve_packs,
    write_projection,
)


ROOT = Path(__file__).resolve().parents[1]


class CompilerTests(unittest.TestCase):
    def test_core_compiles_native_runtime_projections(self):
        catalog = load_catalog(ROOT / "catalog")
        projection = compile_catalog(catalog)
        self.assertEqual(projection.packs, ("core",))
        self.assertIn(PurePosixPath(".codex/agents/scout.toml"), projection.files)
        self.assertIn(PurePosixPath(".claude/agents/scout.md"), projection.files)
        skill = projection.files[PurePosixPath(".codex/skills/tasktra-init/SKILL.md")]
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("\nname: tasktra-init\n", skill)
        self.assertIn("\ndescription: ", skill)
        codex_agent = projection.files[PurePosixPath(".codex/agents/scout.toml")]
        self.assertIn('name = "scout"', codex_agent)
        self.assertGreaterEqual(len(catalog.roles), 30)

    def test_stage_two_operational_skills_compile_with_executable_procedures(self):
        projection = compile_catalog(load_catalog(ROOT / "catalog"))
        expected_commands = {
            "tasktra-handoff": "tasktra handoff validate",
            "tasktra-local-work": "tasktra work-item",
            "tasktra-remote": "tasktra effect --root <root> provider-prepare",
            "tasktra-validate": "tasktra validate",
        }
        for runtime in (".agents", ".codex", ".claude"):
            for skill_id, command in expected_commands.items():
                content = projection.files[
                    PurePosixPath(f"{runtime}/skills/{skill_id}/SKILL.md")
                ]
                self.assertIn("## Procedure", content)
                self.assertIn(command, content)

    def test_projection_is_deterministic_and_drift_is_visible(self):
        projection = compile_catalog(load_catalog(ROOT / "catalog"))
        self.assertEqual(projection, compile_catalog(load_catalog(ROOT / "catalog")))
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_projection(project, projection)
            self.assertTrue(check_drift(project, projection).clean)
            changed = project / ".codex" / "agents" / "scout.toml"
            changed.write_text("changed\n", encoding="utf-8")
            self.assertIn(PurePosixPath(".codex/agents/scout.toml"), check_drift(project, projection).changed)

    def test_pack_conflicts_fail(self):
        catalog = load_catalog(ROOT / "catalog")
        packs = dict(catalog.packs)
        packs["one"] = Pack("one", "1.0.0", ("core",), ("two",), (), ())
        packs["two"] = Pack("two", "1.0.0", ("core",), (), (), ())
        conflicting = Catalog(catalog.version, catalog.roles, catalog.skills, packs)
        with self.assertRaisesRegex(CatalogError, "incompatible packs"):
            resolve_packs(conflicting, ("one", "two"))

    def test_representative_codex_and_claude_outputs_match_golden_files(self):
        projection = compile_catalog(load_catalog(ROOT / "catalog"))
        golden = ROOT / "tests" / "golden"
        expected = {
            PurePosixPath(".codex/agents/scout.toml"): "codex-scout.toml",
            PurePosixPath(".claude/agents/scout.md"): "claude-scout.md",
            PurePosixPath(".codex/skills/tasktra-init/SKILL.md"): "tasktra-init-skill.md",
            PurePosixPath(".claude/skills/tasktra-init/SKILL.md"): "tasktra-init-skill.md",
        }
        for path, fixture in expected.items():
            self.assertEqual(projection.files[path], (golden / fixture).read_text(encoding="utf-8"))

    def test_all_destinations_are_validated_before_any_file_is_written(self):
        projection = Projection(
            {
                PurePosixPath(".codex/agents/safe.toml"): "safe\n",
                PurePosixPath("../outside.txt"): "unsafe\n",
            },
            ("core",),
        )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            with self.assertRaises(ProjectionError):
                write_projection(project, projection)
            self.assertFalse((project / ".codex/agents/safe.toml").exists())
            self.assertFalse((project.parent / "outside.txt").exists())

    def test_projection_rejects_symlinked_runtime_root(self):
        projection = Projection(
            {PurePosixPath(".codex/agents/safe.toml"): "safe\n"},
            ("core",),
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, outside = base / "project", base / "outside"
            project.mkdir()
            outside.mkdir()
            try:
                (project / ".codex").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with self.assertRaisesRegex(ProjectionError, "symlink"):
                write_projection(project, projection)
            self.assertFalse((outside / "agents/safe.toml").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_projection_rejects_junctioned_generated_directory(self):
        projection = Projection(
            {PurePosixPath(".tasktra/generated/pack-plan.json"): "{}\n"},
            ("core",),
        )
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            project = Path(directory)
            outside = Path(outside_directory)
            (project / ".tasktra").mkdir()
            junction = project / ".tasktra" / "generated"
            command = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
            with self.assertRaisesRegex(ProjectionError, "reparse point"):
                write_projection(project, projection)
            self.assertFalse((outside / "pack-plan.json").exists())

    def test_catalog_rejects_identifier_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog"
            shutil.copytree(ROOT / "catalog", catalog)
            source = catalog / "roles" / "scout.md"
            source.write_text(
                source.read_text(encoding="utf-8").replace('id = "scout"', 'id = "../../outside"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "id must use"):
                load_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
