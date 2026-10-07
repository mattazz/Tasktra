from pathlib import Path
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch

from tasktra.validation import MAX_CAPTURE_CHARS, ValidationError, parse_command, run_validations, validation_plan
from tasktra.validation import _posix_group_has_no_live_members, _terminate_posix_group


def python_argv(source: str) -> list[str]:
    return [sys.executable, "-c", source]


class ValidationTests(unittest.TestCase):
    def test_report_accepts_the_callers_root_alias_but_rejects_child_links(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real = base / "actual-project"
            real.mkdir()
            alias = base / "project-alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable on this host")
            report = alias / "validation/latest.json"
            run_validations(alias, [python_argv("print('alias root')")], report_path=report)
            self.assertEqual(json.loads((real / "validation/latest.json").read_text(encoding="utf-8"))["status"], "passed")
            (real / "linked-child").symlink_to(real / "validation", target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symbolic link|reparse"):
                run_validations(alias, [python_argv("print('must not run')")], report_path=alias / "linked-child/latest.json")
            with self.assertRaisesRegex(ValidationError, "within the project"):
                run_validations(alias, [], report_path=base / "outside.json")

    def test_retained_failure_stops_work_and_survives_a_later_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / ".tasktra/runtime/validation/latest.json"
            results = run_validations(root, [python_argv("print('first')"), python_argv("import sys; print('failure evidence'); sys.exit(3)"), python_argv("raise AssertionError('must not run')")], report_path=report_path)
            self.assertEqual([item.status for item in results], ["passed", "failed"])
            failed = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["checks"][1]["exit_code"], 3)
            self.assertIn("failure evidence", failed["checks"][1]["stdout"])
            self.assertIsNotNone(failed["finished_at"])
            history = report_path.parent / "history" / (failed["report_id"] + ".json")
            run_validations(root, [python_argv("print('success')")], report_path=report_path)
            self.assertEqual(json.loads(history.read_text(encoding="utf-8")), failed)
            latest = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(latest["status"], "passed")
            self.assertNotEqual(latest["report_id"], failed["report_id"])

    def test_truncated_failure_stays_failed_and_discloses_partial_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "latest.json"
            run_validations(root, [python_argv(f"import sys; print('x' * {MAX_CAPTURE_CHARS * 2}); sys.exit(7)")], report_path=report_path)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["partial"])
            self.assertTrue(report["checks"][0]["stdout_truncated"])
            self.assertLessEqual(len(report["checks"][0]["stdout"]), MAX_CAPTURE_CHARS)

    def test_report_write_failure_is_visible_and_preview_never_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "validation/latest.json"
            validation_plan([python_argv("print('preview')")])
            self.assertFalse(report_path.parent.exists())
            with patch("tasktra.validation.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValidationError, "retain validation report"):
                    run_validations(root, [python_argv("print('done')")], report_path=report_path)
            self.assertFalse(report_path.exists())
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_report_rejects_a_link_even_when_its_target_is_inside_project(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "actual"
            destination.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(destination, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable on this host")
            with self.assertRaisesRegex(ValidationError, "symbolic link|reparse"):
                run_validations(root, [python_argv("print('must not run')")], report_path=linked / "latest.json")
            self.assertEqual(list(destination.iterdir()), [])

    def test_permission_denied_cleanup_requires_no_live_group_members(self):
        cases = (
            ("42 Z\n42 Z+\n7 S\n", True),
            ("7 S\n", True),
            ("42 S\n", False),
            ("42 Z\n42 T\n", False),
            ("not a process listing\n", False),
            ("", False),
        )
        for listing, expected in cases:
            with self.subTest(listing=listing), patch(
                "tasktra.validation.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, listing, ""),
            ):
                self.assertEqual(_posix_group_has_no_live_members(42), expected)
        for error in (OSError("no ps"), subprocess.TimeoutExpired("ps", 2)):
            with self.subTest(error=error), patch("tasktra.validation.subprocess.run", side_effect=error):
                self.assertFalse(_posix_group_has_no_live_members(42))
        with patch("tasktra.validation.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "42 Z", "denied")):
            self.assertFalse(_posix_group_has_no_live_members(42))

    def test_both_posix_signals_handle_zombie_only_permission_errors(self):
        process = Mock(pid=42)
        # Windows does not expose SIGKILL/killpg; the injected values exercise
        # this platform-independent control flow on every CI host.
        with patch("tasktra.processes.os.killpg", create=True, side_effect=PermissionError) as killpg, patch(
            "tasktra.processes.signal.SIGKILL", 9, create=True,
        ), patch("tasktra.validation._posix_group_has_no_live_members", return_value=True) as probe:
            _terminate_posix_group(process)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(probe.call_count, 2)
        process.wait.assert_called()
        with patch("tasktra.processes.os.killpg", create=True, side_effect=PermissionError), patch(
            "tasktra.processes.signal.SIGKILL", 9, create=True,
        ), patch(
            "tasktra.validation._posix_group_has_no_live_members", return_value=False,
        ):
            with self.assertRaisesRegex(ValidationError, "cleanup is unverified"):
                _terminate_posix_group(process)

    def test_cleanup_denial_fails_validation_and_stops_sequence(self):
        import tasktra.validation as validation
        from tasktra.processes import ProcessError

        with TemporaryDirectory() as directory, patch.object(
            validation.ArgvProcessRunner, "run", side_effect=ProcessError("cleanup denied"),
        ):
            results = run_validations(directory, [python_argv("pass"), python_argv("print('never')")])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("cleanup denied", results[0].stderr)

    def test_plan_does_not_execute_commands(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            command = python_argv(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')")
            plan = validation_plan([command])
            self.assertEqual(plan[0].status, "planned")
            self.assertEqual(plan[0].argv, tuple(command))
            self.assertFalse(marker.exists())

    def test_argv_preserves_windows_sensitive_arguments_without_parsing(self):
        special_path = r"C:\\a path\\trailing\\"
        quoted = 'contains "quotes"'
        command = ["tool.exe", special_path, quoted]
        self.assertEqual(validation_plan([command])[0].argv, tuple(command))
        with TemporaryDirectory() as directory:
            source = (
                "import sys; "
                f"assert sys.argv[1] == {special_path!r}; "
                f"assert sys.argv[2] == {quoted!r}"
            )
            result = run_validations(directory, [python_argv(source) + [special_path, quoted]])[0]
            self.assertEqual(result.status, "passed", result.stderr)

    def test_legacy_string_command_is_rejected_clearly(self):
        with self.assertRaisesRegex(ValidationError, "argv array"):
            parse_command("python -m unittest")
        with self.assertRaisesRegex(ValidationError, "argv array"):
            validation_plan(["python -m unittest"])

    def test_runner_executes_in_order_and_stops_on_failure(self):
        with TemporaryDirectory() as directory:
            commands = [
                python_argv("print('first')"),
                python_argv("import sys; print('bad'); sys.exit(3)"),
                python_argv("print('never')"),
            ]
            results = run_validations(directory, commands)
            self.assertEqual([item.status for item in results], ["passed", "failed"])
            self.assertEqual(results[1].exit_code, 3)
            self.assertNotIn("never", "".join(item.stdout for item in results))

    def test_validation_forwards_periodic_host_tick(self):
        ticks = []
        with TemporaryDirectory() as directory:
            result = run_validations(
                directory, [python_argv("import time; time.sleep(.12)")],
                on_tick=lambda: ticks.append("tick"),
            )[0]
        self.assertEqual(result.status, "passed")
        self.assertTrue(ticks)

    def test_validation_tick_exception_propagates_after_cleanup(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "lease revoked"):
                run_validations(
                    directory, [python_argv("import time; time.sleep(10)")],
                    on_tick=lambda: (_ for _ in ()).throw(RuntimeError("lease revoked")),
                )

    def test_commands_are_not_interpreted_by_a_shell(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            results = run_validations(directory, [python_argv("print('safe')") + [";", "touch", str(marker)]])
            self.assertEqual(results[0].status, "passed")
            self.assertFalse(marker.exists())

    def test_rejects_invalid_timeout_and_nul_argument(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValidationError):
                run_validations(directory, [], timeout_seconds=0)
            with self.assertRaisesRegex(ValidationError, "NUL"):
                validation_plan([["tool", "bad\x00argument"]])

    def test_timeout_stops_the_validation_sequence_and_descendant_tree(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-marker"
            child = (
                "import time; time.sleep(2); "
                f"open({str(marker)!r}, 'w', encoding='utf-8').write('survived')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(10)"
            )
            results = run_validations(
                directory,
                [python_argv(parent), python_argv("print('never')")],
                timeout_seconds=1,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "timed_out")
            sleep(2.5)
            self.assertFalse(marker.exists(), "timed-out validation left a descendant running")

    def test_output_is_bounded_while_a_command_runs(self):
        with TemporaryDirectory() as directory:
            source = (
                "import sys; "
                f"sys.stdout.write('x' * {MAX_CAPTURE_CHARS * 3}); "
                f"sys.stderr.write('y' * {MAX_CAPTURE_CHARS * 3})"
            )
            result = run_validations(directory, [python_argv(source)])[0]
            self.assertEqual(result.status, "passed")
            self.assertLessEqual(len(result.stdout), MAX_CAPTURE_CHARS)
            self.assertLessEqual(len(result.stderr), MAX_CAPTURE_CHARS)
            self.assertIn("bytes omitted", result.stdout)
            self.assertIn("bytes omitted", result.stderr)

    def test_exited_parent_does_not_leave_descendants_holding_output_pipes(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-marker"
            child = (
                "import time; time.sleep(3); "
                f"open({str(marker)!r}, 'w').write('survived')"
            )
            parent = (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}])"
            )
            started = monotonic()
            result = run_validations(directory, [python_argv(parent)], timeout_seconds=1)[0]
            self.assertEqual(result.status, "passed", result.stderr)
            self.assertLess(monotonic() - started, 2.5)
            sleep(3.2)
            self.assertFalse(marker.exists(), "exited validation parent left a descendant running")

    @unittest.skipIf(os.name == "nt", "POSIX process group signal handling")
    def test_timeout_kills_child_that_ignores_sigterm_when_parent_exits(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-marker"
            child = (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(3); "
                f"open({str(marker)!r}, 'w').write('survived')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            )
            result = run_validations(directory, [python_argv(parent)], timeout_seconds=1)[0]
            self.assertEqual(result.status, "timed_out")
            sleep(3.2)
            self.assertFalse(marker.exists(), "SIGTERM-resistant descendant survived cleanup")


if __name__ == "__main__":
    unittest.main()
