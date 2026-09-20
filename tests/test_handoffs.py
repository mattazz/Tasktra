import unittest

from tasktra.handoffs import (
    HANDOFF_KIND,
    HANDOFF_VERSION,
    MAX_HANDOFF_BYTES,
    HandoffError,
    load_handoff,
    serialize_handoff,
    validate_handoff,
)


def valid_handoff():
    return {
        "kind": HANDOFF_KIND,
        "version": HANDOFF_VERSION,
        "handoff_id": "stage-two-contract",
        "source": {"goal_id": "stage-two", "work_unit_id": "handoffs"},
        "producer": {"role": "implementer", "actor_id": "worker-one"},
        "human_summary": "Added a portable structured handoff contract and focused tests.",
        "status": {"state": "completed", "summary": "Contract is ready for later runtime integration."},
        "verified_facts": [
            {"statement": "The handoff tests pass.", "evidence_ids": ["test-run"]},
        ],
        "inferences": [
            {"statement": "The contract can support resumable downstream work.", "basis": ["test-run"]},
        ],
        "changed_paths": [
            {"path": "src/tasktra/handoffs.py", "operation": "added", "summary": "Handoff API."},
        ],
        "validation_results": [
            {
                "name": "unit tests",
                "outcome": "passed",
                "detail": "Focused contract tests passed.",
                "evidence_ids": ["test-run"],
            },
        ],
        "evidence_refs": [
            {"id": "test-run", "kind": "command", "locator": "python -m unittest tests.test_handoffs", "summary": "Focused tests."},
        ],
        "blockers": [],
        "downstream_brief": {
            "objective": "Integrate handoffs with work-unit state in a later stage.",
            "context": ["This module has no storage or CLI coupling."],
            "constraints": ["Treat incoming JSON as untrusted."],
            "recommended_next_steps": ["Persist validated envelopes through the runtime ledger."],
        },
        "requested_actions": [],
    }


