"""Operator-facing draining commands and bounded inspection."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import unittest

from tasktra.autonomy import LOCAL_REVERSIBLE_WRITE
from tasktra.cli import main
from tasktra.overview import format_overview
from tests import test_stage3_autonomy as stage3


def invoke(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class GoalDrainingCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stage3.AutonomyTests()
        self.fixture._initialize({"attempts": 10, "elapsed_seconds": 600, "concurrency": 2})
        self.addCleanup(self.fixture.tearDown)
        self.store, self.digest = self.fixture.store, self.fixture.digest
        self.root = Path(self.fixture.directory.name)
        profile = self.root / ".tasktra/project.toml"
        profile.parent.mkdir()
        profile.write_text('[project]\nname="Drain"\nconfig_version=1\n[runtime]\ndatabase="state.sqlite"\n', encoding="utf-8")
        self.command = ("goal", "--root", str(self.root))
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="unit-2", title="private worker title",
            scope={"paths": ["src/tasktra"], "exclusions": []},
        )
        self.store.record_transition_approval(
            goal_id="goal-1", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="steward", approver_kind="steward",
            performer_id="worker", valid_until=stage3.NOW + timedelta(days=1), at=stage3.NOW,
        )

    def claim(self):
        return self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work",
            token_reservation=3, lease_seconds=30, at=stage3.NOW,
        )

    def test_paginated_preview_is_read_only_and_excludes_secrets(self):
        claims = [self.claim(), self.claim()]
        before = self.store.path.read_bytes()
        code, response = invoke(*self.command, "drain", "goal-1", "--preview", "--limit", "1")
        self.assertEqual(code, 0)
        first = response["drain"]
        self.assertTrue(first["read_only"])
        self.assertEqual((first["total"], first["next_offset"], first["stored_leases"]), (2, 1, 2))
        code, response = invoke(*self.command, "drain", "goal-1", "--preview", "--limit", "1", "--offset", "1")
        self.assertEqual(code, 0)
        second = response["drain"]
        self.assertEqual(first["lease_set_sha256"], second["lease_set_sha256"])
        self.assertIsNone(second["next_offset"])
        self.assertNotEqual(first["attempts"], second["attempts"])
        rendered = json.dumps([first, second])
        self.assertNotIn("private worker title", rendered)
        for claim in claims:
            self.assertNotIn(claim["lease_token"], rendered)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_apply_exposes_draining_in_list_and_overview(self):
        self.claim()
        code, response = invoke(*self.command, "drain", "goal-1", "--apply", "--actor", "operator")
        self.assertEqual(code, 0)
        self.assertEqual(response["drain"]["status_after"], "draining")
        code, response = invoke(*self.command, "list", "--status", "draining")
        self.assertEqual(code, 0)
        self.assertEqual([goal["id"] for goal in response["goals"]], ["goal-1"])
        code, response = invoke("overview", "--root", str(self.root), "--goal-id", "goal-1", "--json")
        self.assertEqual(code, 0)
        report = response
        self.assertEqual(report["goal"]["intake"], {"accepting_claims": False, "draining": True})
        self.assertIn("goal-draining", [item["code"] for item in report["goal"]["attention"]])
        self.assertIn("1 current leases to finish or recover", format_overview(response))

    def test_invalid_controls_leave_runtime_unchanged(self):
        before = self.store.path.read_bytes()
        for arguments in (("--apply",), ("--preview", "--limit", "0"), ("--preview", "--offset", "-1")):
            with self.subTest(arguments=arguments):
                code, response = invoke(*self.command, "drain", "goal-1", *arguments)
                self.assertNotEqual(code, 0)
                self.assertFalse(response["ok"])
        for arguments in ((), ("--preview", "--apply")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                invoke(*self.command, "drain", "goal-1", *arguments)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_empty_goal_pauses_immediately(self):
        code, response = invoke(*self.command, "drain", "goal-1", "--apply", "--actor", "operator")
        self.assertEqual(code, 0)
        self.assertEqual(response["drain"]["status_after"], "paused")


if __name__ == "__main__":
    unittest.main()
