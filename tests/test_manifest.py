import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tasktra.manifest import (
    ManifestError,
    build_generated_manifest,
    build_lockfile,
    read_lockfile,
    read_manifest,
    sha256_text,
    write_lockfile,
    write_manifest,
)
from tasktra.contracts import validate_named


class ManifestTests(unittest.TestCase):
    def test_manifest_and_lockfile_are_deterministic_and_round_trip(self):
        files = {".codex/agents/scout.md": "Scout\n", "AGENTS.md": "Entrypoint\n"}
        manifest = build_generated_manifest(files, tasktra_version="0.1.0", catalog_version="1.2.0", packs=["core"])
        same = build_generated_manifest(dict(reversed(list(files.items()))), tasktra_version="0.1.0", catalog_version="1.2.0", packs=["core"])
        self.assertEqual(manifest.canonical_json(), same.canonical_json())
        self.assertEqual(manifest.files[0].path, ".codex/agents/scout.md")
        self.assertEqual(manifest.files[0].sha256, sha256_text("Scout\n"))
        lockfile = build_lockfile(
            manifest,
            catalog_source_sha256="b" * 64,
            pack_versions={"core": "0.1.0"},
            pack_contracts={"core": {"version": "0.1.0", "contract_version": 1, "trust": "builtin-data-only", "sha256": "c" * 64}},
            schema_versions={"goal": 1, "work-unit": 1},
        )
        validate_named(manifest.as_dict(), "generated-manifest")
        validate_named(lockfile.as_dict(), "lockfile")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(root, manifest)
            write_lockfile(root, lockfile)
            self.assertEqual(read_manifest(root), manifest)
            self.assertEqual(read_lockfile(root), lockfile)
            self.assertEqual(json.loads((root / ".tasktra/tasktra.lock").read_text()) ["generated_manifest_sha256"], manifest.digest)

    def test_manifest_rejects_unsafe_generated_paths(self):
        with self.assertRaises(ManifestError):
            build_generated_manifest({"../AGENTS.md": "no"}, tasktra_version="0.1.0", catalog_version="1", packs=[])

    def test_lockfile_rejects_unknown_keys(self):
        with self.assertRaises(ManifestError):
            from tasktra.manifest import TasktraLock
            TasktraLock.from_dict({
                "schema_version": 3, "tasktra_version": "1", "catalog_version": "1", "catalog_source_sha256": "b" * 64, "packs": [], "pack_versions": {}, "pack_contracts": {},
                "generated_manifest_sha256": "a" * 64, "schema_versions": {}, "surprise": True,
            })

    def test_metadata_write_rejects_symlinked_tasktra_directory(self):
        manifest = build_generated_manifest(
            {"AGENTS.md": "entrypoint\n"}, tasktra_version="0.1.0", catalog_version="1", packs=[]
        )
        with TemporaryDirectory() as directory:
            base = Path(directory)
            project, outside = base / "project", base / "outside"
            project.mkdir()
            outside.mkdir()
            try:
                (project / ".tasktra").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with self.assertRaisesRegex(ManifestError, "link or reparse point"):
                write_manifest(project, manifest)
            self.assertFalse((outside / "generated" / "manifest.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_metadata_write_rejects_junctioned_tasktra_directory(self):
        manifest = build_generated_manifest(
            {"AGENTS.md": "entrypoint\n"}, tasktra_version="0.1.0", catalog_version="1", packs=[]
        )
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside_directory:
            project, outside = Path(directory), Path(outside_directory)
            junction = project / ".tasktra"
            command = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
            with self.assertRaisesRegex(ManifestError, "link or reparse point"):
                write_manifest(project, manifest)
            self.assertFalse((outside / "generated" / "manifest.json").exists())
