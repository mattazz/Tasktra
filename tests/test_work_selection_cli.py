from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.cli import main
from tests.test_stage3_autonomy import envelope


def invoke(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class WorkSelectionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.assertEqual(invoke("init", "--root", str(self.root), "--apply")[0], 0)
        self.database = self.root / ".tasktra/runtime/tasktra.sqlite"
        self.store = AutonomyStore(self.database)
        self.now = datetime.now(timezone.utc)
        self.store.create_goal(goal_id="goal-1", title="Service", description="Delivery", acceptance=["Done."])
        contract = envelope()
        self.digest = authority_envelope_sha256(contract)
        self.store.define_goal_contract("goal-1", contract, actor_id="owner", at=self.now)
        self.store.record_transition_approval(
            goal_id="goal-1", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="human", performer_id="owner",
            valid_until=self.now + timedelta(days=1), at=self.now,
        )
        self.store.activate_goal("goal-1", actor_id="owner", envelope_sha256=self.digest, at=self.now)
        for unit in ("alpha", "bravo"):
            self.store.create_work_unit(
                goal_id="goal-1", work_unit_id=unit, title=unit,
                scope={"paths": ["src/tasktra"], "exclusions": []},
            )
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="bravo", action="work-claim",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="steward", approver_kind="steward", performer_id="worker",
            valid_until=self.now + timedelta(days=1), at=self.now,
        )

    def explain(self, *extra, actor="worker"):
        return invoke(
            "work", "--root", str(self.root), "explain", "goal-1",
            "--actor", actor, "--envelope-sha256", self.digest, *extra,
        )

    def test_preview_is_read_only_and_selects_beyond_page_like_claim(self):
        before = self.database.read_bytes()
        code, output, error = self.explain("--limit", "1", "--lease-seconds", "10")
        self.assertEqual((code, error), (0, ""))
        response = json.loads(output)
        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "work-explain")
        report = response["explanation"]
        self.assertEqual(report["selected_work_unit_id"], "bravo")
        self.assertEqual(report["candidates"][0]["work_unit_id"], "alpha")
        self.assertIn("approval.unavailable", report["candidates"][0]["reason_codes"])
        self.assertEqual((report["total"], report["next_offset"]), (2, 1))
        code, output, error = self.explain("--limit", "1", "--offset", "1")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["explanation"]["candidates"][0]["work_unit_id"], "bravo")
        self.assertEqual(self.database.read_bytes(), before)
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=10, repository="repo", revision="revision", branch="main",
            workspace=str(self.root), lease_token="private-test-token-" * 3,
        )
        self.assertEqual(claim["work_unit_id"], report["selected_work_unit_id"])

    def test_unit_filter_does_not_change_selection_and_actor_is_respected(self):
        before = self.database.read_bytes()
        code, output, error = self.explain("--work-unit-id", "alpha")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)["explanation"]
        self.assertEqual(report["selected_work_unit_id"], "bravo")
        self.assertEqual([item["work_unit_id"] for item in report["candidates"]], ["alpha"])
        self.assertFalse(report["candidates"][0]["eligible"])
        code, output, error = self.explain(actor="another-worker")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)["explanation"]
        self.assertIsNone(report["selected_work_unit_id"])
        self.assertTrue(all("approval.unavailable" in item["reason_codes"] for item in report["candidates"]))
        self.assertEqual(self.database.read_bytes(), before)

    def test_invalid_requests_are_errors_without_mutation(self):
        before = self.database.read_bytes()
        for extra in (
            ("--limit", "0"), ("--limit", "101"), ("--offset", "-1"),
            ("--offset", "999999999999999999999"), ("--lease-seconds", "0"),
            ("--token-reservation", "-1"), ("--work-unit-id", "missing"),
        ):
            with self.subTest(extra=extra):
                code, output, error = self.explain(*extra)
                self.assertEqual((code, output), (2, ""))
                self.assertFalse(json.loads(error)["ok"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_missing_database_is_not_created_and_corruption_is_reported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tasktra").mkdir()
            (root / ".tasktra/project.toml").write_text(
                '[project]\nname="Example"\nconfig_version=1\n', encoding="utf-8",
            )
            arguments = (
                "work", "--root", str(root), "explain", "goal-1", "--actor", "worker",
                "--envelope-sha256", self.digest,
            )
            code, output, error = invoke(*arguments)
            self.assertEqual((code, output), (2, ""))
            self.assertFalse(json.loads(error)["ok"])
            self.assertFalse((root / ".tasktra/runtime").exists())
            database = root / ".tasktra/runtime/tasktra.sqlite"
            database.parent.mkdir()
            database.write_bytes(b"invalid SQLite database")
            code, output, error = invoke(*arguments)
            self.assertEqual((code, output), (2, ""))
            self.assertFalse(json.loads(error)["ok"])
            self.assertEqual(database.read_bytes(), b"invalid SQLite database")


if __name__ == "__main__":
    unittest.main()
