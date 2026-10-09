import copy
import json
import unittest

from tasktra.interventions import (
    InterventionError,
    canonical_intervention_request,
    intervention_request_sha256,
    load_intervention_request,
    load_intervention_response,
    request_from_handoff,
    validate_intervention_request,
    validate_intervention_response,
)


def request():
    return {
        "kind": "tasktra.intervention-request", "version": 1, "request_id": "request-one",
        "source": {"goal_id": "goal-one", "work_unit_id": "unit-one", "attempt_id": "attempt-one"},
        "producer": {"actor_id": "worker-one"}, "outcome_class": "blocked",
        "prompt": "Choose the target.", "rationale": "The target is not recorded.", "impact": "The unit cannot continue.",
        "requires_human_approval": False,
        "evidence_refs": [{"id": "test-run", "kind": "command", "locator": "python -m unittest tests.test_intervention_contracts", "summary": "Focused tests."}],
    }


def response():
    return {
        "kind": "tasktra.intervention-response", "version": 1, "response_id": "response-one",
        "request": {"request_id": "request-one", "request_sha256": "a" * 64}, "expected_current_response": None,
        "responder": {"kind": "human", "actor_id": "operator-one"}, "disposition": "answered",
        "answer": "Use the reviewed target.", "rationale": "It is the approved scope.", "evidence_refs": [],
    }


def handoff():
    return {
        "kind": "tasktra.handoff", "version": 1, "handoff_id": "blocked-work", "source": {"goal_id": "goal-one", "work_unit_id": "unit-one"},
        "producer": {"role": "implementer", "actor_id": "worker-one"}, "human_summary": "Only selected fields should cross this boundary.",
        "status": {"state": "blocked", "summary": "Waiting for a decision."}, "verified_facts": [], "inferences": [], "changed_paths": [], "validation_results": [],
        "evidence_refs": [{"id": "kept-evidence", "kind": "file", "locator": "docs/decision.md", "summary": "Decision record."}, {"id": "hidden-evidence", "kind": "note", "locator": "private context", "summary": "Unselected."}],
        "blockers": [{"id": "target-blocker", "severity": "blocking", "summary": "Target is missing.", "next_action": "Select one target."}],
        "downstream_brief": {"objective": "Should not leak.", "context": [], "constraints": [], "recommended_next_steps": []},
        "requested_actions": [{"id": "choose-target", "action": "Choose the target.", "rationale": "The worker cannot infer it.", "requires_human_approval": True}],
    }


