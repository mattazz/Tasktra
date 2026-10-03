"""Regression coverage for compile rollback across generated and metadata writes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.cli import main
from tasktra.migrations import CommittedRuntimeRecoveryRequired
from tasktra.manifest import (
    build_generated_manifest,
    build_lockfile,
    read_lockfile,
    read_manifest,
    write_lockfile,
    write_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


class CompileAtomicityTests(unittest.TestCase):
    def test_cli_preserves_closed_runtime_recovery_evidence_when_receipt_is_unavailable(self):
        verification = {
            "runtime_schema_before": 8,
            "runtime_schema_after": 9,
            "runtime_schema_observed": 10,
            "runtime_schema_changed": True,
            "runtime_backup_path": "C:/recovery/runtime.sqlite.v8.bak",
            "runtime_backup_sha256": "a" * 64,
            "untrusted_extra": "must not be emitted",
        }
        with patch(
            "tasktra.cli._compile",
            side_effect=CommittedRuntimeRecoveryRequired("recovery required", verification),
        ):
            code, result = run_cli("compile")
        self.assertEqual(code, 2)
        self.assertTrue(result["recovery_required"])
        self.assertEqual(result["recovery"]["runtime_backup_sha256"], "a" * 64)
        self.assertEqual(result["recovery"]["runtime_schema_observed"], 10)
        self.assertNotIn("untrusted_extra", result["recovery"])

    def _project_with_changed_catalog(self, directory: Path) -> tuple[Path, Path, tuple[str, ...]]:
        project, catalog = directory / "project", directory / "catalog"
        shutil.copytree(ROOT / "catalog", catalog)
        arguments = ("compile", "--root", str(project), "--catalog", str(catalog), "--trust-catalog")
        self.assertEqual(run_cli("init", "--root", str(project), "--apply")[0], 0)
        self.assertEqual(run_cli(*arguments)[0], 0)
        (project / "unrelated.txt").write_text("preserve me\n", encoding="utf-8")
        role = catalog / "roles" / "scout.md"
        role.write_text(role.read_text(encoding="utf-8") + "\nChanged compile fixture.\n", encoding="utf-8")
        return project, catalog, arguments

    def _assert_failure_restores(self, project: Path, arguments: tuple[str, ...]) -> None:
        before = file_snapshot(project)
        code, result = run_cli(*arguments)
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(file_snapshot(project), before)
        self.assertEqual((project / "unrelated.txt").read_text(encoding="utf-8"), "preserve me\n")

    @staticmethod
    def _add_stale_managed_output(project: Path) -> Path:
        old_manifest, old_lock = read_manifest(project), read_lockfile(project)
        stale = project / ".codex" / "legacy-managed.txt"
        stale.write_text("legacy managed output\n", encoding="utf-8")
        files = {
            item.path: (project / Path(item.path)).read_bytes()
            for item in old_manifest.files
        }
        files[stale.relative_to(project).as_posix()] = stale.read_bytes()
        manifest = build_generated_manifest(
            files,
            tasktra_version=old_manifest.tasktra_version,
            catalog_version=old_manifest.catalog_version,
            packs=old_manifest.packs,
        )
        lock = build_lockfile(
            manifest,
            catalog_source_sha256=old_lock.catalog_source_sha256,
            pack_versions=dict(old_lock.pack_versions),
            pack_contracts={
                name: {
                    "version": version,
                    "contract_version": contract_version,
                    "trust": trust,
                    "sha256": digest,
                }
                for name, version, contract_version, trust, digest in old_lock.pack_contracts
            },
            schema_versions=dict(old_lock.schema_versions),
        )
        write_manifest(project, manifest)
        write_lockfile(project, lock)
        return stale

    def test_second_projection_replacement_restores_every_affected_file(self):
        with TemporaryDirectory() as directory:
            project, _, arguments = self._project_with_changed_catalog(Path(directory))
            original_replace = os.replace
            replacements = 0

            def fail_second_replace(source: object, destination: object) -> None:
                nonlocal replacements
                replacements += 1
                if replacements == 2:
                    raise OSError("forced second projection replacement failure")
                original_replace(source, destination)

            with patch("tasktra.compiler.os.replace", side_effect=fail_second_replace):
                self._assert_failure_restores(project, arguments)

    def test_manifest_failure_restores_projection(self):
        with TemporaryDirectory() as directory:
            project, _, arguments = self._project_with_changed_catalog(Path(directory))
            with patch("tasktra.cli.write_manifest", side_effect=OSError("forced manifest failure")):
                self._assert_failure_restores(project, arguments)

    def test_lock_failure_restores_projection_and_manifest(self):
        with TemporaryDirectory() as directory:
            project, _, arguments = self._project_with_changed_catalog(Path(directory))
            with patch("tasktra.cli.write_lockfile", side_effect=OSError("forced lock failure")):
                self._assert_failure_restores(project, arguments)

    def test_lock_failure_restores_pruned_stale_output(self):
        with TemporaryDirectory() as directory:
            project, _, arguments = self._project_with_changed_catalog(Path(directory))
            stale = self._add_stale_managed_output(project)
            with patch("tasktra.cli.write_lockfile", side_effect=OSError("forced lock failure")):
                self._assert_failure_restores(project, (*arguments, "--prune-stale"))
            self.assertEqual(stale.read_text(encoding="utf-8"), "legacy managed output\n")

    def test_restore_rejects_a_symlinked_output_parent_swapped_after_snapshot(self):
        with TemporaryDirectory() as directory:
            project, _, arguments = self._project_with_changed_catalog(Path(directory))
            outside = Path(directory) / "outside"
            outside.mkdir()
            external = outside / "external.txt"
            external.write_text("do not touch\n", encoding="utf-8")
            redirected = project / ".codex"

            def swap_parent_then_fail(*args: object, **kwargs: object) -> None:
                shutil.rmtree(redirected)
                if os.name == "nt":
                    command = subprocess.run(
                        ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirected), str(outside)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if command.returncode != 0:
                        raise OSError(command.stderr or command.stdout)
                else:
                    redirected.symlink_to(outside, target_is_directory=True)
                raise OSError("forced lock failure after topology swap")

            try:
                with patch("tasktra.cli.write_lockfile", side_effect=swap_parent_then_fail):
                    code, result = run_cli(*arguments)
                self.assertEqual(code, 2)
                self.assertIn("link or reparse point", str(result["error"]))
                self.assertEqual(external.read_text(encoding="utf-8"), "do not touch\n")
                self.assertFalse((outside / "agents").exists())
            except OSError as error:
                self.skipTest(f"link or junction creation is unavailable: {error}")
            finally:
                if redirected.is_symlink() or bool(getattr(redirected, "is_junction", lambda: False)()):
                    redirected.unlink()


if __name__ == "__main__":
    unittest.main()
