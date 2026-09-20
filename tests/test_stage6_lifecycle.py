"""Stage 6 read-only adoption and lifecycle-planning acceptance tests."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.adoption import inspect_existing_instructions
from tasktra.compiler import compile_catalog, load_catalog
from tasktra.lifecycle import preview_adoption, preview_upgrade
from tasktra.manifest import TasktraLock, build_generated_manifest, write_manifest
from tasktra.state import StateStore


ROOT = Path(__file__).resolve().parents[1]


class LifecyclePlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "catalog")

    def test_instruction_inspection_is_bounded_metadata_only_and_reports_uncertainty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("first", encoding="utf-8")
            (root / "CLAUDE.md").write_text("second", encoding="utf-8")
            report = inspect_existing_instructions(root, max_files=1)
            self.assertEqual([item.path for item in report.instructions], ["AGENTS.md"])
            self.assertTrue(any("file limit" in item for item in report.uncertain))
            payload = report.as_dict()
            self.assertEqual(payload["semantic_equivalence"], "not-assessed")
            self.assertNotIn("first", str(payload))

    def test_project_validation_plan_can_replace_pack_defaults(self):
        plan = preview_upgrade(ROOT, self.catalog).as_dict()
        self.assertNotIn(
            ["python", "-m", "unittest", "discover"],
            plan["validation_commands"],
        )
        self.assertIn(
            ["python", "-m", "tasktra", "compile", "--root", ".", "--check", "--trust-catalog"],
            plan["validation_commands"],
        )

    def test_adoption_preview_preserves_instruction_surfaces_and_reports_conflicts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("project owned", encoding="utf-8")
            for name in (".agents", ".codex", ".claude"):
                path = root / name / "private"
                path.mkdir(parents=True)
                (path / "note.md").write_text(name, encoding="utf-8")
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            plan = preview_adoption(root, self.catalog).as_dict()
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(after, before)
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["instruction_inspection"]["semantic_equivalence"], "not-assessed")
            self.assertEqual({item["path"] for item in plan["project_owned"]}, {
                "AGENTS.md", ".agents/private/note.md", ".codex/private/note.md", ".claude/private/note.md",
            })
            self.assertIn("AGENTS.md", plan["preserved_paths"])
            self.assertTrue(any(item.get("path") == "AGENTS.md" for item in plan["conflicts"]))
            self.assertTrue(any(item["path"] == ".tasktra/tasktra.lock" for item in plan["managed_writes"]))
            self.assertEqual(plan["validation_commands"], [["python", "-m", "tasktra", "compile", "--root", ".", "--check"]])

    def test_upgrade_composes_exact_pack_and_immediately_preceding_runtime_previews(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._runtime_schema(root, 9)
            plan = preview_upgrade(root, self.catalog, current_lock=self._lock("0.6.0", runtime=9)).as_dict()
            self.assertTrue(plan["ok"])
            self.assertEqual([item["order"] for item in plan["migration_steps"]], [1, 2])
            self.assertEqual([item["kind"] for item in plan["migration_steps"]], ["runtime-schema", "pack"])
            self.assertTrue(all(item["execution"] == "preview-only" for item in plan["migration_steps"]))
            self.assertFalse(plan["authority_scope"]["preview_executes"])
            self.assertTrue(plan["authority_scope"]["rollback_evidence_required"])
            self.assertEqual(plan["authority_scope"]["required_action"], "local-effect")

    def test_upgrade_blocks_when_runtime_state_does_not_match_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            StateStore(root / ".tasktra/runtime/tasktra.sqlite").migrate()

            plan = preview_upgrade(root, self.catalog, current_lock=self._lock("0.6.0", runtime=9)).as_dict()

            self.assertFalse(plan["ok"])
            self.assertTrue(any(
                item.get("component") == "runtime-state"
                and item.get("lock_schema") == 9
                and item.get("on_disk_schema") == 10
                for item in plan["conflicts"]
            ))

    def test_upgrade_preview_blocks_an_external_runtime_database_without_mutating_it(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "project"
            root.mkdir()
            external = parent / "external.sqlite"
            self._runtime_schema_7(parent)
            produced = parent / ".tasktra/runtime/tasktra.sqlite"
            produced.replace(external)
            config = root / ".tasktra/project.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                f'[project]\nname = "escape"\nconfig_version = 1\n\n[runtime]\ndatabase = "{external.as_posix()}"\n',
                encoding="utf-8",
            )

            plan = preview_upgrade(root, self.catalog, current_lock=self._lock("0.5.0", runtime=7)).as_dict()

            self.assertFalse(plan["ok"])
            self.assertTrue(any("inside the project root" in item["reason"] for item in plan["conflicts"]))
            self.assertEqual(StateStore(external).inspect_schema_version(), 7)
            self.assertEqual(list(parent.glob("external.sqlite.v7*.bak")), [])

    def test_upgrade_supports_the_declared_compound_runtime_edge(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            StateStore(root / ".tasktra/runtime/tasktra.sqlite").migrate()
            connection = sqlite3.connect(root / ".tasktra/runtime/tasktra.sqlite")
            try:
                connection.execute("PRAGMA user_version = 8")
                connection.commit()
            finally:
                connection.close()
            lock = self._lock("1.0.0", runtime=8)
            lock = TasktraLock(
                tasktra_version="1.0.0", catalog_version="1.0.0", packs=lock.packs,
                pack_versions=lock.pack_versions, generated_manifest_sha256=lock.generated_manifest_sha256,
                catalog_source_sha256=lock.catalog_source_sha256, schema_versions=lock.schema_versions,
                pack_contracts=lock.pack_contracts,
            )

            plan = preview_upgrade(root, self.catalog, current_lock=lock).as_dict()

            self.assertTrue(plan["ok"], plan["conflicts"])
            edge = next(item for item in plan["compatibility"] if item["component"] == "runtime-schema")
            self.assertEqual((edge["from"], edge["to"]), (8, 10))
            self.assertIn("compound", edge["reason"])

    def test_upgrade_fails_visibly_for_unsupported_pack_runtime_and_major_edges(self):
        with TemporaryDirectory() as directory:
            plan = preview_upgrade(
                Path(directory), self.catalog, current_lock=self._lock("0.3.0", runtime=6), tasktra_version="1.0.0",
            ).as_dict()
            self.assertFalse(plan["ok"])
            reasons = "\n".join(item["reason"] for item in plan["conflicts"])
            self.assertIn("major-version", reasons)
            self.assertIn("exact migration declaration", reasons)
            self.assertIn("immediately preceding", reasons)

    def test_same_version_checksum_change_is_not_silently_accepted(self):
        with TemporaryDirectory() as directory:
            lock = self._lock("1.0.0", runtime=10, digest="0" * 64)
            plan = preview_upgrade(Path(directory), self.catalog, current_lock=lock).as_dict()
            self.assertFalse(plan["ok"])
            self.assertTrue(any(item.get("component") == "pack-contract" for item in plan["conflicts"]))

    def test_upgrade_rejects_a_manifest_not_bound_to_the_installed_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            projection = compile_catalog(self.catalog, ("core",))
            manifest = build_generated_manifest(
                projection.files,
                tasktra_version="0.5.0",
                catalog_version="0.5.0",
                packs=("core",),
            )
            write_manifest(root, manifest)
            lock = self._lock("0.5.0", runtime=7)

            plan = preview_upgrade(root, self.catalog, current_lock=lock).as_dict()

            self.assertFalse(plan["ok"])
            self.assertTrue(any(
                item.get("component") == "generated-manifest" and "lock binding" in item["reason"]
                for item in plan["conflicts"]
            ))

    def _lock(self, core_version: str, *, runtime: int, digest: str | None = None) -> TasktraLock:
        core = self.catalog.packs["core"]
        return TasktraLock(
            tasktra_version=core_version, catalog_version=core_version, packs=("core",),
            pack_versions=(("core", core_version),), generated_manifest_sha256="a" * 64,
            catalog_source_sha256="b" * 64, schema_versions=(("runtime", runtime),),
            pack_contracts=(("core", core_version, core.contract_version, core.trust, digest or core.source_sha256),),
        )

    @staticmethod
    def _runtime_schema(root: Path, version: int) -> None:
        path = root / ".tasktra/runtime/tasktra.sqlite"
        StateStore(path).migrate()
        connection = sqlite3.connect(path)
        try:
            connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _runtime_schema_7(root: Path) -> None:
        LifecyclePlanningTests._runtime_schema(root, 7)


if __name__ == "__main__":
    unittest.main()
