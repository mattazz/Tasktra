import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from tasktra.processes import ArgvProcessRunner, ProcessError


def python_argv(source: str) -> list[str]:
    return [sys.executable, "-c", source]


class ArgvProcessRunnerTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX session signal behavior")
    def test_normal_parent_exit_still_kills_term_resistant_descendant(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "escaped-descendant"
            child = (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(2); "
                f"open({str(marker)!r}, 'w').write('survived')"
            )
            parent = "import subprocess, sys; " f"subprocess.Popen([sys.executable, '-c', {child!r}])"
            started = monotonic()
            result = ArgvProcessRunner(timeout=3).run(python_argv(parent), cwd=directory, env=None)
            self.assertEqual(result.returncode, 0)
            self.assertLess(monotonic() - started, 2)
            sleep(2.2)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "POSIX session signal behavior")
    def test_callback_error_cleans_up_owned_descendants(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "callback-descendant"
            child = "import time; time.sleep(2); " f"open({str(marker)!r}, 'w').write('survived')"
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); print('event', flush=True); time.sleep(10)"
            )
            with self.assertRaisesRegex(RuntimeError, "stop stream"):
                ArgvProcessRunner(timeout=4).run(
                    python_argv(parent), cwd=directory, env=None,
                    on_stdout_chunk=lambda _: (_ for _ in ()).throw(RuntimeError("stop stream")),
                )
            sleep(2.2)
            self.assertFalse(marker.exists())

    def test_output_limit_terminates_and_retains_only_the_cap(self):
        result = ArgvProcessRunner(timeout=5, output_limit=32).run(
            python_argv("import sys, time; sys.stdout.write('x' * 10000); sys.stdout.flush(); time.sleep(5)"),
            cwd=Path.cwd(), env=None,
        )
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout), 32)
        self.assertFalse(result.timed_out)

    def test_tick_can_cancel_a_running_command(self):
        result = ArgvProcessRunner(timeout=5).run(
            python_argv("import time; time.sleep(5)"), cwd=Path.cwd(), env=None,
            on_tick=lambda: False, tick_interval=0.01,
        )
        self.assertTrue(result.cancelled)

    @unittest.skipUnless(os.name == "nt", "Windows Job handle lifecycle")
    def test_windows_job_close_failure_is_a_cleanup_error(self):
        class FailingJob:
            def close(self):
                raise OSError("CloseHandle denied")

        with patch("tasktra.processes._WindowsJob.assign", return_value=FailingJob()):
            with self.assertRaisesRegex(ProcessError, "Job close failed"):
                ArgvProcessRunner(timeout=.05).run(
                    python_argv("import time; time.sleep(10)"), cwd=Path.cwd(), env=None,
                )


if __name__ == "__main__":
    unittest.main()
