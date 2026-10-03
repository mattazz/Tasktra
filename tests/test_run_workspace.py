from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.run_workspace import RunWorkspace, RunWorkspaceError


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


class RunWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        _git(self.root, "init")
        _git(self.root, "config", "user.email", "tests@example.invalid")
        _git(self.root, "config", "user.name", "Tests")
        (self.root / "tracked.txt").write_text("before\n", encoding="utf-8")
        (self.root / "removed.txt").write_text("remove me\n", encoding="utf-8")
        (self.root / ".tasktra").mkdir()
        (self.root / ".tasktra" / "project.toml").write_text("[project]\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".tasktra/runtime/\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clone_isolated_has_no_remote_and_publish_applies_patch(self) -> None:
        with RunWorkspace(self.root, "attempt-one") as workspace:
            assert workspace.path is not None
            self.assertEqual(_git(workspace.path, "remote").stdout, b"")
            self.assertFalse(os.path.samefile(self.root / "tracked.txt", workspace.path / "tracked.txt"))
            (workspace.path / "tracked.txt").write_text("after\n", encoding="utf-8")
            captured = workspace.capture()
            self.assertEqual(captured["changed_paths"], ["tracked.txt"])
            self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "before\n")
            self.assertIsNotNone(workspace.patch_path)
            workspace.publish()
            self.assertTrue(workspace.published)
        self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "after\n")

    def test_capture_and_publish_handles_binary_untracked_and_deleted_files(self) -> None:
        with RunWorkspace(self.root, "attempt-mixed") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("changed\n", encoding="utf-8")
            (workspace.path / "removed.txt").unlink()
            (workspace.path / "new.bin").write_bytes(b"\0\xffbinary\0")
            (workspace.path / "new.txt").write_text("untracked\n", encoding="utf-8")
            captured = workspace.capture()
            self.assertEqual(captured["changed_paths"], ["new.bin", "new.txt", "removed.txt", "tracked.txt"])
            self.assertIsNotNone(workspace.patch_path)
            self.assertEqual(workspace.patch_path.read_bytes().count(b"GIT binary patch"), 1)
            workspace.publish()
        self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertFalse((self.root / "removed.txt").exists())
        self.assertEqual((self.root / "new.bin").read_bytes(), b"\0\xffbinary\0")
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "untracked\n")

    def test_repeated_capture_preserves_each_snapshot_and_publishes_latest(self) -> None:
        with RunWorkspace(self.root, "attempt-snapshots") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("first\n", encoding="utf-8")
            first = workspace.capture()
            assert workspace.patch_path is not None
            first_path = workspace.patch_path
            self.assertEqual(workspace.capture(), first)
            self.assertEqual(workspace.patch_path, first_path)
            (workspace.path / "tracked.txt").write_text("second\n", encoding="utf-8")
            second = workspace.capture()
            assert workspace.patch_path is not None
            self.assertNotEqual(first["patch_sha256"], second["patch_sha256"])
            self.assertNotEqual(first_path, workspace.patch_path)
            self.assertTrue(first_path.is_file())
            workspace.publish()
        self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "second\n")

    def test_refresh_uses_only_retained_patch_and_discards_ignored_worker_residue(self) -> None:
        with RunWorkspace(self.root, "attempt-refresh") as workspace:
            assert workspace.path is not None
            worker_checkout = workspace.path
            (worker_checkout / ".gitignore").write_text(
                ".tasktra/runtime/\nignored_module/\n", encoding="utf-8"
            )
            ignored_module = worker_checkout / "ignored_module"
            ignored_module.mkdir()
            (ignored_module / "module.py").write_text("worker-only\n", encoding="utf-8")
            captured = workspace.capture()
            self.assertEqual(captured["changed_paths"], [".gitignore"])
            refreshed = workspace.refresh_from_patch()
            self.assertNotEqual(refreshed, worker_checkout)
            self.assertTrue((worker_checkout / "ignored_module" / "module.py").is_file())
            self.assertEqual(
                (refreshed / ".gitignore").read_text(encoding="utf-8"),
                ".tasktra/runtime/\nignored_module/\n",
            )
            self.assertFalse((refreshed / "ignored_module").exists())
            self.assertEqual(_git(refreshed, "remote").stdout, b"")

    def test_empty_capture_publishes_as_a_noop(self) -> None:
        with RunWorkspace(self.root, "attempt-empty") as workspace:
            captured = workspace.capture()
            self.assertEqual(captured["changed_paths"], [])
            workspace.publish()
            self.assertTrue(workspace.published)
        self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "before\n")

    def test_coordinator_runtime_is_not_copied_to_worker_clone(self) -> None:
        runtime = self.root / ".tasktra" / "runtime"
        runtime.mkdir()
        (runtime / "state.sqlite").write_bytes(b"coordinator-only")
        with RunWorkspace(self.root, "attempt-no-runtime") as workspace:
            assert workspace.path is not None
            self.assertFalse((workspace.path / ".tasktra" / "runtime").exists())

    def test_reserved_runtime_is_rejected_and_never_copied_back(self) -> None:
        with RunWorkspace(self.root, "attempt-runtime") as workspace:
            assert workspace.path is not None
            runtime = workspace.path / ".tasktra" / "runtime"
            runtime.mkdir()
            (runtime / "state.sqlite").write_bytes(b"not coordinator state")
            with self.assertRaisesRegex(RunWorkspaceError, "reserved path .tasktra/runtime"):
                workspace.capture()
        self.assertFalse((self.root / ".tasktra" / "runtime").exists())

    def test_reserved_runtime_is_rechecked_after_earlier_capture(self) -> None:
        with RunWorkspace(self.root, "attempt-runtime-later") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("first\n", encoding="utf-8")
            workspace.capture()
            assert workspace.patch_path is not None
            first_artifact = workspace.patch_path
            runtime = workspace.path / ".tasktra" / "runtime"
            runtime.mkdir()
            (runtime / "state.sqlite").write_bytes(b"forbidden")
            with self.assertRaisesRegex(RunWorkspaceError, "reserved path .tasktra/runtime"):
                workspace.capture()
            self.assertTrue(first_artifact.is_file())

    def test_reserved_project_configuration_is_rejected(self) -> None:
        with RunWorkspace(self.root, "attempt-config") as workspace:
            assert workspace.path is not None
            (workspace.path / ".tasktra" / "project.toml").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RunWorkspaceError, "reserved path .tasktra/project.toml"):
                workspace.capture()

    def test_nested_instruction_and_host_control_paths_are_rejected(self) -> None:
        for attempt, relative in (
            ("attempt-instruction", "nested/aGeNtS.Md"),
            ("attempt-override", "AGENTS.override.md"),
            ("attempt-nested-override", "nested/AgEnTs.OvErRiDe.Md"),
            ("attempt-codex", ".CoDeX/settings.toml"),
            ("attempt-agents", ".AgEnTs/role.md"),
            ("attempt-claude", ".ClAuDe/role.md"),
            ("attempt-mcp", ".MCP.JSON"),
        ):
            with self.subTest(relative=relative), RunWorkspace(self.root, attempt) as workspace:
                assert workspace.path is not None
                target = workspace.path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("worker control\n", encoding="utf-8")
                with self.assertRaisesRegex(RunWorkspaceError, "reserved host"):
                    workspace.capture()

    def test_worker_commit_is_rejected(self) -> None:
        with RunWorkspace(self.root, "attempt-commit") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("worker\n", encoding="utf-8")
            _git(workspace.path, "add", "tracked.txt")
            _git(workspace.path, "-c", "user.email=tests@example.invalid", "-c", "user.name=Tests", "commit", "-m", "worker")
            with self.assertRaisesRegex(RunWorkspaceError, "HEAD changed"):
                workspace.capture()

    def test_original_change_blocks_publish_and_preserves_artifact(self) -> None:
        with RunWorkspace(self.root, "attempt-conflict") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("worker\n", encoding="utf-8")
            captured = workspace.capture()
            assert workspace.patch_path is not None
            artifact = workspace.patch_path
            (self.root / "tracked.txt").write_text("coordinator\n", encoding="utf-8")
            with self.assertRaisesRegex(RunWorkspaceError, "pristine"):
                workspace.publish()
            self.assertFalse(workspace.published)
            self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "coordinator\n")
            self.assertEqual(captured["patch_sha256"], sha256(artifact.read_bytes()).hexdigest())

    def test_apply_failure_does_not_mark_publish_complete_or_overwrite_root(self) -> None:
        with RunWorkspace(self.root, "attempt-apply-failure") as workspace:
            assert workspace.path is not None
            (workspace.path / "tracked.txt").write_text("worker\n", encoding="utf-8")
            workspace.capture()
            actual_git = workspace._git

            def change_after_check(directory: Path, *arguments: str, **kwargs: object) -> bytes:
                result = actual_git(directory, *arguments, **kwargs)
                if arguments[:1] == ("apply",) and "--check" in arguments:
                    (self.root / "tracked.txt").write_text("coordinator\n", encoding="utf-8")
                return result

            with patch.object(workspace, "_git", side_effect=change_after_check):
                with self.assertRaisesRegex(RunWorkspaceError, "Git command failed"):
                    workspace.publish()
            self.assertFalse(workspace.published)
            self.assertEqual((self.root / "tracked.txt").read_text(encoding="utf-8"), "coordinator\n")

    def test_symlink_change_is_rejected(self) -> None:
        with RunWorkspace(self.root, "attempt-symlink") as workspace:
            assert workspace.path is not None
            link = workspace.path / "linked.txt"
            try:
                os.symlink("tracked.txt", link)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaisesRegex(RunWorkspaceError, "symlink or reparse"):
                workspace.capture()

    def test_non_portable_path_is_rejected_before_git_can_apply_it(self) -> None:
        with RunWorkspace(self.root, "attempt-portable") as workspace:
            assert workspace.path is not None
            with self.assertRaisesRegex(RunWorkspaceError, "non-portable"):
                workspace._validate_changed_path(workspace.path, "ambiguous:name.txt")


if __name__ == "__main__":
    unittest.main()
