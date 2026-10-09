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
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class WorkDependenciesCliTests(unittest.TestCase):
    def test_create_and_inspect_prerequisites_with_bounded_read_only_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(invoke("init", "--root", str(root), "--apply")[0], 0)
            self.assertEqual(invoke("goal", "--root", str(root), "create", "Goal", "Delivery", "--id", "goal")[0], 0)
            scope = root / "scope.json"
            scope.write_text('{"paths":["src"],"exclusions":[]}', encoding="utf-8")
            command = ("work", "--root", str(root))
            for identifier, prerequisites in (("parent", []), ("child", ["parent"])):
                arguments = [*command, "create", "goal", identifier, "--id", identifier, "--scope", str(scope)]
                for prerequisite in prerequisites:
                    arguments.extend(("--depends-on", prerequisite))
                self.assertEqual(invoke(*arguments)[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            before = database.read_bytes()
            code, response = invoke(*command, "dependencies", "goal", "--limit", "1")
            self.assertEqual(code, 0)
            self.assertEqual(response["action"], "work-dependencies")
            graph = response["dependencies"]
            self.assertTrue(graph["read_only"])
            self.assertEqual((graph["total"], graph["next_offset"]), (2, 1))
            self.assertEqual(graph["units"][0]["work_unit_id"], "child")
            self.assertFalse(graph["units"][0]["ready"])
            self.assertEqual(graph["units"][0]["prerequisites"][0]["id"], "parent")
            code, response = invoke(*command, "dependencies", "goal", "--work-unit-id", "parent")
            self.assertEqual(code, 0)
            self.assertTrue(response["dependencies"]["units"][0]["ready"])
            for extra in (("--limit", "0"), ("--offset", "-1"), ("--work-unit-id", "missing")):
                with self.subTest(extra=extra):
                    code, response = invoke(*command, "dependencies", "goal", *extra)
                    self.assertEqual(code, 2)
                    self.assertFalse(response["ok"])
            self.assertEqual(database.read_bytes(), before)
            code, response = invoke(
                *command, "create", "goal", "Invalid", "--id", "invalid", "--scope", str(scope),
                "--depends-on", "parent", "--depends-on", "parent",
            )
            self.assertEqual(code, 2)
            self.assertFalse(response["ok"])
            self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
