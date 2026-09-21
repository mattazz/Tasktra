"""Coverage for the optional, plan-only Jira synchronization pack."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.compiler import compile_catalog, load_catalog
from tasktra.config import ConfigError, load_project_config
from tasktra.jira_sync import JiraSyncError, JiraSyncPolicy, build_sync_plan


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> tuple[int, dict]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class JiraSyncTests(unittest.TestCase):
    def policy(self) -> JiraSyncPolicy:
        return JiraSyncPolicy.from_mapping({
            "host": "acme.atlassian.net", "project": "PROJ",
            "claim_transition": "In Progress", "review_transition": "In Review",
            "complete_transition": "Done",
        })

    def test_plan_is_closed_deterministic_and_never_dispatches(self):
        first = build_sync_plan(
            policy=self.policy(), event="claimed", issue="PROJ-123",
            goal_id="goal-one", work_unit_id="unit-one",
        )
        second = build_sync_plan(
            policy=self.policy(), event="claimed", issue="PROJ-123",
            goal_id="goal-one", work_unit_id="unit-one",
        )
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["mutation"], "none")
        self.assertEqual(first["request"], {"issue": "PROJ-123", "transition": "In Progress"})
        self.assertEqual(first["descriptor"]["action"], "jira-sync-claimed")
        self.assertEqual(first["descriptor"]["resource_scope"]["host"], "acme.atlassian.net")

    def test_plan_rejects_cross_project_issue_and_unconfigured_review(self):
        with self.assertRaisesRegex(JiraSyncError, "jira_sync.project"):
            build_sync_plan(policy=self.policy(), event="claimed", issue="OTHER-1", goal_id="goal-one", work_unit_id="unit-one")
        policy = JiraSyncPolicy.from_mapping({
            "host": "acme.atlassian.net", "project": "PROJ",
            "claim_transition": "In Progress", "complete_transition": "Done",
        })
        with self.assertRaisesRegex(JiraSyncError, "review_transition"):
            build_sync_plan(policy=policy, event="review-ready", issue="PROJ-1", goal_id="goal-one", work_unit_id="unit-one")

    def test_policy_requires_explicit_pack_and_safe_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".tasktra" / "project.toml"
            config.parent.mkdir()
            config.write_text(
                """[project]\nname = \"Example\"\nconfig_version = 1\n\n[jira_sync]\nhost = \"acme.atlassian.net\"\nproject = \"PROJ\"\nclaim_transition = \"In Progress\"\ncomplete_transition = \"Done\"\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "optional jira-sync pack"):
                load_project_config(root)
            config.write_text(
                """[project]\nname = \"Example\"\nconfig_version = 1\n\n[packs]\nenabled = [\"core\", \"jira-sync\"]\n\n[jira_sync]\nhost = \"https://acme.atlassian.net\"\nproject = \"PROJ\"\nclaim_transition = \"In Progress\"\ncomplete_transition = \"Done\"\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "lowercase host name"):
                load_project_config(root)

    def test_pack_projection_is_opt_in(self):
        catalog = load_catalog(ROOT / "catalog")
        core = compile_catalog(catalog, ("core",))
        enabled = compile_catalog(catalog, ("jira-sync",))
        skill = PurePosixPath(".agents/skills/tasktra-jira-sync/SKILL.md")
        self.assertNotIn(skill, core.files)
        self.assertIn(skill, enabled.files)
        self.assertIn(PurePosixPath(".claude/skills/tasktra-jira-sync/SKILL.md"), enabled.files)

    def test_cli_returns_a_plan_only_for_a_configured_project(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".tasktra" / "project.toml"
            config.parent.mkdir()
            config.write_text(
                """[project]\nname = \"Example\"\nconfig_version = 1\n\n[packs]\nenabled = [\"core\", \"jira-sync\"]\n\n[jira_sync]\nhost = \"acme.atlassian.net\"\nproject = \"PROJ\"\nclaim_transition = \"In Progress\"\ncomplete_transition = \"Done\"\n""",
                encoding="utf-8",
            )
            code, output = run_cli(
                "jira-sync", "--root", str(root), "plan", "--event", "completed",
                "--issue", "PROJ-42", "--goal-id", "goal-one", "--work-unit-id", "unit-one",
            )
            self.assertEqual(code, 0)
            self.assertEqual(output["action"], "jira-sync-plan")
            self.assertEqual(output["mutation"], "none")
            self.assertEqual(output["request"]["transition"], "Done")

