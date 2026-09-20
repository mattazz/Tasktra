import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tasktra.migrations import (
    MigrationError,
    apply_migration_plan,
    migration_plan_digest,
    preview_migration_snapshot,
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


if __name__ == "__main__":
    unittest.main()
