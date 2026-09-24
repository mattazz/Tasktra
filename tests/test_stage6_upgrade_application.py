"""Stage 6 acceptance tests for applying an exact lifecycle upgrade plan."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

from tasktra import __version__
from tasktra.compiler import compile_catalog, load_catalog, write_projection
from tasktra.config import ProjectConfig, ProjectRoute
from tasktra.delegation import projection_overrides
from tasktra.lifecycle import preview_upgrade
from tasktra.manifest import (
    build_generated_manifest,
    build_lockfile,
    read_lockfile,
    read_manifest,
    write_lockfile,
    write_manifest,
)
from tasktra.migrations import MigrationError
from tasktra.state import SCHEMA_VERSION, StateStore
from tasktra.upgrades import UpgradeError, apply_upgrade, rollback_upgrade, upgrade_plan_digest


ROOT = Path(__file__).resolve().parents[1]


class UpgradeApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog_root = ROOT / "catalog"
        cls.catalog = load_catalog(cls.catalog_root)

    def _project(self, root: Path, *, include_stale: bool = False) -> tuple[Path, ProjectConfig, dict[str, object]]:
        project = root / "project"
        project.mkdir()
        config = ProjectConfig(
            name="upgrade fixture",
            enabled_packs=("core",),
            validation_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
        )
        (project / "PROJECT.md").write_text("project-owned\n", encoding="utf-8")

        # A valid old projection/lock makes generated state distinct from the
        # project-owned file and gives rollback concrete before-state evidence.
        projection = compile_catalog(self.catalog, ("core",))
        write_projection(project, projection)
        installed_files = dict(projection.files)
        if include_stale:
            installed_files[PurePosixPath("legacy-managed.txt")] = "obsolete generated content\n"
            (project / "legacy-managed.txt").write_bytes(b"obsolete generated content\n")
        manifest = build_generated_manifest(
            installed_files,
            tasktra_version="0.6.0",
            catalog_version="0.6.0",
            packs=("core",),
        )
        core = self.catalog.packs["core"]
        lock = build_lockfile(
            manifest,
            catalog_source_sha256="a" * 64,
            pack_versions={"core": "0.6.0"},
            pack_contracts={
                "core": {
                    "version": "0.6.0",
                    "contract_version": core.contract_version,
                    "trust": core.trust,
                    "sha256": core.source_sha256,
                }
            },
            schema_versions={"runtime": 8},
        )
        write_manifest(project, manifest)
        write_lockfile(project, lock)
        self._runtime_schema_8(config.database_path(project))

        plan = preview_upgrade(
            project,
            self.catalog,
            current_lock=lock,
            validation_commands=config.validation_commands,
        ).as_dict()
        self.assertTrue(plan["ok"], plan["conflicts"])
        return project, config, plan

    @staticmethod
    def _runtime_schema_8(path: Path) -> None:
        StateStore(path).migrate()
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA user_version = 8")
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _bytes(project: Path, relative: str) -> bytes:
        return (project / Path(relative)).read_bytes()

    def _apply(self, project: Path, config: ProjectConfig, plan: dict[str, object]) -> dict[str, object]:
        return apply_upgrade(
            project,
            self.catalog_root,
            self.catalog,
            config,
            plan,
            expected_plan_sha256=upgrade_plan_digest(plan),
            confirmed=True,
        )

    def test_apply_rejects_route_changes_after_preview_without_writing(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            before = {
                path: self._bytes(project, path)
                for path in ("AGENTS.md", "CLAUDE.md", ".tasktra/generated/manifest.json", ".tasktra/tasktra.lock")
            }
            changed_config = replace(config, routes=(
                ProjectRoute("art", "Create artwork", skills=("imagegen",)),
            ))
            with self.assertRaisesRegex(UpgradeError, "projection changed after the upgrade preview"):
                self._apply(project, changed_config, plan)
            self.assertEqual(
                before,
                {path: self._bytes(project, path) for path in before},
            )

    def test_apply_requires_the_exact_lifecycle_plan_digest_before_writing(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            before = self._bytes(project, ".tasktra/tasktra.lock")
            original_digest = upgrade_plan_digest(plan)
            changed = deepcopy(plan)
            changed["target"]["catalog_version"] = "0.5.1"  # type: ignore[index]

            with self.assertRaisesRegex(UpgradeError, "digest changed"):
                apply_upgrade(
                    project, self.catalog_root, self.catalog, config, changed,
                    expected_plan_sha256=original_digest, confirmed=True,
                )

            self.assertEqual(self._bytes(project, ".tasktra/tasktra.lock"), before)

    def test_apply_rechecks_manifest_lock_binding_before_ownership_writes(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            projection = compile_catalog(self.catalog, ("core",))
            forged = build_generated_manifest(
                projection.files,
                tasktra_version="9.9.9",
                catalog_version=self.catalog.version,
                packs=("core",),
            )
            write_manifest(project, forged)

            with self.assertRaisesRegex(UpgradeError, "lock binding"):
                self._apply(project, config, plan)

    def test_success_writes_canonical_projection_and_lock_without_touching_project_files(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            result = self._apply(project, config, plan)

            projection = compile_catalog(self.catalog, ("core",))
            self.assertTrue(result["ok"])
            self.assertEqual((project / "PROJECT.md").read_text(encoding="utf-8"), "project-owned\n")
            self.assertEqual(read_manifest(project).files, build_generated_manifest(
                projection.files,
                tasktra_version=__version__,
                catalog_version=self.catalog.version,
                packs=("core",),
            ).files)
            self.assertEqual(read_lockfile(project).pack_versions, (("core", self.catalog.packs["core"].version),))
            self.assertEqual(read_lockfile(project).generated_manifest_sha256, read_manifest(project).digest)

    def test_apply_derives_project_model_overrides_from_config(self):
        with TemporaryDirectory() as directory:
            project, base_config, _ = self._project(Path(directory))
            config = ProjectConfig(
                name=base_config.name,
                enabled_packs=base_config.enabled_packs,
                validation_commands=base_config.validation_commands,
                codex_tier_models={"fast": "project-fast"},
                codex_role_overrides={
                    "scout": {"model": "inherit", "reasoning_effort": "inherit"}
                },
            )
            policy, overrides = projection_overrides(self.catalog, config)
            plan = preview_upgrade(
                project,
                self.catalog,
                current_lock=read_lockfile(project),
                validation_commands=config.validation_commands,
                codex_model_policy=policy,
                codex_role_overrides=overrides,
            ).as_dict()
            self.assertTrue(plan["ok"], plan["conflicts"])

            self._apply(project, config, plan)

            scout = (project / ".codex/agents/scout.toml").read_text(encoding="utf-8")
            self.assertNotIn("model =", scout)
            self.assertNotIn("model_reasoning_effort", scout)

    def test_validation_failure_restores_generated_and_runtime_state(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            plan["validation_commands"] = [[sys.executable, "-c", "raise SystemExit(7)"]]
            tracked = (
                ".tasktra/generated/manifest.json",
                ".tasktra/tasktra.lock",
                ".tasktra/runtime/tasktra.sqlite",
            )
            before = {path: self._bytes(project, path) for path in tracked}

            with self.assertRaisesRegex(MigrationError, "configured validation failed"):
                self._apply(project, config, plan)

            self.assertEqual({path: self._bytes(project, path) for path in tracked}, before)

    def test_runtime_migration_receipts_exact_backup_and_disables_automatic_rollback(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            database = config.database_path(project)
            default_backup = database.with_name(f"{database.name}.v8.bak")
            default_backup.write_bytes(b"pre-existing backup")
            result = self._apply(project, config, plan)
            migration = result["migration"]
            self.assertIsInstance(migration, dict)
            migration = dict(migration)
            verification = dict(migration["verification"])
            backup = Path(str(verification["runtime_backup_path"]))

            self.assertFalse(migration["rollback_available"])
            self.assertNotEqual(backup, default_backup)
            self.assertTrue(backup.is_file())
            self.assertEqual(verification["runtime_backup_sha256"], sha256(backup.read_bytes()).hexdigest())
            receipt = json.loads((Path(str(migration["snapshot"])).parent / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["rollback_available"])
            self.assertEqual(receipt["verification"]["runtime_backup_path"], str(backup))

            with self.assertRaisesRegex(UpgradeError, str(backup).replace("\\", "\\\\")):
                rollback_upgrade(
                    project,
                    str(migration["plan_sha256"]),
                    expected_before_sha256=str(migration["before_sha256"]),
                )

    def test_apply_rejects_runtime_schema_changed_after_preview(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            connection = sqlite3.connect(config.database_path(project))
            try:
                connection.execute("PRAGMA user_version = 9")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(UpgradeError, "does not match the installed lock"):
                self._apply(project, config, plan)

    def test_apply_rejects_external_runtime_database_without_mutating_or_backing_it_up(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            project, config, plan = self._project(parent)
            external = parent / "external.sqlite"
            self._runtime_schema_8(external)
            escaped = ProjectConfig(
                name=config.name,
                database=str(external),
                enabled_packs=config.enabled_packs,
                validation_commands=config.validation_commands,
            )

            with self.assertRaisesRegex(UpgradeError, "outside the project authority scope"):
                self._apply(project, escaped, plan)

            self.assertEqual(StateStore(external).inspect_schema_version(), 8)
            self.assertEqual(list(parent.glob("external.sqlite.v8*.bak")), [])

    def test_stale_managed_deletion_is_previewed_bound_and_applied(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory), include_stale=True)
            deletion = next(item for item in plan["managed_writes"] if item["path"] == "legacy-managed.txt")
            self.assertEqual(deletion["action"], "delete")
            self.assertEqual(deletion["sha256"], sha256(b"obsolete generated content\n").hexdigest())

            result = self._apply(project, config, plan)

            self.assertFalse((project / "legacy-managed.txt").exists())
            snapshot = Path(str(dict(result["migration"])["snapshot"]))
            self.assertIn("legacy-managed.txt", json.loads(snapshot.read_text(encoding="utf-8"))["write_paths"])

    def test_apply_refuses_a_stale_deletion_omitted_from_the_preview(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory), include_stale=True)
            plan["managed_writes"] = [
                item for item in plan["managed_writes"] if item["path"] != "legacy-managed.txt"
            ]

            with self.assertRaisesRegex(MigrationError, "deletion was not declared"):
                self._apply(project, config, plan)

    def test_snapshot_paths_cover_generated_runtime_writes_and_reject_unsafe_entries(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            result = self._apply(project, config, plan)
            migration = dict(result["migration"])
            snapshot = Path(str(migration["snapshot"]))
            write_paths = set(json.loads(snapshot.read_text(encoding="utf-8"))["write_paths"])
            self.assertTrue({
                ".tasktra/generated/manifest.json",
                ".tasktra/tasktra.lock",
            }.issubset(write_paths))
            self.assertNotIn(".tasktra/runtime/tasktra.sqlite", write_paths)

        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            plan["managed_writes"].append({"path": "../outside", "action": "update"})  # type: ignore[index]
            with self.assertRaisesRegex(MigrationError, "unsafe relative POSIX path"):
                self._apply(project, config, plan)


if __name__ == "__main__":
    unittest.main()