class HandoffTests(unittest.TestCase):
    def test_valid_envelope_round_trips_with_byte_stable_serialization(self):
        value = valid_handoff()
        serialized = serialize_handoff(value)
        self.assertTrue(serialized.endswith("\n"))
        self.assertEqual(serialize_handoff(load_handoff(serialized)), serialized)
        self.assertEqual(load_handoff(serialized)["kind"], HANDOFF_KIND)

    def test_schema_rejects_wrong_version_and_unknown_fields(self):
        value = valid_handoff()
        value["version"] = 2
        with self.assertRaises(HandoffError):
            validate_handoff(value)

    def test_handoff_identifiers_use_the_shared_lowercase_slug_limit(self):
        for field, value in (
            ("handoff_id", "Uppercase"),
            ("handoff_id", "a" * 65),
            ("handoff_id", "two--dash"),
            ("handoff_id", "trailing-"),
        ):
            with self.subTest(field=field, value=value):
                handoff = valid_handoff()
                handoff[field] = value
                with self.assertRaisesRegex(HandoffError, "maxLength|lowercase slug|does not match pattern"):
                    validate_handoff(handoff)
        value = valid_handoff()
        value["untrusted"] = True
        with self.assertRaises(HandoffError):
            validate_handoff(value)

    def test_evidence_links_must_exist(self):
        value = valid_handoff()
        value["verified_facts"][0]["evidence_ids"] = ["not-recorded"]
        with self.assertRaisesRegex(HandoffError, "unknown evidence"):
            validate_handoff(value)
        value = valid_handoff()
        value["verified_facts"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(HandoffError, "must cite"):
            validate_handoff(value)

    def test_paths_must_be_portable_contained_and_unique(self):
        for path in ("../secret.txt", "/absolute.txt", "src\\unsafe.py", "C:/unsafe.py", "src/trailing. "):
            value = valid_handoff()
            value["changed_paths"][0]["path"] = path
            with self.assertRaises(HandoffError, msg=path):
                validate_handoff(value)
        value = valid_handoff()
        value["changed_paths"].append(
            {"path": "SRC/tasktra/handoffs.py", "operation": "modified", "summary": "Duplicate on case-insensitive filesystems."}
        )
        with self.assertRaisesRegex(HandoffError, "Duplicate changed path"):
            validate_handoff(value)

    def test_file_evidence_is_contained_and_urls_are_safe(self):
        value = valid_handoff()
        value["evidence_refs"][0] = {
            "id": "test-run", "kind": "file", "locator": "../../secrets", "summary": "Unsafe file reference."
        }
        with self.assertRaises(HandoffError):
            validate_handoff(value)
        value = valid_handoff()
        value["evidence_refs"][0] = {
            "id": "test-run", "kind": "url", "locator": "https://example.invalid/\nnext", "summary": "Unsafe URL."
        }
        with self.assertRaises(HandoffError):
            validate_handoff(value)
        value = valid_handoff()
        value["evidence_refs"][0] = {
            "id": "test-run", "kind": "url", "locator": "http://example.invalid", "summary": "Insecure URL."
        }
        with self.assertRaises(HandoffError):
            validate_handoff(value)

    def test_loader_rejects_duplicate_keys_and_non_finite_values(self):
        with self.assertRaisesRegex(HandoffError, "Duplicate JSON object key"):
            load_handoff('{"kind":"tasktra.handoff","kind":"tasktra.handoff"}')
        with self.assertRaisesRegex(HandoffError, "Non-finite"):
            load_handoff('{"kind": NaN}')

    def test_oversized_handoff_is_rejected(self):
        value = valid_handoff()
        value["verified_facts"] = [
            {"statement": f"Fact {index}", "evidence_ids": ["test-run"]}
            for index in range(33)
        ]
        with self.assertRaisesRegex(HandoffError, "32-item limit|more than maxItems"):
            validate_handoff(value)

    def test_completed_handoff_cannot_hide_unsuccessful_validation_or_open_risk(self):
        for outcome in ("failed", "not-run"):
            with self.subTest(outcome=outcome):
                value = valid_handoff()
                value["validation_results"][0]["outcome"] = outcome
                with self.assertRaisesRegex(HandoffError, "Completed handoffs"):
                    validate_handoff(value)
        value = valid_handoff()
        value["blockers"] = [{"id": "release-blocker", "severity": "blocking", "summary": "Needs attention.", "next_action": "Resolve it."}]
        with self.assertRaisesRegex(HandoffError, "blocking blockers"):
            validate_handoff(value)
        value = valid_handoff()
        value["requested_actions"] = [{"id": "human-gate", "action": "Approve release.", "rationale": "External effect.", "requires_human_approval": True}]
        with self.assertRaisesRegex(HandoffError, "pending human-approval"):
            validate_handoff(value)

    def test_completed_tester_requires_an_evidenced_passed_check(self):
        value = valid_handoff()
        value["producer"] = {"role": "tester", "actor_id": "tester-one"}
        value["validation_results"] = [{"name": "tests", "outcome": "skipped", "detail": "Not available.", "evidence_ids": ["test-run"]}]
        with self.assertRaisesRegex(HandoffError, "tester handoffs"):
            validate_handoff(value)

    def test_completed_implementer_and_reviewer_templates_cannot_be_empty(self):
        for role in ("implementer", "reviewer"):
            with self.subTest(role=role):
                value = valid_handoff()
                value["producer"] = {"role": role, "actor_id": f"{role}-one"}
                value["verified_facts"] = []
                value["inferences"] = []
                value["validation_results"] = []
                value["evidence_refs"] = []
                with self.assertRaisesRegex(HandoffError, "evidence-backed verified fact"):
                    validate_handoff(value)
        value = valid_handoff()
        value["producer"] = {"role": "reviewer", "actor_id": "reviewer-one"}
        value["validation_results"] = []
        with self.assertRaisesRegex(HandoffError, "reviewer handoffs"):
            validate_handoff(value)
        value = valid_handoff()
        value["producer"] = {"role": "tester", "actor_id": "tester-one"}
        value["validation_results"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(HandoffError, "tester handoffs"):
            validate_handoff(value)

    def test_validation_returns_a_deep_copy(self):
        value = valid_handoff()
        accepted = validate_handoff(value)
        accepted["producer"]["actor_id"] = "changed"
        accepted["verified_facts"][0]["evidence_ids"].append("other")
        self.assertEqual(value["producer"]["actor_id"], "worker-one")
        self.assertEqual(value["verified_facts"][0]["evidence_ids"], ["test-run"])

    def test_loader_rejects_oversized_bytes_before_json_parsing(self):
        payload = b"{" + (b"x" * MAX_HANDOFF_BYTES)
        with self.assertRaisesRegex(HandoffError, "byte limit"):
            load_handoff(payload)
        with self.assertRaisesRegex(HandoffError, "byte limit"):
            load_handoff("{" + ("x" * MAX_HANDOFF_BYTES))
