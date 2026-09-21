"""End-to-end proof that approved lessons use normal canonical compiler gates."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.config import initialize_project
from tasktra.lessons import LessonProposalStore
from tasktra.validation import run_validations


ROOT = Path(__file__).resolve().parents[1]


def invoke(*arguments: str) -> int:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return main(arguments)


class LessonPromotionWorkflowTests(unittest.TestCase):
    def test_approved_plan_updates_canonical_source_then_regenerates_through_drift_gates(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            shutil.copytree(ROOT / "catalog", project / "catalog")
            initialize_project(project, name="lesson promotion fixture")
            self.assertEqual(invoke("compile", "--root", str(project), "--trust-catalog"), 0)

            store = LessonProposalStore(project)
            proposal = store.create(
                proposal_id="promotion-proof", author="author", problem="A reusable instruction is missing.",
                general_principle="Promote reviewed lessons through canonical sources and compiler gates.",
                evidence=[{"kind": "test", "ref": "tests/promotion.json", "sha256": "a" * 64}],
                affected_contracts=[{
                    "contract_id": "skill.tasktra-init",
                    "canonical_source": "catalog/skills/tasktra-init.md",
                    "change": "Add the independently reviewed promotion proof marker.",
                }],
                applicability={"contexts": ["Tasktra adoption"], "constraints": ["Independent review"]},
                risks=["An unreviewed edit could weaken adoption safety."],
                regression_checks=[{
                    "id": "promotion-regression", "description": "The promoted source remains valid.",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                }],
            )
            reviewed = store.transition(proposal.id, expected_version=1, to_status="reviewed", actor="reviewer")
            approved = store.transition(
                proposal.id, expected_version=reviewed.version, to_status="approved", actor="reviewer",
                reason="Evidence and applicability are sufficient.",
            )
            plan = store.promotion_plan(approved.id)
            self.assertTrue(plan["applies_no_changes"])

            source = project / plan["canonical_changes"][0]["canonical_source"]
            source.write_text(
                source.read_text(encoding="utf-8") + "\nPromotion proof: independently reviewed.\n",
                encoding="utf-8",
            )
            self.assertEqual(invoke("compile", "--root", str(project), "--check", "--trust-catalog"), 1)
            self.assertEqual(invoke("compile", "--root", str(project), "--trust-catalog"), 0)
            self.assertEqual(invoke("compile", "--root", str(project), "--check", "--trust-catalog"), 0)

            checks = tuple(tuple(item) for item in plan["regression_checks"][0:1] for item in [item["argv"]])
            results = run_validations(project, checks)
            self.assertTrue(all(item.status == "passed" for item in results))
            generated = project / ".agents" / "skills" / "tasktra-init" / "SKILL.md"
            self.assertIn("Promotion proof: independently reviewed.", generated.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
