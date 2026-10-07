from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from tasktra.portal_insights import _read_build_metadata, build_insights, validation_summary


class PortalInsightsTests(unittest.TestCase):
    def test_exact_work_targets_and_plans_are_not_measurement_gaps(self):
        snapshot = {"runtime": {"available": True, "emergency_stopped": False}, "summary": {"agents": 3}, "warnings": [], "jobs": [], "agents": [
            {"id": "same", "work_id": "run-a", "job_id": "job-a", "state": "failed", "model": None, "total_tokens": None},
            {"id": "same", "work_id": "run-b", "job_id": "job-b", "state": "started", "model": None, "total_tokens": None},
            {"id": "plan", "work_id": "planned", "job_id": "job-c", "state": "planned", "model": None, "total_tokens": None},
        ]}
        with TemporaryDirectory() as directory:
            items = {item["id"]: item for item in build_insights(snapshot, Path(directory), goal_id=None, expected_schema_version=12)["attention"]["items"]}
        self.assertEqual(items["missing-usage:run-a"]["target"], {"type": "agent", "id": "run-a"})
        self.assertIn("missing-usage:run-b", items)
        self.assertIn("historical-agent-failure:run-a", items)
        self.assertNotIn("missing-usage:planned", items)

    def test_validation_projection_excludes_private_report_content_and_rejects_malformed_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".tasktra" / "runtime" / "validation" / "latest.json"
            path.parent.mkdir(parents=True)
            report = {"kind": "tasktra.validation-report", "version": 1, "report_id": "one", "status": "failed", "started_at": "2026-10-07T10:00:00Z", "finished_at": "2026-10-07T10:00:01Z", "partial": False, "checks": [{"index": 0, "status": "failed", "exit_code": 1, "elapsed_ms": 1, "argv": ["secret"], "stdout": "secret", "stderr": "secret", "stdout_truncated": False, "stderr_truncated": False}]}
            path.write_text(json.dumps(report), encoding="utf-8")
            public = validation_summary(root)
            self.assertTrue(public["available"])
            self.assertNotIn("secret", json.dumps(public))
            report["status"] = []
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertFalse(validation_summary(root)["available"])
            report["status"] = "passed"
            report["checks"][0]["status"] = "passed"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertFalse(validation_summary(root)["available"])
            report["checks"][0]["exit_code"] = 0
            report["checks"][0]["stdout_truncated"] = True
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertFalse(validation_summary(root)["available"])

    def test_packaged_build_metadata_is_allowlisted_and_source_falls_back(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "BUILD.json"
            self.assertEqual(_read_build_metadata(build, "source-version"), {"version": "source-version", "commit": None})
            build.write_text(json.dumps({"version": "1.2.3", "commit": "a" * 40, "private": "ignored"}), encoding="utf-8")
            self.assertEqual(_read_build_metadata(build, "source-version"), {"version": "1.2.3", "commit": "a" * 40})
            build.write_text(json.dumps({"version": "1.2.3", "commit": "not-a-commit"}), encoding="utf-8")
            self.assertEqual(_read_build_metadata(build, "source-version")["commit"], None)


if __name__ == "__main__":
    unittest.main()
