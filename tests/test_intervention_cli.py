from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from tasktra import interventions
from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import AUTHORITY_ENVELOPE_KIND, AUTHORITY_ENVELOPE_VERSION, authority_envelope_sha256
from tasktra.cli import build_parser, main


def run_cli(*arguments: str):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def request():
    return {
        "kind": "tasktra.intervention-request", "version": 1, "request_id": "request-one",
        "source": {"goal_id": "goal-one", "work_unit_id": "unit-one", "attempt_id": "attempt-one"},
        "producer": {"actor_id": "worker-one"}, "outcome_class": "blocked",
        "prompt": "Choose the target.", "rationale": "The target is not recorded.", "impact": "The unit cannot continue.",
        "requires_human_approval": False, "evidence_refs": [],
    }


def response():
    return {
        "kind": "tasktra.intervention-response", "version": 1, "response_id": "response-one",
        "request": {"request_id": "request-one", "request_sha256": "a" * 64}, "expected_current_response": None,
        "responder": {"kind": "human", "actor_id": "operator-one"}, "disposition": "answered",
        "answer": "Use the reviewed target.", "rationale": "", "evidence_refs": [],
    }


def handoff():
    return {
        "kind": "tasktra.handoff", "version": 1, "handoff_id": "blocked-work", "source": {"goal_id": "goal-one", "work_unit_id": "unit-one"},
        "producer": {"role": "implementer", "actor_id": "worker-one"}, "human_summary": "Selected fields only.",
        "status": {"state": "blocked", "summary": "Waiting."}, "verified_facts": [], "inferences": [], "changed_paths": [], "validation_results": [], "evidence_refs": [],
        "blockers": [{"id": "target-blocker", "severity": "blocking", "summary": "Target is missing.", "next_action": "Select one target."}],
        "downstream_brief": {"objective": "Not copied.", "context": [], "constraints": [], "recommended_next_steps": []},
        "requested_actions": [{"id": "choose-target", "action": "Choose the target.", "rationale": "Worker cannot infer it.", "requires_human_approval": False}],
    }


