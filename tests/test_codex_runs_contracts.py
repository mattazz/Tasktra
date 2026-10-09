from __future__ import annotations

import unittest

from tasktra.codex_runs import CodexRunError, task_name, validate_finish


class CodexRunContractTests(unittest.TestCase):
    def test_requested_task_name_is_collaboration_safe_and_deterministic(self) -> None:
        value = task_name(attempt_id="attempt-a", run_no=1, requested_role="backend-api-specialist")
        self.assertRegex(value, r"^[a-z][a-z0-9_]*$")
        self.assertEqual(value, task_name(attempt_id="attempt-a", run_no=1, requested_role="backend-api-specialist"))

    def test_unavailable_usage_rejects_partial_counters(self) -> None:
        with self.assertRaises(CodexRunError):
            validate_finish(outcome="failed", result_status="unavailable", result_sha256=None,
                            usage_status="unavailable", input_tokens=1, output_tokens=None)

    def test_completed_requires_observed_digest(self) -> None:
        with self.assertRaises(CodexRunError):
            validate_finish(outcome="completed", result_status="unavailable", result_sha256=None,
                            usage_status="unavailable", input_tokens=None, output_tokens=None)

