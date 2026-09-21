import unittest
from hashlib import sha256

from tasktra.authority import (
    AUTHORITY_ENVELOPE_KIND,
    AUTHORITY_ENVELOPE_VERSION,
    MAX_AUTHORITY_PAYLOAD_BYTES,
    TRANSITION_APPROVAL_KIND,
    TRANSITION_APPROVAL_VERSION,
    AuthorityError,
    authority_envelope_sha256,
    load_authority_envelope,
    load_transition_approval,
    serialize_authority_envelope,
    serialize_transition_approval,
    transition_approval_sha256,
    transition_approval_subject_sha256,
    validate_authority_envelope,
    validate_transition_approval,
)


def envelope():
    return {
        "kind": AUTHORITY_ENVELOPE_KIND, "version": AUTHORITY_ENVELOPE_VERSION,
        "goal_id": "goal-1", "outcome": "Deliver bounded contracts.",
        "motivation": "Make authority explicit and durable.", "author_id": "human-owner",
        "acceptance_criteria": [{"id": "criteria-1", "statement": "Focused tests pass."}],
        "scope": {"paths": ["src/tasktra"], "exclusions": ["src/tasktra/cli.py"]},
        "allowed_actions": ["edit-contracts", "run-tests"],
        "allowed_effects": ["read-only", "local-reversible-write"],
        "prohibited_actions": ["push-remote"],
        "quality_requirements": ["Preserve compatibility."],
        "budgets": {"tokens": None, "attempts": 3, "elapsed_seconds": 3600, "concurrency": 1},
        "dependencies": ["stage-2"], "checkpoints": ["tests-pass"],
        "stop_conditions": ["Authority is ambiguous."],
        "escalation_conditions": ["A new effect is needed."],
    }


def approval():
    return {
        "kind": TRANSITION_APPROVAL_KIND, "version": TRANSITION_APPROVAL_VERSION,
        "approval_id": "approval-1", "goal_id": "goal-1", "work_unit_id": "authority-contracts",
        "action": "edit-contracts", "effect": "local-reversible-write",
        "scope": {"paths": ["src/tasktra"], "exclusions": ["src/tasktra/cli.py"]},
        "envelope_sha256": authority_envelope_sha256(envelope()), "decision": "approved",
        "approver": {"kind": "steward", "id": "goal-steward"}, "performer_id": "implementer-one",
        "authority_clause": "allowed-actions.edit-contracts", "evidence": ["scope-check"],
        "valid_until": "2026-09-20T12:00:00Z", "revoked_at": None,
    }


class AuthorityEnvelopeTests(unittest.TestCase):
    def test_envelope_is_canonical_and_hash_stable(self):
        payload = serialize_authority_envelope(envelope())
        self.assertEqual(payload, serialize_authority_envelope(load_authority_envelope(payload)))
        self.assertEqual(authority_envelope_sha256(envelope()), authority_envelope_sha256(load_authority_envelope(payload)))

    def test_adversarial_envelope_inputs_are_rejected(self):
        value = envelope()
        value["scope"]["paths"] = ["."]
        self.assertEqual(validate_authority_envelope(value)["scope"]["paths"], ["."])
        with self.assertRaisesRegex(AuthorityError, "Duplicate JSON object key"):
            load_authority_envelope('{"kind":"x","kind":"y"}')
        with self.assertRaisesRegex(AuthorityError, "Non-finite"):
            load_authority_envelope('{"budget":1e999999}')
        with self.assertRaisesRegex(AuthorityError, "exceeds"):
            load_authority_envelope("x" * (MAX_AUTHORITY_PAYLOAD_BYTES + 1))
        value = envelope()
        value["scope"]["paths"] = ["../escape"]
        with self.assertRaisesRegex(AuthorityError, "escapes"):
            validate_authority_envelope(value)
        value = envelope()
        value["prohibited_actions"] = ["run-tests"]
        with self.assertRaisesRegex(AuthorityError, "overlap"):
            validate_authority_envelope(value)
        value = envelope()
        value["budgets"]["attempts"] = 0
        with self.assertRaises(AuthorityError):
            validate_authority_envelope(value)


class TransitionApprovalTests(unittest.TestCase):
    def test_approval_is_canonical_and_hash_stable(self):
        payload = serialize_transition_approval(approval())
        self.assertEqual(payload, serialize_transition_approval(load_transition_approval(payload)))
        self.assertEqual(transition_approval_sha256(approval()), transition_approval_sha256(load_transition_approval(payload)))

    def test_self_approval_and_invalid_validity_are_rejected(self):
        value = approval()
        value["approver"]["id"] = value["performer_id"]
        with self.assertRaisesRegex(AuthorityError, "cannot approve their own"):
            validate_transition_approval(value)
        value = approval()
        value["valid_until"] = None
        with self.assertRaisesRegex(AuthorityError, "require valid_until"):
            validate_transition_approval(value)
        value = approval()
        value["decision"] = "rejected"
        with self.assertRaisesRegex(AuthorityError, "Only approved"):
            validate_transition_approval(value)

    def test_v3_ceremony_binds_the_exact_typed_evidence_subject(self):
        value = approval()
        value["version"] = 3
        value["approver"] = {"kind": "human", "id": "human-owner"}
        value["resource_scope"] = None
        value["evidence"] = [{
            "kind": "acceptance-evidence", "id": "criteria-1", "sha256": "a" * 64,
        }]
        value["provenance"] = {
            "kind": "local-human-ceremony", "attester_id": "human-owner",
            "subject_sha256": transition_approval_subject_sha256(value),
            "attested_at": "2030-01-01T00:00:00Z",
        }
        self.assertEqual(validate_transition_approval(value)["version"], 3)
        value["evidence"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(AuthorityError, "does not bind"):
            validate_transition_approval(value)

    def test_v4_codex_message_binds_the_exact_approval_subject(self):
        value = approval()
        value["version"] = 4
        value["approver"] = {"kind": "human", "id": "human-owner"}
        value["resource_scope"] = None
        message = "I approve this exact bounded transition in Codex."
        value["evidence"] = [{
            "kind": "acceptance-evidence", "id": "criteria-1", "sha256": "a" * 64,
        }]
        value["provenance"] = {
            "kind": "codex-user-message", "attester_id": "human-owner",
            "subject_sha256": transition_approval_subject_sha256(value),
            "attested_at": "2030-01-01T00:00:00Z",
            "message_sha256": sha256(message.encode("utf-8")).hexdigest(),
        }
        self.assertEqual(validate_transition_approval(value)["version"], 4)
        value["scope"]["paths"] = ["docs"]
        with self.assertRaisesRegex(AuthorityError, "does not bind"):
            validate_transition_approval(value)