class InterventionContractTests(unittest.TestCase):
    def test_valid_contracts_are_canonical_and_independent(self):
        accepted = validate_intervention_request(request())
        accepted["producer"]["actor_id"] = "changed"
        self.assertEqual(request()["producer"]["actor_id"], "worker-one")
        canonical = canonical_intervention_request(request())
        self.assertEqual(load_intervention_request(canonical), request())
        self.assertEqual(intervention_request_sha256(request()), intervention_request_sha256(json.loads(canonical)))
        self.assertEqual(validate_intervention_response(response())["disposition"], "answered")

    def test_loader_rejects_duplicate_keys_and_non_finite_values(self):
        with self.assertRaisesRegex(InterventionError, "Duplicate JSON object key"):
            load_intervention_request('{"kind":"tasktra.intervention-request","kind":"tasktra.intervention-request"}')
        with self.assertRaisesRegex(InterventionError, "Non-finite"):
            load_intervention_response('{"kind":NaN}')

    def test_request_is_closed_bounded_and_secret_safe(self):
        value = request(); value["unexpected"] = True
        with self.assertRaises(InterventionError): validate_intervention_request(value)
        value = request(); value["prompt"] = "bearer secret-value"
        with self.assertRaisesRegex(InterventionError, "credentials or secrets"): validate_intervention_request(value)
        for secret in ("secret=do-not-echo", "credential=do-not-echo"):
            value = request(); value["impact"] = secret
            with self.subTest(secret=secret), self.assertRaisesRegex(InterventionError, "credentials or secrets"):
                validate_intervention_request(value)
        value = request(); value["impact"] = "Read /private/workspace first."
        with self.assertRaisesRegex(InterventionError, "absolute path"): validate_intervention_request(value)
        value = request(); value["evidence_refs"][0].update({"kind": "file", "locator": "../secrets"})
        with self.assertRaises(InterventionError): validate_intervention_request(value)
        value = request(); value["evidence_refs"][0] = {"id": "test-run", "kind": "url", "locator": "https://example.invalid/path?token=value", "summary": "Unsafe."}
        with self.assertRaises(InterventionError): validate_intervention_request(value)
        value = request(); value["evidence_refs"][0] = {"id": "test-run", "kind": "url", "locator": "https://[broken", "summary": "Unsafe."}
        with self.assertRaises(InterventionError): validate_intervention_request(value)
        value = request(); value["evidence_refs"][0]["locator"] = "command\nnext"
        with self.assertRaisesRegex(InterventionError, "control characters"): validate_intervention_request(value)
        value = request(); value["not-a-secret"] = True
        with self.assertRaises(InterventionError) as caught:
            validate_intervention_request(value)
        self.assertNotIn("not-a-secret", str(caught.exception))
        value = request(); value["requires_human_approval"] = True
        with self.assertRaisesRegex(InterventionError, "match outcome_class"): validate_intervention_request(value)

    def test_response_head_shape_and_secret_checks(self):
        value = response(); value["expected_current_response"] = {"response_id": "old-head", "response_sha256": "B" * 64}
        with self.assertRaises(InterventionError): validate_intervention_response(value)
        value = response(); value["answer"] = "token=do-not-echo"
        with self.assertRaisesRegex(InterventionError, "credentials or secrets"): validate_intervention_response(value)

    def test_field_and_evidence_limits_accept_boundaries_and_reject_overflow(self):
        value = request()
        value['request_id'] = 'r' * 64
        for name in ('prompt', 'rationale', 'impact'):
            value[name] = 'x' * 500
        value['evidence_refs'] = [
            {'id': f'proof-{number}', 'kind': 'command', 'locator': 'x' * 1000, 'summary': 'x' * 500}
            for number in range(8)
        ]
        self.assertEqual(validate_intervention_request(value), value)
        for field in ('request_id', 'prompt', 'rationale', 'impact'):
            overflow = copy.deepcopy(value); overflow[field] += 'x'
            with self.subTest(field=field), self.assertRaises(InterventionError):
                validate_intervention_request(overflow)
        overflow = copy.deepcopy(value)
        overflow['evidence_refs'].append({'id': 'ninth', 'kind': 'command', 'locator': 'check', 'summary': 'Extra.'})
        with self.assertRaises(InterventionError):
            validate_intervention_request(overflow)
        for field in ('locator', 'summary'):
            overflow = copy.deepcopy(value); overflow['evidence_refs'][0][field] += 'x'
            with self.subTest(field=field), self.assertRaises(InterventionError):
                validate_intervention_request(overflow)
        answer = response(); answer['answer'] = 'x' * 1000
        self.assertEqual(validate_intervention_response(answer), answer)
        answer['answer'] += 'x'
        with self.assertRaises(InterventionError):
            validate_intervention_response(answer)

    def test_utf8_canonical_and_raw_byte_caps_are_independent_of_character_limits(self):
        value = request()
        value['evidence_refs'] = [
            {'id': f'proof-{number}', 'kind': 'command', 'locator': '\u754c' * 1000, 'summary': '\u754c' * 500}
            for number in range(8)
        ]
        with self.assertRaisesRegex(InterventionError, 'byte limit'):
            validate_intervention_request(value)
        for loader, content in ((load_intervention_request, request()), (load_intervention_response, response())):
            with self.subTest(loader=loader.__name__), self.assertRaisesRegex(InterventionError, 'byte limit'):
                loader(json.dumps(content) + ' ' * (16 * 1024))

    def test_punctuation_file_urls_and_unc_do_not_hide_absolute_paths(self):
        examples = ('Inspect (/etc/passwd).', 'Open file:///var/private/data.',
                    r'See (C:\Users\Matt\private.txt).', r'Inspect \\server\share\private.txt.',
                    'Inspect //server/share/private.')
        for text in examples:
            with self.subTest(text=text):
                value = request(); value['prompt'] = text
                with self.assertRaisesRegex(InterventionError, 'absolute path'):
                    validate_intervention_request(value)
                value = response(); value['answer'] = text
                with self.assertRaisesRegex(InterventionError, 'absolute path'):
                    validate_intervention_response(value)

    def test_format_controls_are_rejected_without_rejecting_normal_slashes(self):
        for control in ('\x7f', '\x85', '\u200b', '\u202e'):
            value = request(); value['rationale'] = 'Text' + control + 'hidden'
            with self.subTest(control=repr(control)), self.assertRaisesRegex(InterventionError, 'control characters'):
                validate_intervention_request(value)
        value = request(); value['prompt'] = 'Compare input/output at 1/2; use / as a separator.'
        value['evidence_refs'][0].update(kind='url', locator='https://example.invalid/docs/format')
        self.assertEqual(validate_intervention_request(value), value)

        value['prompt'] = 'The browser rejects file:// navigation.'
        self.assertEqual(validate_intervention_request(value), value)

    def test_converter_copies_only_selected_handoff_fields(self):
        draft = request_from_handoff(handoff(), request_id="request-one", attempt_id="attempt-one", blocker_id="target-blocker", action_id="choose-target", evidence_ids=["kept-evidence"])
        self.assertEqual(draft["outcome_class"], "approval-required")
        self.assertEqual(draft["evidence_refs"], [{"id": "kept-evidence", "kind": "file", "locator": "docs/decision.md", "summary": "Decision record."}])
        rendered = json.dumps(draft)
        self.assertNotIn("Should not leak", rendered)
        self.assertNotIn("hidden-evidence", rendered)

    def test_converter_rejects_unselected_or_invalid_inputs(self):
        for kwargs in ({"blocker_id": "missing"}, {"action_id": "missing"}, {"evidence_ids": ["hidden-evidence"]}):
            values = {"request_id": "request-one", "attempt_id": "attempt-one", "blocker_id": "target-blocker", "action_id": "choose-target", "evidence_ids": []}
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(InterventionError):
                request_from_handoff(handoff(), **values)
        value = handoff(); value["status"]["state"] = "paused"
        self.assertEqual(request_from_handoff(value, request_id="request-one", attempt_id="attempt-one", blocker_id="target-blocker", action_id="choose-target")["outcome_class"], "approval-required")
