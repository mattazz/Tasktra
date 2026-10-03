import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tasktra.migrations import (
    CommittedRuntimeRecoveryRequired,
    MigrationError,
    MAX_PREPARED_RECOVERY_BYTES,
    apply_migration_plan,
    migration_plan_digest,
    preview_migration_snapshot,
    read_prepared_runtime_recoveries,
    rollback_migration,
)


def preview(*, argv: list[str], write_paths: list[str], network: bool = False) -> dict:
    return {
        "ok": True,
        "changes": [{
            "pack": "vendor",
            "from": "1.0.0",
            "to": "1.1.0",
            "kind": "executable",
            "description": "fixture",
            "effects": {"argv": argv, "network": network, "read_paths": ["migrate.py"], "write_paths": write_paths},
        }],
        "blockers": [],
        "mutation": "none",
    }


class StageSixMigrationTests(unittest.TestCase):
    def test_oversized_prepared_recovery_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            plan_sha256 = "a" * 64
            journal = project / ".tasktra/upgrades" / plan_sha256 / ("prepared-" + "b" * 32 + ".json")
            journal.parent.mkdir(parents=True)
            journal.write_bytes(b" " * (MAX_PREPARED_RECOVERY_BYTES + 1))
            with self.assertRaisesRegex(MigrationError, "byte bound"):
                read_prepared_runtime_recoveries(project, migration_plan_sha256=plan_sha256)

    def _fixture(self, root: Path, script: str) -> tuple[Path, dict]:
        project = root / "project"
        catalog = root / "catalog"
        pack = catalog / "packs" / "vendor"
        pack.mkdir(parents=True)
        project.mkdir()
        (project / "data.txt").write_text("before\n", encoding="utf-8")
        (pack / "migrate.py").write_text(script, encoding="utf-8")
        plan = preview(argv=["python", "migrate.py"], write_paths=["data.txt"])
        return catalog, plan

    def test_snapshot_preview_is_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "data.txt").write_text("before\n", encoding="utf-8")
            plan = preview(argv=["python", "migrate.py"], write_paths=["data.txt"])
            before = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            report = preview_migration_snapshot(project, plan)
            after = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(report["mutation"], "none")
            self.assertEqual(report["entry_count"], 1)

    def test_digest_confirmation_success_and_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nPath(__import__('os').environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after\\n')\n",
            )
            project = root / "project"
            digest = migration_plan_digest(plan)
            result = apply_migration_plan(project, catalog, plan, expected_plan_sha256=digest, confirmed=True)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "after\n")
            self.assertTrue(result.snapshot_path.is_file())
            restored = rollback_migration(project, digest, expected_before_sha256=result.before_sha256)
            self.assertTrue(restored["ok"])
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "before\n")

    def test_explicit_rollback_requires_untampered_pre_mutation_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nPath(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after')\n",
            )
            project = root / "project"
            digest = migration_plan_digest(plan)
            result = apply_migration_plan(project, catalog, plan, expected_plan_sha256=digest, confirmed=True)
            with self.assertRaisesRegex(MigrationError, "expected pre-mutation digest"):
                rollback_migration(project, digest, expected_before_sha256="0" * 64)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "after")
            rollback_migration(project, digest, expected_before_sha256=result.before_sha256)

    def test_failure_rolls_back_created_and_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nr=Path(os.environ['TASKTRA_PROJECT_ROOT']); (r/'data.txt').write_text('broken'); raise SystemExit(7)\n",
            )
            project = root / "project"
            with self.assertRaisesRegex(MigrationError, "rollback completed"):
                apply_migration_plan(project, catalog, plan, expected_plan_sha256=migration_plan_digest(plan), confirmed=True)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "before\n")

    def test_verification_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nPath(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after')\n",
            )
            project = root / "project"
            with self.assertRaisesRegex(MigrationError, "post-migration verification failed"):
                apply_migration_plan(
                    project,
                    catalog,
                    plan,
                    expected_plan_sha256=migration_plan_digest(plan),
                    confirmed=True,
                    verifier=lambda: {"ok": False, "reason": "fixture"},
                )
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "before\n")

    def test_command_receives_an_isolated_project_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nr=Path(os.environ['TASKTRA_PROJECT_ROOT']); assert r != Path.cwd().parents[2]; (r/'data.txt').write_text('isolated')\n",
            )
            project = root / "project"
            result = apply_migration_plan(
                project,
                catalog,
                plan,
                expected_plan_sha256=migration_plan_digest(plan),
                confirmed=True,
            )
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "isolated")
            self.assertEqual(result.commands[0]["exit_code"], 0)

    def test_command_environment_excludes_host_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "import os\nfrom pathlib import Path\nassert 'TASKTRA_TEST_SECRET' not in os.environ\n"
                "assert os.environ['TASKTRA_NETWORK_ALLOWED'] == '0'\n"
                "Path(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('scrubbed')\n",
            )
            project = root / "project"
            with patch.dict("os.environ", {"TASKTRA_TEST_SECRET": "do-not-forward"}, clear=False):
                result = apply_migration_plan(
                    project, catalog, plan,
                    expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                )
            self.assertEqual(result.commands[0]["exit_code"], 0)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "scrubbed")

    def test_network_digest_and_unsafe_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(root, "raise SystemExit(0)\n")
            project = root / "project"
            network_plan = preview(argv=["python", "migrate.py"], write_paths=["data.txt"], network=True)
            with self.assertRaisesRegex(MigrationError, "declares network access"):
                apply_migration_plan(project, catalog, network_plan, expected_plan_sha256=migration_plan_digest(network_plan), confirmed=True)
            with self.assertRaisesRegex(MigrationError, "digest changed"):
                apply_migration_plan(project, catalog, plan, expected_plan_sha256="0" * 64, confirmed=True)
            unsafe = preview(argv=["python", "migrate.py"], write_paths=["../outside"])
            with self.assertRaisesRegex(MigrationError, "unsafe"):
                preview_migration_snapshot(project, unsafe)

    def test_migration_journal_rejects_symlinked_tasktra_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            catalog, plan = self._fixture(base, "raise SystemExit(0)\n")
            project, outside = base / "project", base / "outside"
            outside.mkdir()
            try:
                (project / ".tasktra").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with self.assertRaisesRegex(MigrationError, "journal path crosses a link or reparse point"):
                apply_migration_plan(
                    project, catalog, plan,
                    expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                )
            self.assertFalse(any(outside.rglob("snapshot.json")))

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_migration_journal_rejects_junctioned_tasktra_directory(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            base = Path(directory)
            catalog, plan = self._fixture(base, "raise SystemExit(0)\n")
            project, outside = base / "project", Path(outside_directory)
            junction = project / ".tasktra"
            command = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
            with self.assertRaisesRegex(MigrationError, "journal path crosses a link or reparse point"):
                apply_migration_plan(
                    project, catalog, plan,
                    expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                )
            self.assertFalse(any(outside.rglob("snapshot.json")))

    def test_post_verifier_capture_failure_preserves_committed_runtime_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nPath(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after')\n",
            )
            project = root / "project"
            import tasktra.migrations as migrations

            original = migrations._capture_entries
            captures = 0

            def fail_post_verifier_capture(*args: object, **kwargs: object) -> tuple[dict[str, object], ...]:
                nonlocal captures
                captures += 1
                if captures == 3:
                    raise OSError("forced post-verifier capture failure")
                return original(*args, **kwargs)

            verification = {
                "ok": True,
                "runtime_schema_changed": True,
                "runtime_backup_path": "retained.sqlite.bak",
                "runtime_backup_sha256": "a" * 64,
            }
            with patch("tasktra.migrations._capture_entries", side_effect=fail_post_verifier_capture):
                with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                    apply_migration_plan(
                        project, catalog, plan,
                        expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                        verifier=lambda: verification,
                    )
            self.assertEqual(raised.exception.verification, verification)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "after")
            receipt = next((project / ".tasktra" / "upgrades").glob("*/receipt.json"))
            self.assertTrue(__import__("json").loads(receipt.read_text(encoding="utf-8"))["recovery_required"])

    def test_recovery_receipt_failure_preserves_recovery_marker_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nPath(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after')\n",
            )
            project = root / "project"
            import tasktra.migrations as migrations

            original = migrations._write_json
            writes = 0

            def fail_receipt_writes(*args: object, **kwargs: object) -> None:
                nonlocal writes
                writes += 1
                if writes > 1:
                    raise OSError("forced recovery receipt write failure")
                original(*args, **kwargs)

            verification = {
                "ok": True,
                "runtime_schema_changed": True,
                "runtime_backup_path": "retained.sqlite.bak",
                "runtime_backup_sha256": "b" * 64,
            }
            with patch("tasktra.migrations._write_json", side_effect=fail_receipt_writes):
                with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                    apply_migration_plan(
                        project, catalog, plan,
                        expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                        verifier=lambda: verification,
                    )
            self.assertIn("recovery receipt could not be written", str(raised.exception))
            self.assertEqual(raised.exception.verification, verification)
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "after")

    def test_indeterminate_recovery_probe_fails_closed_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, plan = self._fixture(
                root,
                "from pathlib import Path\nimport os\nPath(os.environ['TASKTRA_PROJECT_ROOT'], 'data.txt').write_text('after')\n",
            )
            project = root / "project"

            def unavailable_probe() -> None:
                raise OSError("runtime inspection unavailable")

            with self.assertRaises(CommittedRuntimeRecoveryRequired) as raised:
                apply_migration_plan(
                    project, catalog, plan,
                    expected_plan_sha256=migration_plan_digest(plan), confirmed=True,
                    verifier=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
                    recovery_probe=unavailable_probe,
                )
            self.assertTrue(raised.exception.verification["runtime_schema_indeterminate"])
            self.assertEqual((project / "data.txt").read_text(encoding="utf-8"), "after")


if __name__ == "__main__":
    unittest.main()
