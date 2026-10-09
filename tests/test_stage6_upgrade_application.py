"""Stage 6 acceptance tests for applying an exact lifecycle upgrade plan."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
from tasktra.state import SCHEMA_VERSION, StateStore, _now
from tests.runtime_schema_helpers import peel_schema13_interventions
from tasktra.migrations import MigrationError, write_prepared_runtime_recovery
from tasktra.migrations import CommittedRuntimeRecoveryRequired
from tasktra.state import SCHEMA_VERSION, StateStore, _now
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
            schema_versions={"runtime": SCHEMA_VERSION - 1},
        )
        write_manifest(project, manifest)
        write_lockfile(project, lock)
        self._runtime_schema_previous(config.database_path(project))

        plan = preview_upgrade(
            project,
            self.catalog,
            current_lock=lock,
            validation_commands=config.validation_commands,
        ).as_dict()
        self.assertTrue(plan["ok"], plan["conflicts"])
        return project, config, plan

    @staticmethod
    def _runtime_schema_previous(path: Path) -> None:
        StateStore(path).migrate()
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            if SCHEMA_VERSION - 1 < 11:
                connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")
            peel_schema13_interventions(connection, target_version=SCHEMA_VERSION - 1)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
            StateStore._seal_current_state_in_transaction(connection, _now(), existing_only=True)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _populated_schema_10(path: Path) -> StateStore:
        """Build a sealed, populated v10-shaped ledger without inventing trust."""
        store = StateStore(path)
        store.create_goal(goal_id="legacy-goal", title="Legacy", description="Legacy", acceptance=["Done."])
        store.create_work_unit(goal_id="legacy-goal", work_unit_id="legacy-unit", title="Legacy unit")
        connection = sqlite3.connect(path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE work_units DROP COLUMN verification_policy")
            connection.execute("ALTER TABLE work_units DROP COLUMN acceptance_checks")
            connection.execute("ALTER TABLE workflow_evidence DROP COLUMN completion_evidence_json")
            connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")
            peel_schema13_interventions(connection, target_version=10)
            # The historical v10 seal hashes cover the historical row shape.
            # Re-seal it here only to model a legitimate previously committed
            # v10 ledger; the tamper case below deliberately does not do so.
            StateStore._seal_current_state_in_transaction(
                connection, "2035-01-01T00:00:00Z", existing_only=True,
            )
            connection.execute("PRAGMA user_version = 10")
            connection.commit()
        finally:
            connection.close()
        return store

    def test_populated_sealed_v10_migrates_to_policy_default_with_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite"
            store = self._populated_schema_10(path)
            evidence = store.migrate_with_evidence()
            self.assertEqual((evidence["before_schema"], evidence["after_schema"]), (10, SCHEMA_VERSION))
            backup = Path(str(evidence["backup_path"]))
            self.assertTrue(backup.is_file())
            self.assertEqual(evidence["backup_sha256"], sha256(backup.read_bytes()).hexdigest())
            self.assertEqual(store.get_work_unit("legacy-unit")["verification_policy"], "implementation-review")
            self.assertTrue(store.verify_audit()["ok"])

    def test_tampered_v10_ledger_is_not_auto_attested_during_policy_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite"
            store = self._populated_schema_10(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE work_units SET title='tampered' WHERE id='legacy-unit'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(Exception, "unsealed or tampered"):
                store.migrate_with_evidence()
            connection = sqlite3.connect(path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(work_units)")}
                self.assertNotIn("verification_policy", columns)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
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
            runtime_step = next(
                item for item in plan["migration_steps"] if item["kind"] == "runtime-schema"
            )
            self.assertEqual((runtime_step["from"], runtime_step["to"]), (SCHEMA_VERSION - 1, SCHEMA_VERSION))
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
            self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION)
            connection = sqlite3.connect(config.database_path(project))
            try:
                self.assertEqual(connection.execute("SELECT count(*) FROM work_unit_dependencies").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()

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

    def test_target_lock_is_validated_before_pre15_database_commit(self):
        for fails in (False, True):
            with self.subTest(validation_fails=fails), TemporaryDirectory() as directory:
                project, config, _ = self._project(Path(directory))
                database = config.database_path(project)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
                    StateStore._seal_current_state_in_transaction(connection, _now(), existing_only=True)
                lock = replace(read_lockfile(project), schema_versions=(("runtime", SCHEMA_VERSION - 1),))
                write_lockfile(project, lock)
                check = (
                    "import json,sqlite3;from pathlib import Path;"
                    f"assert json.loads(Path('.tasktra/tasktra.lock').read_text())['schema_versions']['runtime']=={SCHEMA_VERSION};"
                    "c=sqlite3.connect('file:.tasktra/runtime/tasktra.sqlite?mode=ro',uri=True);"
                    f"assert c.execute('PRAGMA user_version').fetchone()[0]=={SCHEMA_VERSION - 1};c.close();"
                    + ("raise SystemExit(7)" if fails else "")
                )
                config = replace(config, validation_commands=((sys.executable, "-c", check),))
                plan = preview_upgrade(project, self.catalog, current_lock=lock,
                                       validation_commands=config.validation_commands).as_dict()
                before = {path: self._bytes(project, path) for path in
                          (".tasktra/tasktra.lock", ".tasktra/runtime/tasktra.sqlite")}
                if fails:
                    with self.assertRaisesRegex(MigrationError, "configured validation failed"):
                        self._apply(project, config, plan)
                    self.assertEqual({path: self._bytes(project, path) for path in before}, before)
                else:
                    result = self._apply(project, config, plan)
                    self.assertEqual(dict(read_lockfile(project).schema_versions)["runtime"], SCHEMA_VERSION)
                    self.assertEqual(StateStore(database).inspect_schema_version(), SCHEMA_VERSION)
                    self.assertFalse(result["migration"]["rollback_available"])

    def test_runtime_migration_receipts_exact_backup_and_disables_automatic_rollback(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            database = config.database_path(project)
            default_backup = database.with_name(f"{database.name}.v{SCHEMA_VERSION - 1}.bak")
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

    def test_post_commit_canonical_failure_requires_recovery_and_preserves_receipt(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            before_manifest = self._bytes(project, ".tasktra/generated/manifest.json")
            import tasktra.upgrades as upgrades

            original = upgrades._write_canonical_projection
            calls = 0

            def fail_second_projection(*args: object, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("forced finalization failure")
                original(*args, **kwargs)

            with patch("tasktra.upgrades._write_canonical_projection", side_effect=fail_second_projection):
                with self.assertRaisesRegex(MigrationError, "recovery is required"):
                    self._apply(project, config, plan)

            self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION)
            self.assertNotEqual(self._bytes(project, ".tasktra/generated/manifest.json"), before_manifest)
            receipts = list((project / ".tasktra" / "upgrades").glob("*/receipt.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            verification = receipt["verification"]
            backup = Path(str(verification["runtime_backup_path"]))
            self.assertTrue(receipt["recovery_required"])
            self.assertFalse(receipt["rollback_available"])
            self.assertTrue(backup.is_file())
            self.assertEqual(verification["runtime_backup_sha256"], sha256(backup.read_bytes()).hexdigest())

    def test_post_commit_drift_failures_and_interrupts_require_recovery(self):
        for failure in (OSError("forced drift failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), TemporaryDirectory() as directory:
                project, config, plan = self._project(Path(directory))
                with patch("tasktra.upgrades.check_drift", side_effect=failure):
                    with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                        self._apply(project, config, plan)
                self.assertTrue(raised.exception.verification["runtime_schema_changed"])
                self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION)
                receipt = next((project / ".tasktra" / "upgrades").glob("*/receipt.json"))
                self.assertTrue(json.loads(receipt.read_text(encoding="utf-8"))["recovery_required"])

    def test_commit_return_interrupt_uses_prepared_backup_evidence_without_file_rollback(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            original = StateStore.migrate_with_evidence

            def commit_then_interrupt(store: StateStore, *args: object, **kwargs: object) -> dict[str, object]:
                original(store, *args, **kwargs)
                raise KeyboardInterrupt()

            with patch.object(StateStore, "migrate_with_evidence", new=commit_then_interrupt):
                with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                    self._apply(project, config, plan)

            evidence = raised.exception.verification
            backup = Path(str(evidence["runtime_backup_path"]))
            self.assertTrue(backup.is_file())
            self.assertEqual(evidence["runtime_backup_sha256"], sha256(backup.read_bytes()).hexdigest())
            prepared = next((project / ".tasktra" / "upgrades").glob("*/prepared-*.json"))
            journal = json.loads(prepared.read_text(encoding="utf-8"))
            self.assertEqual(journal["phase"], "prepared")
            self.assertEqual(journal["runtime_schema_before"], SCHEMA_VERSION - 1)
            self.assertEqual(journal["runtime_schema_target"], SCHEMA_VERSION)
            self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION)

    def test_prepared_journal_failure_aborts_runtime_commit(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            with patch("tasktra.upgrades.write_prepared_runtime_recovery", side_effect=OSError("journal unavailable")):
                with self.assertRaisesRegex(MigrationError, "rollback completed"):
                    self._apply(project, config, plan)
            self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION - 1)

    def test_concurrent_runtime_advance_uses_preview_schema_as_the_recovery_baseline(self):
        with TemporaryDirectory() as directory:
            project, config, plan = self._project(Path(directory))
            before_lock = self._bytes(project, ".tasktra/tasktra.lock")
            original = StateStore.migrate_with_evidence
            advanced = False

            def advance_then_retry(store: StateStore, *args: object, **kwargs: object) -> dict[str, object]:
                nonlocal advanced
                if not advanced:
                    advanced = True
                    original(store)
                return original(store, *args, **kwargs)

            with patch.object(StateStore, "migrate_with_evidence", new=advance_then_retry):
                with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                    self._apply(project, config, plan)

            evidence = raised.exception.verification
            self.assertEqual(evidence["runtime_schema_before"], SCHEMA_VERSION - 1)
            self.assertEqual(evidence["runtime_schema_observed"], SCHEMA_VERSION)
            self.assertIsNone(evidence["runtime_backup_path"])
            self.assertEqual(StateStore(config.database_path(project)).inspect_schema_version(), SCHEMA_VERSION)
            self.assertNotEqual(self._bytes(project, ".tasktra/tasktra.lock"), before_lock)

    def test_prepared_journal_and_database_links_are_rejected_before_reading(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            verification = {
                "runtime_schema_before": 8,
                "runtime_schema_after": SCHEMA_VERSION,
                "runtime_schema_changed": True,
                "runtime_backup_path": str(project / ".tasktra/runtime/backup.sqlite"),
                "runtime_backup_sha256": "a" * 64,
            }
            import tasktra.upgrades as upgrades

            def linked(link: Path, target: Path) -> None:
                if os.name == "nt":
                    command = subprocess.run(
                        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                        capture_output=True, text=True, check=False,
                    )
                    if command.returncode != 0:
                        raise OSError(command.stderr or command.stdout)
                else:
                    link.symlink_to(target, target_is_directory=True)

            try:
                journal_plan = "b" * 64
                journal = write_prepared_runtime_recovery(
                    project, migration_plan_sha256=journal_plan, upgrade_plan_sha256="c" * 64,
                    attempt_id="d" * 32, database_path=str(project / ".tasktra/runtime/tasktra.sqlite"),
                    verification=verification,
                )
                outside = project.parent / "outside-journal"
                outside.mkdir()
                shutil.rmtree(journal.parent)
                linked(journal.parent, outside)
                with self.assertRaisesRegex(UpgradeError, "unresolved"):
                    upgrades._assert_prepared_recovery_is_uncommitted(project, journal_plan)
                self.assertFalse(any(outside.iterdir()))
                journal.parent.unlink()

                database_plan = "e" * 64
                write_prepared_runtime_recovery(
                    project, migration_plan_sha256=database_plan, upgrade_plan_sha256="f" * 64,
                    attempt_id="1" * 32, database_path=str(project / ".tasktra/link-runtime/tasktra.sqlite"),
                    verification=verification,
                )
                outside_database = project.parent / "outside-database"
                outside_database.mkdir()
                linked(project / ".tasktra/link-runtime", outside_database)
                with self.assertRaisesRegex(UpgradeError, "unresolved"):
                    upgrades._assert_prepared_recovery_is_uncommitted(project, database_plan)
            except OSError as error:
                self.skipTest(f"link or junction creation is unavailable: {error}")
            finally:
                for path in (project / ".tasktra/upgrades" / ("b" * 64), project / ".tasktra/link-runtime"):
                    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                        path.unlink()

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
            self._runtime_schema_previous(external)
            escaped = ProjectConfig(
                name=config.name,
                database=str(external),
                enabled_packs=config.enabled_packs,
                validation_commands=config.validation_commands,
            )

            with self.assertRaisesRegex(UpgradeError, "outside the project authority scope"):
                self._apply(project, escaped, plan)

            self.assertEqual(StateStore(external).inspect_schema_version(), SCHEMA_VERSION - 1)
            self.assertEqual(list(parent.glob(f"external.sqlite.v{SCHEMA_VERSION - 1}*.bak")), [])

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
