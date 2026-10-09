"""Bootstrap keeps runtime-derived state and installed metadata consistent."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import _bootstrap
from tasktra.config import initialize_project
from tasktra.manifest import ManifestError, TasktraLock, read_lockfile, write_lockfile
from tasktra.state import SCHEMA_VERSION, StateError, StateStore
from tests.runtime_schema_helpers import peel_schema13_interventions


class BootstrapLockTests(unittest.TestCase):
    @staticmethod
    def _lock(runtime: int = 10) -> TasktraLock:
        return TasktraLock(
            tasktra_version="1.0.0",
            catalog_version="1.0.0",
            packs=("core",),
            pack_versions=(("core", "1.0.0"),),
            generated_manifest_sha256="a" * 64,
            catalog_source_sha256="b" * 64,
            schema_versions=(("runtime", runtime),),
            pack_contracts=(("core", "1.0.0", 1, "builtin-data-only", "c" * 64),),
        )

    @staticmethod
    def _args(root: Path) -> argparse.Namespace:
        return argparse.Namespace(root=str(root), name=None)

    def _configured_root(self, parent: Path, *, runtime: int = 10) -> Path:
        root = parent / "project"
        root.mkdir()
        initialize_project(root, name="Bootstrap fixture")
        write_lockfile(root, self._lock(runtime))
        return root

    def test_missing_runtime_rebinds_only_an_existing_lock_runtime_field(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._configured_root(Path(directory))
            profile = root / ".tasktra/project.toml"
            lock_path = root / ".tasktra/tasktra.lock"
            manifest_path = root / ".tasktra/generated/manifest.json"
            generated_path = root / "generated-output.txt"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(b"preserved manifest bytes\n")
            generated_path.write_bytes(b"preserved generated output\n")
            profile_before = profile.read_bytes()
            lock_before = lock_path.read_bytes()
            projection_before = (manifest_path.read_bytes(), generated_path.read_bytes())

            result = _bootstrap(self._args(root))

            self.assertEqual(result["runtime_schema"], SCHEMA_VERSION)
            self.assertEqual(profile.read_bytes(), profile_before)
            self.assertEqual((manifest_path.read_bytes(), generated_path.read_bytes()), projection_before)
            self.assertEqual(
                lock_path.read_bytes(),
                lock_before.replace(b'"runtime":10', f'"runtime":{SCHEMA_VERSION}'.encode("ascii")),
            )
            self.assertEqual(dict(read_lockfile(root).schema_versions)["runtime"], SCHEMA_VERSION)
            self.assertEqual(StateStore(root / ".tasktra/runtime/tasktra.sqlite").inspect_schema_version(), SCHEMA_VERSION)

    def test_invalid_lock_is_rejected_before_profile_or_runtime_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            lock_path = root / ".tasktra/tasktra.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("not a lockfile\n", encoding="utf-8")
            before = lock_path.read_bytes()

            with self.assertRaises(ManifestError):
                _bootstrap(self._args(root))

            self.assertEqual(lock_path.read_bytes(), before)
            self.assertFalse((root / ".tasktra/project.toml").exists())
            self.assertFalse((root / ".tasktra/runtime/tasktra.sqlite").exists())

    def test_existing_old_runtime_is_refused_without_repair_or_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._configured_root(Path(directory))
            database = root / ".tasktra/runtime/tasktra.sqlite"
            StateStore(database).migrate()
            connection = sqlite3.connect(database)
            try:
                peel_schema13_interventions(connection, target_version=10)
                connection.execute("PRAGMA user_version = 10")
                connection.commit()
            finally:
                connection.close()
            profile = root / ".tasktra/project.toml"
            lock_path = root / ".tasktra/tasktra.lock"
            before = (database.read_bytes(), profile.read_bytes(), lock_path.read_bytes())

            with self.assertRaisesRegex(StateError, "exact authority-bound"):
                _bootstrap(self._args(root))

            self.assertEqual((database.read_bytes(), profile.read_bytes(), lock_path.read_bytes()), before)
            self.assertEqual(list(database.parent.glob(f"{database.name}.v10*.bak")), [])

    def test_repeat_bootstrap_with_a_current_runtime_preserves_the_lock(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._configured_root(Path(directory))
            first = _bootstrap(self._args(root))
            database = root / ".tasktra/runtime/tasktra.sqlite"
            lock_path = root / ".tasktra/tasktra.lock"
            before = (database.read_bytes(), lock_path.read_bytes())

            second = _bootstrap(self._args(root))

            self.assertEqual((first["runtime_schema"], second["runtime_schema"]), (SCHEMA_VERSION, SCHEMA_VERSION))
            self.assertEqual((database.read_bytes(), lock_path.read_bytes()), before)

    def test_existing_current_runtime_does_not_repair_unrelated_lock_drift(self) -> None:
        with TemporaryDirectory() as directory:
            root = self._configured_root(Path(directory))
            database = root / ".tasktra/runtime/tasktra.sqlite"
            StateStore(database).migrate()
            lock_path = root / ".tasktra/tasktra.lock"
            lock_before = lock_path.read_bytes()

            result = _bootstrap(self._args(root))

            self.assertEqual(result["runtime_schema"], SCHEMA_VERSION)
            self.assertEqual(lock_path.read_bytes(), lock_before)

    def test_unadopted_bootstrap_leaves_projection_metadata_absent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()

            _bootstrap(self._args(root))

            self.assertTrue((root / ".tasktra/project.toml").is_file())
            self.assertFalse((root / ".tasktra/tasktra.lock").exists())
            self.assertFalse((root / ".tasktra/generated/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
