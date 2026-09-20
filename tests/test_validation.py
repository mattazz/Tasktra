from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import sleep
import unittest

from tasktra.validation import MAX_CAPTURE_CHARS, ValidationError, parse_command, run_validations, validation_plan


def python_argv(source: str) -> list[str]:
    return [sys.executable, "-c", source]


class ValidationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
