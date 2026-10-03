"""Shared file locks serialize processes and survive interrupted writers."""

import multiprocessing
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest

from tasktra.filelocks import FileLockBusyError, FileLockError, exclusive_file_lock


def hold_lock(root, ready):
    with exclusive_file_lock(Path(root) / ".store.lock", root=root):
        ready.send(True)
        ready.close()
        time.sleep(30)


class FileLockTests(unittest.TestCase):
    def test_other_process_is_excluded_and_termination_releases_ownership(self):
        context = multiprocessing.get_context("spawn")
        with TemporaryDirectory() as root:
            path = Path(root) / ".store.lock"
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=hold_lock, args=(root, sender))
            process.start()
            sender.close()
            try:
                self.assertTrue(receiver.poll(15), "lock holder did not start")
                self.assertTrue(receiver.recv())
                with self.assertRaises(FileLockBusyError):
                    with exclusive_file_lock(path, root=root):
                        self.fail("concurrent caller entered the locked region")
                process.terminate()
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertTrue(path.exists(), "lock inode must stay stable for waiters")
                with exclusive_file_lock(path, root=root):
                    pass
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(10)
                process.close()
                receiver.close()

    def test_stale_marker_and_exception_do_not_leave_ownership(self):
        with TemporaryDirectory() as root:
            path = Path(root) / ".store.lock"
            path.write_text("legacy marker", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with exclusive_file_lock(path, root=root):
                    raise RuntimeError("interrupted")
            with exclusive_file_lock(path, root=root):
                pass
            self.assertEqual(path.read_text(encoding="utf-8"), "legacy marker")

    def test_bounds_traversal_directories_and_hardlinks_are_rejected(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as other:
            path = Path(root) / ".store.lock"
            for timeout in (-1, 61, float("inf"), float("nan"), True):
                with self.subTest(timeout=timeout), self.assertRaises(FileLockError):
                    with exclusive_file_lock(path, root=root, timeout_seconds=timeout):
                        pass
            for unsafe in (Path(other) / "lock", Path(root) / ".." / "lock", Path(root)):
                with self.subTest(path=unsafe), self.assertRaises(FileLockError):
                    with exclusive_file_lock(unsafe, root=root):
                        pass
            original = Path(other) / "original"
            original.write_text("unchanged", encoding="utf-8")
            os.link(original, path)
            with self.assertRaisesRegex(FileLockError, "one link"):
                with exclusive_file_lock(path, root=root):
                    pass
            self.assertEqual(original.read_text(encoding="utf-8"), "unchanged")

    def test_linked_lock_and_parent_are_rejected(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as other:
            target = Path(other) / "original"
            target.write_text("unchanged", encoding="utf-8")
            link = Path(root) / ".store.lock"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable on this platform")
            with self.assertRaisesRegex(FileLockError, "link or reparse point"):
                with exclusive_file_lock(link, root=root):
                    pass
            link.unlink()
            link.symlink_to(other, target_is_directory=True)
            with self.assertRaisesRegex(FileLockError, "link or reparse point"):
                with exclusive_file_lock(link / "new.lock", root=root):
                    pass
            self.assertFalse((Path(other) / "new.lock").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior only")
    def test_windows_junction_parent_is_rejected_without_symlink_privilege(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as other:
            junction = Path(root) / "redirect"
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), other],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            with self.assertRaisesRegex(FileLockError, "reparse point"):
                with exclusive_file_lock(junction / "new.lock", root=root):
                    pass
            self.assertFalse((Path(other) / "new.lock").exists())


if __name__ == "__main__":
    unittest.main()
