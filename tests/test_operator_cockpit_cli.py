from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main


def invoke(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class OperatorCockpitCliTests(unittest.TestCase):
    def test_export_is_read_only_and_refuses_existing_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(invoke("init", "--root", str(root), "--apply")[0], 0)
            self.assertEqual(invoke("goal", "--root", str(root), "create", "Goal", "Description", "--id", "goal")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            before = database.read_bytes()
            output = root / "cockpit.html"
            code, text, error = invoke("cockpit", "--root", str(root), "export", str(output), "--page-size", "5")
            self.assertEqual((code, error), (0, ""))
            result = json.loads(text)
            self.assertEqual(result["action"], "cockpit-export")
            self.assertTrue(result["read_only_runtime"])
            self.assertEqual(Path(result["output"]), output.resolve())
            self.assertTrue(output.is_file())
            self.assertEqual(database.read_bytes(), before)
            code, text, error = invoke("cockpit", "--root", str(root), "export", str(output))
            self.assertEqual((code, text), (2, ""))
            self.assertIn("already exists", json.loads(error)["error"])

    def test_invalid_output_and_page_size_do_not_publish(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(invoke("init", "--root", str(root), "--apply")[0], 0)
            for output, extra in ((root / "cockpit.txt", ()), (root / "bad.html", ("--page-size", "0"))):
                with self.subTest(output=output):
                    code, text, error = invoke("cockpit", "--root", str(root), "export", str(output), *extra)
                    self.assertEqual((code, text), (2, ""))
                    self.assertFalse(output.exists())
                    self.assertFalse(json.loads(error)["ok"])


if __name__ == "__main__":
    unittest.main()
