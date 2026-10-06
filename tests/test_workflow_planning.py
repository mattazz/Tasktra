import unittest

from tasktra.workflow_planning import WorkflowPlanningError, plan_verification_policy


class WorkflowPlanningTests(unittest.TestCase):
    def test_deterministic_implementation_requires_explicit_policy_and_records_checks(self):
        plan = plan_verification_policy(
            work_type="implementation", deterministic_acceptance_available=True,
            independent_review_required=True, exploratory_tests_required=False,
            allowed_policies={"implementation-deterministic-review", "implementation-review"},
        )
        self.assertEqual(plan["verification_policy"], "implementation-deterministic-review")
        self.assertEqual(plan["stages"], ["implementer", "reviewer"])
        self.assertEqual(
            plan["mandatory_checks"],
            ["configured-deterministic-acceptance", "independent-review"],
        )

    def test_exploratory_requirement_rejects_shorter_or_weaker_policy(self):
        with self.assertRaisesRegex(WorkflowPlanningError, "implementation-review is required"):
            plan_verification_policy(
                work_type="implementation", deterministic_acceptance_available=True,
                independent_review_required=True, exploratory_tests_required=True,
                allowed_policies={"implementation-deterministic-review"},
            )

    def test_deterministic_direct_cannot_bypass_required_independent_review(self):
        with self.assertRaisesRegex(WorkflowPlanningError, "weaker than independent_review_required"):
            plan_verification_policy(
                work_type="deterministic", deterministic_acceptance_available=True,
                independent_review_required=True, exploratory_tests_required=False,
                allowed_policies={"deterministic-direct"},
            )

    def test_unsupported_policy_and_non_boolean_requirements_are_rejected(self):
        with self.assertRaisesRegex(WorkflowPlanningError, "unsupported verification policy"):
            plan_verification_policy(
                work_type="implementation", deterministic_acceptance_available=False,
                independent_review_required=False, exploratory_tests_required=False,
                allowed_policies={"invented-policy"},
            )
        with self.assertRaisesRegex(WorkflowPlanningError, "must be a boolean"):
            plan_verification_policy(
                work_type="implementation", deterministic_acceptance_available=1,  # type: ignore[arg-type]
                independent_review_required=False, exploratory_tests_required=False,
                allowed_policies={"implementation-review"},
            )
