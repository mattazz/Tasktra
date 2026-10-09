from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra import diagnostics
from tasktra.cli import main
from tasktra.state import SCHEMA_VERSION


def run_cli(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(arguments)
    return result, json.loads(stdout.getvalue() or stderr.getvalue())


class RuntimeProvenanceTests(unittest.TestCase):
    def source_project(self, root):
        package = root / "src/tasktra"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text('[project]\nname = "tasktra"\n', encoding="utf-8")
        return package

    def test_matching_checkout_and_foreign_editable_install(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.source_project(root)
            with patch.object(diagnostics, "__file__", str(package / "diagnostics.py")):
                report = diagnostics.runtime_provenance(root)
            self.assertTrue(report["source_matches_project"])
            self.assertEqual(report["supported_runtime_schema"], SCHEMA_VERSION)
            self.assertTrue(report["python_executable"])
            report = diagnostics.runtime_provenance(root)
            self.assertFalse(report["source_matches_project"])
            self.assertNotEqual(report["package_path"], report["project_source_path"])

    def test_installed_package_targeting_other_project_is_valid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            (root / "pyproject.toml").write_text('[project]\nname = "my-app"\n', encoding="utf-8")
            self.assertIsNone(diagnostics.runtime_provenance(root)["source_matches_project"])
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertIsNone(report["provenance"]["source_matches_project"])

    def test_installed_package_can_inspect_a_tasktra_source_checkout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            with patch.object(diagnostics, "__file__", str(root / "installed/tasktra/diagnostics.py")):
                code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertIsNone(report["provenance"]["source_matches_project"])
            self.assertEqual(report["provenance"]["package_kind"], "installed-package")
            self.assertFalse(report["provenance"]["foreign_source_checkout"])
            self.assertIsNone(report["provenance"]["loaded_source_checkout"])
            self.assertIn("installed or copied", next(check["detail"] for check in report["checks"] if check["name"] == "runtime_source"))

    def test_ambiguous_foreign_source_layout_is_not_treated_as_installed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            with patch.object(diagnostics, "__file__", str(root / "foreign/src/tasktra/diagnostics.py")):
                code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 1)
            self.assertEqual(report["provenance"]["package_kind"], "source-layout-unknown")
            self.assertFalse(report["provenance"]["source_matches_project"])
            self.assertTrue(report["provenance"]["foreign_source_checkout"])

    def test_doctor_reports_foreign_build_without_changing_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            before = database.read_bytes()
            files = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 1)
            self.assertFalse(report["ok"])
            source = next(check for check in report["checks"] if check["name"] == "runtime_source")
            self.assertFalse(source["ok"])
            self.assertIn("PYTHONPATH", source["detail"])
            self.assertEqual(report["provenance"]["database_runtime_schema"], SCHEMA_VERSION)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob("*")), files)

    def test_missing_project_still_reports_provenance_without_creating_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "absent"
            code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 1)
            self.assertIn("package_path", report["provenance"])
            self.assertFalse(root.exists())

    def test_schema_mismatch_guidance_respects_build_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            with patch("tasktra.cli.StateStore.inspect_schema_version", return_value=SCHEMA_VERSION - 1):
                code, report = run_cli("doctor", "--root", str(root))
            detail = next(check["detail"] for check in report["checks"] if check["name"] == "runtime_state")
            self.assertEqual(code, 1)
            self.assertIn("resolve the runtime_source mismatch", detail)
            (root / "pyproject.toml").write_text('[project]\nname = "consumer"\n', encoding="utf-8")
            with patch("tasktra.cli.StateStore.inspect_schema_version", return_value=SCHEMA_VERSION + 1):
                code, report = run_cli("doctor", "--root", str(root))
            detail = next(check["detail"] for check in report["checks"] if check["name"] == "runtime_state")
            self.assertEqual(code, 1)
            self.assertIn("newer than supported", detail)
            self.assertNotIn("migration to", detail)

    def test_wal_diagnostics_preserve_database_and_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                count = connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
            connection.close()
            before = database.read_bytes()
            code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertEqual(report["provenance"]["database_runtime_schema"], SCHEMA_VERSION)
            self.assertEqual(database.read_bytes(), before)
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM audit_events").fetchone()[0], count)
            connection.close()

    def test_malformed_pyproject_does_not_hide_runtime_diagnostics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.source_project(root)
            for content in ('[project', 'project = "unexpected"'):
                with self.subTest(content=content):
                    (root / "pyproject.toml").write_text(content, encoding="utf-8")
                    self.assertIsNone(diagnostics.runtime_provenance(root)["source_matches_project"])

    def test_corrupt_database_is_an_actionable_diagnostic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            database.write_bytes(b"not a sqlite database")
            code, report = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 1)
            self.assertFalse(report["ok"])
            self.assertEqual(report["checks"][-1]["name"], "runtime_state")
            self.assertEqual(database.read_bytes(), b"not a sqlite database")


if __name__ == "__main__":
    unittest.main()
