from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.workspace import (
    GitCommandResult,
    WorkspaceRequest,
    assess_workspace,
    recommend_workspace,
)


def _git_result(*, branch="main", dirty="", git_dir=".git", common_dir=".git"):
    def fake(root, args):
        responses = {
            ("--version",): GitCommandResult(0, "git version 2.45.0\n"),
            ("rev-parse", "--show-toplevel"): GitCommandResult(0, f"{root}\n"),
            ("symbolic-ref", "--quiet", "--short", "HEAD"): GitCommandResult(0, f"{branch}\n"),
            ("rev-parse", "HEAD"): GitCommandResult(0, "abc123\n"),
            ("status", "--porcelain=v1", "--untracked-files=all", "-z"): GitCommandResult(0, dirty),
            ("rev-parse", "--path-format=absolute", "--git-dir"): GitCommandResult(0, f"{git_dir}\n"),
            ("rev-parse", "--path-format=absolute", "--git-common-dir"): GitCommandResult(0, f"{common_dir}\n"),
        }
        return responses[args]
    return fake


class WorkspaceAssessmentTests(unittest.TestCase):
    def test_assesses_clean_repository_and_recommends_existing_checkout(self):
        with TemporaryDirectory() as directory, patch("tasktra.workspace._run_git", _git_result()):
            assessment = assess_workspace(directory)
        self.assertTrue(assessment.git_available)
        self.assertTrue(assessment.is_repository)
        self.assertEqual(assessment.branch, "main")
        self.assertEqual(assessment.head_revision, "abc123")
        self.assertFalse(assessment.dirty)
        self.assertFalse(assessment.has_untracked)
        self.assertFalse(assessment.linked_worktree)
        self.assertEqual(assessment.worktree_identity, "main")
        recommendation = recommend_workspace(assessment, WorkspaceRequest())
        self.assertEqual(recommendation.strategy, "existing-checkout")

    def test_dirty_concurrent_work_isolated_and_unknown_overlap_escalates(self):
        with TemporaryDirectory() as directory, patch(
            "tasktra.workspace._run_git", _git_result(dirty=" M src/app.py\0?? notes.txt\0")
        ):
            assessment = assess_workspace(directory)
        self.assertTrue(assessment.dirty)
        self.assertTrue(assessment.has_untracked)
        self.assertEqual(
            recommend_workspace(assessment, WorkspaceRequest(concurrent_workers=2)).strategy,
            "isolated-worktree",
        )
        self.assertEqual(recommend_workspace(assessment, WorkspaceRequest()).strategy, "escalation")
        self.assertEqual(
            recommend_workspace(assessment, WorkspaceRequest(paths_known_disjoint=True)).strategy,
            "existing-checkout",
        )

    def test_linked_worktree_identity_and_detached_head_are_visible(self):
        def detached(root, args):
            if args == ("symbolic-ref", "--quiet", "--short", "HEAD"):
                return GitCommandResult(1)
            return _git_result(git_dir="/common/worktrees/feature", common_dir="/common")(root, args)

        with TemporaryDirectory() as directory, patch("tasktra.workspace._run_git", detached):
            assessment = assess_workspace(directory)
        self.assertTrue(assessment.detached)
        self.assertTrue(assessment.linked_worktree)
        self.assertEqual(assessment.worktree_identity, "feature")
        self.assertEqual(recommend_workspace(assessment, WorkspaceRequest()).strategy, "escalation")

    def test_missing_git_and_non_repo_are_visible_but_do_not_block_local_work(self):
        with TemporaryDirectory() as directory, patch("tasktra.workspace._run_git", return_value=None):
            missing = assess_workspace(directory)
        self.assertFalse(missing.git_available)
        self.assertTrue(missing.local_work_can_continue)
        self.assertEqual(recommend_workspace(missing, WorkspaceRequest()).strategy, "existing-checkout")
        self.assertEqual(
            recommend_workspace(missing, WorkspaceRequest(change_scope="substantial")).strategy,
            "escalation",
        )

        def non_repo(root, args):
            if args == ("--version",):
                return GitCommandResult(0, "git version 2\n")
            return GitCommandResult(128, "", "not a repository")

        with TemporaryDirectory() as directory, patch("tasktra.workspace._run_git", non_repo):
            assessment = assess_workspace(directory)
        self.assertTrue(assessment.git_available)
        self.assertFalse(assessment.is_repository)
        self.assertTrue(assessment.local_work_can_continue)

    def test_invalid_request_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkspaceRequest(concurrent_workers=0)
        with self.assertRaises(ValueError):
            WorkspaceRequest(change_scope="huge")
