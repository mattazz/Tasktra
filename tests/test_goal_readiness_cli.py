"""Public CLI coverage for the goal-readiness read model."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import build_parser, main
from tests import test_work_plan_cli as plans


def invoke(*arguments: str) -> tuple[int, dict[str, object], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    text = stdout.getvalue() or stderr.getvalue()
    return code, json.loads(text), text


class GoalReadinessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # A spaced nondefault root proves returned arrays retain one exact path
        # token rather than relying on caller shell quoting or the cwd.
        self.root = Path(self.directory.name) / "operator root"
        self.root.mkdir()
        self.store = plans.make_project(self.root)
        manifest = plans.example_plan()
        preview = self.store.preview_work_plan(manifest)
        self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.command = ("work", "--root", str(self.root), "readiness", "goal-1")

    def _readiness(self, *arguments: str) -> tuple[dict[str, object], str]:
        code, result, text = invoke(*self.command, *arguments)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["action"], "work-readiness")
        return result["readiness"], text

    @staticmethod
    def _argvs(report: dict[str, object]) -> list[list[str]]:
        found: list[list[str]] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith("_argv") or key.endswith("_argv_template") or key == "report_template":
                        if child is not None:
                            assert isinstance(child, list)
                            found.append(child)
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(report)
        return found

    def test_rooted_deterministic_pages_and_read_only_drilldowns(self) -> None:
        before = self.store.path.read_bytes()
        report, first_bytes = self._readiness("--limit", "1", "--offset", "0")
        second, second_bytes = self._readiness("--limit", "1", "--offset", "0")
        self.assertEqual(report, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(self.store.path.read_bytes(), before)

        self.assertTrue(report["read_only"])
        self.assertFalse(report["claimability_evaluated"])
        self.assertEqual(report["goal_id"], "goal-1")
        for page in (
            report["remaining_structure"]["waves"],
            report["frontiers"]["ready_frontier"],
            report["frontiers"]["blocking_frontier"],
        ):
            self.assertEqual((page["limit"], page["offset"]), (1, 0))
            self.assertLessEqual(len(page["items"]), 1)
            self.assertEqual(page["next_offset"], 1 if page["total"] > 1 else None)

        resolved_root = str(self.root.resolve())
        for argv in self._argvs(report):
            with self.subTest(argv=argv):
                self.assertEqual(argv[:4], ["tasktra", argv[1], "--root", resolved_root])
                self.assertIn(argv[1], {"work", "intervention", "delegation"})
                concrete = [
                    {"{performer_id}": "operator", "{envelope_sha256}": "digest",
                     "{lease_seconds}": "300", "{token_reservation}": "0"}.get(item, item)
                    for item in argv
                ]
                build_parser().parse_args(concrete[1:])

        ready = report["frontiers"]["ready_frontier"]["items"]
        self.assertEqual(len(ready), 1)
        drilldowns = ready[0]["drilldowns"]
        for field in ("dependencies_argv", "impact_argv"):
            argv = drilldowns[field]
            code, result, _ = invoke(*argv[1:])
            self.assertEqual(code, 0, result)
            self.assertTrue(result["ok"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_offset_is_shared_request_but_each_page_keeps_its_own_metadata(self) -> None:
        report, _ = self._readiness("--limit", "1", "--offset", "1")
        for page in (
            report["remaining_structure"]["waves"],
            report["frontiers"]["ready_frontier"],
            report["frontiers"]["blocking_frontier"],
        ):
            self.assertEqual((page["limit"], page["offset"]), (1, 1))
            expected_next = 2 if page["total"] > 2 else None
            self.assertEqual(page["next_offset"], expected_next)

    def test_invalid_request_and_missing_runtime_fail_without_writes(self) -> None:
        before = self.store.path.read_bytes()
        for arguments in (("--limit", "0"), ("--limit", "101"), ("--offset", "-1"), ("--offset", "1000001")):
            with self.subTest(arguments=arguments):
                code, result, _ = invoke(*self.command, *arguments)
                self.assertEqual(code, 2, result)
                self.assertFalse(result["ok"])
        code, result, _ = invoke("work", "--root", str(self.root), "readiness", "missing")
        self.assertEqual(code, 2, result)
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.path.read_bytes(), before)

        empty = self.root / "empty project"
        (empty / ".tasktra").mkdir(parents=True)
        (empty / ".tasktra/project.toml").write_text(
            '[project]\nname="Empty"\nconfig_version=1\n[runtime]\ndatabase="state.sqlite"\n',
            encoding="utf-8",
        )
        before_files = sorted(path.relative_to(empty).as_posix() for path in empty.rglob("*"))
        code, result, _ = invoke("work", "--root", str(empty), "readiness", "goal-1")
        self.assertEqual(code, 2, result)
        self.assertFalse(result["ok"])
        self.assertEqual(sorted(path.relative_to(empty).as_posix() for path in empty.rglob("*")), before_files)


if __name__ == "__main__":
    unittest.main()
