from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main


def invoke(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class OverviewCliTests(unittest.TestCase):
    def test_text_default_json_and_goal_drilldown_are_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(invoke("init", "--root", str(root), "--apply")[0], 0)
            self.assertEqual(invoke("goal", "--root", str(root), "create", "Build service", "Service delivery", "--id", "service")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            before = database.read_bytes()
            code, text, error = invoke("overview", "--root", str(root))
            self.assertEqual((code, error), (0, ""))
            self.assertIn("Build service", text)
            code, text, error = invoke("overview", "--root", str(root), "--json")
            self.assertEqual((code, error), (0, ""))
            report = json.loads(text)
            self.assertTrue(report["ok"])
            self.assertEqual(report["goals"][0]["id"], "service")
            recommendation = report["goals"][0]["recommendations"][0]
            self.assertEqual(recommendation["argv"], ["tasktra", "status", "--root", str(root.resolve()), "--goal-id", "service"])
            self.assertNotIn("command", recommendation)
            self.assertEqual(report["pagination"]["total"], 1)
            code, text, error = invoke("overview", "--root", str(root), "--goal-id", "service", "--json")
            self.assertEqual((code, error), (0, ""))
            report = json.loads(text)
            self.assertEqual(report["goal"]["id"], "service")
            self.assertEqual(report["pagination"]["scope"], "work_units")
            self.assertEqual(database.read_bytes(), before)

    def test_pagination_and_invalid_requests(self):
        with TemporaryDirectory() as directory:
            root = str(Path(directory))
            self.assertEqual(invoke("init", "--root", root, "--apply")[0], 0)
            for identifier in ("alpha", "bravo"):
                self.assertEqual(invoke("goal", "--root", root, "create", identifier, "Delivery", "--id", identifier)[0], 0)
            code, text, _ = invoke("overview", "--root", root, "--json", "--limit", "1")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["pagination"]["next_offset"], 1)
            code, text, _ = invoke("overview", "--root", root, "--json", "--limit", "1", "--offset", "1")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["goals"][0]["id"], "bravo")
            for extra in (("--goal-id", "missing"), ("--limit", "0"), ("--limit", "101"), ("--offset", "-1")):
                with self.subTest(extra=extra):
                    code, text, error = invoke("overview", "--root", root, *extra)
                    self.assertEqual((code, text), (2, ""))
                    self.assertFalse(json.loads(error)["ok"])

    def test_missing_runtime_is_not_created(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tasktra").mkdir()
            (root / ".tasktra/project.toml").write_text('[project]\nname="Example"\nconfig_version=1\n', encoding="utf-8")
            code, _, error = invoke("overview", "--root", str(root), "--json")
            self.assertEqual(code, 2)
            self.assertIn("does not exist", json.loads(error)["error"])
            self.assertFalse((root / ".tasktra/runtime").exists())


if __name__ == "__main__":
    unittest.main()
