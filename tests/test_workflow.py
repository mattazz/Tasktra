import unittest

from tasktra.handoffs import HANDOFF_KIND, HANDOFF_VERSION
from tasktra.workflow import (
    WorkflowError,
    accept_handoff,
    is_workflow_complete,
    new_workflow,
    serialize_workflow,
    validate_workflow_completion_token,
    workflow_completion_token,
)


SOURCE = {"goal_id": "stage-two", "work_unit_id": "workflow"}


def handoff(identifier, *, status="completed", source=None, role="implementer", actor_id=None):
    return {
        "kind": HANDOFF_KIND, "version": HANDOFF_VERSION, "handoff_id": identifier,
        "source": source or SOURCE, "human_summary": "Completed bounded work.",
        "producer": {"role": role, "actor_id": actor_id or f"{role}-one"},
        "status": {"state": status, "summary": f"Result is {status}."},
        "verified_facts": [{"statement": "Focused check completed.", "evidence_ids": ["check"]}],
        "inferences": [], "changed_paths": [],
        "validation_results": [{"name": "check", "outcome": "passed" if status == "completed" else "failed", "detail": "Recorded result.", "evidence_ids": ["check"]}],
        "evidence_refs": [{"id": "check", "kind": "command", "locator": "python -m unittest", "summary": "Focused check."}],
        "blockers": [],
        "downstream_brief": {"objective": "Continue the workflow.", "context": [], "constraints": [], "recommended_next_steps": []},
        "requested_actions": [],
    }


class WorkflowTests(unittest.TestCase):
    def test_completed_handoffs_advance_implement_test_review_and_finish(self):
        state = new_workflow(SOURCE)
        state = accept_handoff(state, handoff("implement-result"))
        self.assertEqual((state["current_role"], state["status"]), ("tester", "ready"))
        state = accept_handoff(state, handoff("test-result", role="tester"))
        self.assertEqual((state["current_role"], state["status"]), ("reviewer", "ready"))
        state = accept_handoff(state, handoff("review-result", role="reviewer"))
        self.assertEqual((state["current_role"], state["status"]), (None, "completed"))
        self.assertEqual([item["from_role"] for item in state["transitions"]], ["implementer", "tester", "reviewer"])
        self.assertEqual(serialize_workflow(state), serialize_workflow(state))
        self.assertTrue(is_workflow_complete(state))
        token = workflow_completion_token(state)
        self.assertEqual(validate_workflow_completion_token(state, token), token)

    def test_invalid_or_cross_source_handoffs_cannot_advance_workflow(self):
        state = new_workflow(SOURCE)
        invalid = handoff("bad")
        invalid["verified_facts"][0]["evidence_ids"] = ["missing"]
        with self.assertRaisesRegex(WorkflowError, "unknown evidence"):
            accept_handoff(state, invalid)
        with self.assertRaisesRegex(WorkflowError, "source"):
            accept_handoff(state, handoff("other", source={"goal_id": "other", "work_unit_id": "workflow"}))
        self.assertEqual(state["current_role"], "implementer")

    def test_workflow_source_schema_rejects_double_and_trailing_hyphens(self):
        for source in (
            {"goal_id": "two--dash", "work_unit_id": "workflow"},
            {"goal_id": "stage-two", "work_unit_id": "trailing-"},
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(WorkflowError, "does not match pattern|identifier"):
                    new_workflow(source)

    def test_failed_result_keeps_full_evidence_and_status_without_advancing(self):
        state = accept_handoff(new_workflow(SOURCE), handoff("failed-implementation", status="failed"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["current_role"], None)
        self.assertEqual(state["accepted_handoffs"][0]["evidence_refs"][0]["id"], "check")
        with self.assertRaisesRegex(WorkflowError, "not ready"):
            accept_handoff(state, handoff("retry"))

    def test_tampered_transition_history_is_rejected_before_acceptance(self):
        state = accept_handoff(new_workflow(SOURCE), handoff("implement-result"))
        state["transitions"][0]["to_role"] = "reviewer"
        with self.assertRaisesRegex(WorkflowError, "target"):
            accept_handoff(state, handoff("test-result", role="tester"))

    def test_wrong_producer_role_cannot_advance_a_stage(self):
        state = accept_handoff(new_workflow(SOURCE), handoff("implement-result"))
        with self.assertRaisesRegex(WorkflowError, "producer"):
            accept_handoff(state, handoff("wrong-role", role="reviewer"))

    def test_reviewer_must_be_independent_from_implementation_and_test(self):
        state = accept_handoff(new_workflow(SOURCE), handoff("implement-result", actor_id="implementer-one"))
        state = accept_handoff(state, handoff("test-result", role="tester", actor_id="tester-one"))
        with self.assertRaisesRegex(WorkflowError, "implementation actor"):
            accept_handoff(state, handoff("review-result", role="reviewer", actor_id="implementer-one"))
        with self.assertRaisesRegex(WorkflowError, "tester actor"):
            accept_handoff(state, handoff("review-result", role="reviewer", actor_id="tester-one"))

    def test_completion_token_cannot_be_issued_or_replayed_for_tampered_state(self):
        state = new_workflow(SOURCE)
        self.assertFalse(is_workflow_complete(state))
        with self.assertRaisesRegex(WorkflowError, "not eligible"):
            workflow_completion_token(state)
        state = accept_handoff(state, handoff("implement-result"))
        state = accept_handoff(state, handoff("test-result", role="tester"))
        state = accept_handoff(state, handoff("review-result", role="reviewer"))
        token = workflow_completion_token(state)
        token["workflow_sha256"] = "0" * 64
        with self.assertRaisesRegex(WorkflowError, "does not match"):
            validate_workflow_completion_token(state, token)
        forged = new_workflow(SOURCE)
        forged["status"] = "completed"
        self.assertFalse(is_workflow_complete(forged))

    def test_workflow_outputs_do_not_alias_inputs(self):
        source = dict(SOURCE)
        state = new_workflow(source)
        source["goal_id"] = "other-goal"
        self.assertEqual(state["source"]["goal_id"], "stage-two")
        result = handoff("implement-result")
        advanced = accept_handoff(state, result)
        advanced["accepted_handoffs"][0]["producer"]["actor_id"] = "changed"
        self.assertEqual(result["producer"]["actor_id"], "implementer-one")