def runtime_envelope():
    return {
        "kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION,
        "goal_id": "goal-one", "outcome": "Exercise the isolated CLI flow.", "motivation": "Test.", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done."}],
        "scope": {"paths": ["."], "exclusions": []},
        "allowed_actions": ["goal-activate", "work-claim", "work-complete", "work-requeue", "effect-write", "goal-complete"],
        "allowed_effects": [LOCAL_REVERSIBLE_WRITE], "prohibited_actions": [], "quality_requirements": ["Test."],
        "budgets": {"tokens": 20, "attempts": 2, "elapsed_seconds": 60, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": ["Stop."], "escalation_conditions": ["Escalate."],
    }


class InterventionCliTests(unittest.TestCase):
    def test_parser_exposes_closed_intervention_and_explicit_requeue_inputs(self):
        parser = build_parser()
        parsed = parser.parse_args(("intervention", "--root", "project", "responses", "request-one", "--after-revision", "3", "--limit", "5"))
        self.assertEqual((parsed.command, parsed.intervention_command, parsed.request_id, parsed.after_revision), ("intervention", "responses", "request-one", 3))
        requeue = parser.parse_args(("work", "requeue", "unit-one", "--actor", "worker-one", "--envelope-sha256", "a" * 64, "--evidence-json", "evidence.json", "--request-id", "request-one", "--response-id", "response-one", "--response-sha256", "b" * 64))
        self.assertEqual((requeue.request_id, requeue.response_id, requeue.response_sha256), ("request-one", "response-one", "b" * 64))

    def test_request_from_handoff_is_pure_and_returns_request_outer_key(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"; path.write_text(json.dumps(handoff()), encoding="utf-8")
            with patch("tasktra.cli._autonomy", side_effect=AssertionError("must stay pure")):
                code, payload = run_cli("intervention", "request-from-handoff", str(path), "--request-id", "request-one", "--attempt-id", "attempt-one", "--blocker-id", "target-blocker", "--action-id", "choose-target")
        self.assertEqual((code, payload["action"], payload["request"]["request_id"]), (0, "intervention-request-from-handoff", "request-one"))

    def test_yield_and_requeue_forward_all_runtime_bindings(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); request_path = root / "request.json"; request_path.write_text(json.dumps(request()), encoding="utf-8")
            evidence_path = root / "evidence.json"; evidence_path.write_text("{}", encoding="utf-8")
            store = Mock(); store.yield_for_intervention.return_value = {"request_id": "request-one"}; store.requeue_work.return_value = {"status": "eligible"}
            with patch("tasktra.cli._autonomy", return_value=store), patch.dict(os.environ, {"TASKTRA_LEASE_TOKEN": "lease-token"}):
                code, yielded = run_cli("work", "--root", str(root), "yield", "attempt-one", "--actor", "worker-one", "--request", str(request_path), "--tokens-consumed", "7", "--elapsed-ms", "11")
                code_requeue, requeued = run_cli("work", "--root", str(root), "requeue", "unit-one", "--actor", "worker-one", "--envelope-sha256", "a" * 64, "--evidence-json", str(evidence_path), "--request-id", "request-one", "--response-id", "response-one", "--response-sha256", "b" * 64)
        self.assertEqual((code, yielded["yield"]["request_id"], code_requeue, requeued["work_unit"]["status"]), (0, "request-one", 0, "eligible"))
        self.assertEqual(store.yield_for_intervention.call_args.kwargs["elapsed_ms"], 11)
        self.assertEqual(store.requeue_work.call_args.kwargs["expected_intervention_response_sha256"], "b" * 64)
        self.assertEqual(store.requeue_work.call_args.kwargs["intervention_request_id"], "request-one")

    def test_read_and_response_commands_forward_their_public_api(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); response_path = root / "response.json"; response_path.write_text(json.dumps(response()), encoding="utf-8")
            store = Mock(); store.record_intervention_response.return_value = {"response_id": "response-one"}
            inbox = Mock(return_value={"items": []})
            detail = Mock(return_value={"request": {"request_id": "request-one"}})
            history = Mock(return_value={"items": []})
            with patch("tasktra.cli._autonomy", return_value=store), patch.object(interventions, "intervention_inbox", inbox, create=True), patch.object(interventions, "intervention_detail", detail, create=True), patch.object(interventions, "intervention_response_history", history, create=True):
                code_list, listed = run_cli("intervention", "--root", str(root), "list", "--goal-id", "goal-one", "--no-legacy", "--limit", "7", "--offset", "2")
                code_show, shown = run_cli("intervention", "--root", str(root), "show", "request-one")
                code_history, responses = run_cli("intervention", "--root", str(root), "responses", "request-one", "--after-revision", "4")
                code_respond, responded = run_cli("intervention", "--root", str(root), "respond", str(response_path), "--actor", "operator-one", "--actor-kind", "human")
        self.assertEqual((code_list, listed["action"], code_show, shown["detail"]["request"]["request_id"], code_history, responses["action"], code_respond, responded["response"]["response_id"]), (0, "intervention-list", 0, "request-one", 0, "intervention-responses", 0, "response-one"))
        self.assertFalse(inbox.call_args.kwargs["include_legacy"])
        self.assertEqual(history.call_args.kwargs["after_revision"], 4)
        self.assertEqual(store.record_intervention_response.call_args.kwargs["responder_kind"], "human")

    def test_bad_contract_loader_returns_safe_cli_error_before_runtime(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad-handoff.json"; path.write_text('{"kind":"tasktra.handoff","kind":"tasktra.handoff"}', encoding="utf-8")
            with patch("tasktra.cli._autonomy", side_effect=AssertionError("must stay pure")):
                code, payload = run_cli("intervention", "request-from-handoff", str(path), "--request-id", "request-one", "--attempt-id", "attempt-one", "--blocker-id", "target-blocker", "--action-id", "choose-target")
        self.assertEqual(code, 2)
        self.assertIn("Duplicate JSON object key", payload["error"])

    def test_cli_flow_yields_reads_responds_and_requeues_with_reviewed_head(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); store = AutonomyStore(root / "state.sqlite")
            envelope = runtime_envelope(); digest = authority_envelope_sha256(envelope)
            valid_until = datetime.now(timezone.utc) + timedelta(days=1)
            store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done."])
            store.define_goal_contract("goal-one", envelope, actor_id="owner")
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="human", performer_id="owner", valid_until=valid_until)
            store.activate_goal("goal-one", actor_id="owner", envelope_sha256=digest)
            store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
            store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="steward", approver_kind="steward", performer_id="worker-one", valid_until=valid_until)
            claim = store.claim_next_work(goal_id="goal-one", performer_id="worker-one", envelope_sha256=digest, repository="repo", revision="revision", branch="main", workspace="workspace")
            self.assertIsNotNone(claim)
            request_value = request(); request_value["source"]["attempt_id"] = claim["attempt_id"]
            request_path = root / "request.json"; request_path.write_text(json.dumps(request_value), encoding="utf-8")
            with patch("tasktra.cli._autonomy", return_value=store), patch.dict(os.environ, {"TASKTRA_LEASE_TOKEN": claim["lease_token"]}):
                code_yield, yielded = run_cli("work", "--root", str(root), "yield", claim["attempt_id"], "--actor", "worker-one", "--request", str(request_path))
                code_inbox, inbox = run_cli("intervention", "--root", str(root), "list")
                code_detail, detail = run_cli("intervention", "--root", str(root), "show", "request-one")
                response_value = response(); response_value["request"]["request_sha256"] = yielded["yield"]["request_sha256"]
                response_path = root / "response.json"; response_path.write_text(json.dumps(response_value), encoding="utf-8")
                code_response, responded = run_cli("intervention", "--root", str(root), "respond", str(response_path), "--actor", "operator-one", "--actor-kind", "human")
                response_value['response_id'] = 'stale-answer'
                response_path.write_text(json.dumps(response_value), encoding='utf-8')
                code_conflict, conflict = run_cli('intervention', '--root', str(root), 'respond', str(response_path), '--actor', 'operator-one', '--actor-kind', 'human')
                store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-requeue", effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=digest, approver_id="steward", approver_kind="steward", performer_id="worker-one", valid_until=valid_until)
                evidence_path = root / "evidence.json"; evidence_path.write_text('{"decision":"approved"}', encoding="utf-8")
                code_requeue, requeued = run_cli("work", "--root", str(root), "requeue", "unit-one", "--actor", "worker-one", "--envelope-sha256", digest, "--evidence-json", str(evidence_path), "--request-id", "request-one", "--response-id", "response-one", "--response-sha256", responded["response"]["response_sha256"])
        self.assertEqual((code_yield, yielded["yield"]["request_id"], code_inbox, inbox["inbox"]["items"][0]["request_id"], code_detail, detail["detail"]["request"]["request_id"], code_response, responded["response"]["response_id"], code_requeue, requeued["work_unit"]["status"]), (0, "request-one", 0, "request-one", 0, "request-one", 0, "response-one", 0, "eligible"))
        self.assertEqual(code_conflict, 2)
        self.assertEqual(conflict['error_code'], 'response_head_changed')
        self.assertEqual(conflict['details']['current_response_id'], 'response-one')
        self.assertEqual(conflict['details']['current_response_sha256'], responded['response']['response_sha256'])
        self.assertNotIn(response_value['answer'], json.dumps(conflict))
