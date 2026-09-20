import unittest

from tasktra.handoffs import HANDOFF_KIND, HANDOFF_VERSION
from tasktra.routing import RoutingError, build_brief, route_task


def request(*, primary_signal="change", signals=None):
    return {
        "kind": "tasktra.routing-request", "version": 1, "task_id": "routing-unit",
        "source": {"goal_id": "stage-two", "work_unit_id": "routing"},
        "objective": "Implement bounded deterministic routing.",
        "primary_signal": primary_signal, "signals": signals or [primary_signal],
        "constraints": ["Do not call models."],
        "verified_facts": [{"statement": "The role catalog has core roles.", "evidence_ids": ["catalog"]}],
        "evidence_refs": [{"id": "catalog", "kind": "file", "locator": "catalog/catalog.toml", "summary": "Core role catalog."}],
    }


def completed_handoff():
    return {
        "kind": HANDOFF_KIND, "version": HANDOFF_VERSION, "handoff_id": "implementation-result",
        "source": {"goal_id": "stage-two", "work_unit_id": "routing"},
        "producer": {"role": "implementer", "actor_id": "worker-one"},
        "human_summary": "Implemented the unit.", "status": {"state": "completed", "summary": "Ready for test."},
        "verified_facts": [{"statement": "Focused tests passed.", "evidence_ids": ["tests"]}],
        "inferences": [], "changed_paths": [],
        "validation_results": [{"name": "tests", "outcome": "passed", "detail": "Focused tests passed.", "evidence_ids": ["tests"]}],
        "evidence_refs": [{"id": "tests", "kind": "command", "locator": "python -m unittest", "summary": "Focused tests."}],
        "blockers": [],
        "downstream_brief": {"objective": "Test the change.", "context": [], "constraints": ["Keep scope bounded."], "recommended_next_steps": ["Run focused tests."]},
        "requested_actions": [],
    }


class RoutingTests(unittest.TestCase):
    def test_primary_signal_selects_exact_smallest_role(self):
        expected = {"inspect": "scout", "write": "writer", "change": "implementer", "validate": "tester", "review": "reviewer", "authority": "goal-steward", "escalate": "escalation"}
        for signal, role in expected.items():
            with self.subTest(signal=signal):
                self.assertEqual(route_task(request(primary_signal=signal))["role"], role)

    def test_primary_signal_must_be_explicit_not_inferred(self):
        with self.assertRaisesRegex(RoutingError, "primary_signal"):
            route_task(request(primary_signal="change", signals=["inspect"]))

    def test_routing_source_identifiers_use_the_shared_lowercase_slug_limit(self):
        for identifier in ("Uppercase", "a" * 65, "two--dash", "trailing-"):
            with self.subTest(identifier=identifier):
                routed = request()
                routed["source"]["goal_id"] = identifier
                with self.assertRaisesRegex(RoutingError, "maxLength|does not match|lowercase slug"):
                    route_task(routed)

    def test_brief_is_bounded_deterministic_and_uses_verified_handoff_context(self):
        routed = request()
        decision = route_task(routed)
        brief = build_brief(routed, decision, handoff=completed_handoff())
        self.assertEqual(brief["target_role"], "implementer")
        self.assertEqual(brief["source_handoff_id"], "implementation-result")
        self.assertEqual(brief["verified_facts"], [
            {"statement": "The role catalog has core roles.", "evidence_ids": ["catalog"]},
            {"statement": "Focused tests passed.", "evidence_ids": ["tests"]},
        ])
        self.assertEqual([item["id"] for item in brief["evidence_refs"]], ["catalog", "tests"])
        self.assertEqual(brief["constraints"], ["Do not call models.", "Keep scope bounded."])
        self.assertEqual(brief["recommended_next_steps"], ["Run focused tests."])

    def test_brief_refuses_tampered_decision_or_cross_goal_handoff(self):
        routed = request()
        decision = route_task(routed)
        decision["role"] = "reviewer"
        with self.assertRaisesRegex(RoutingError, "deterministic"):
            build_brief(routed, decision)
        handoff = completed_handoff()
        handoff["source"]["goal_id"] = "other-goal"
        with self.assertRaisesRegex(RoutingError, "source"):
            build_brief(routed, route_task(routed), handoff=handoff)

    def test_brief_and_decision_do_not_alias_untrusted_inputs(self):
        routed = request()
        decision = route_task(routed)
        brief = build_brief(routed, decision, handoff=completed_handoff())
        decision["rationale"] = "mutated"
        brief["source"]["goal_id"] = "other-goal"
        brief["verified_facts"][0]["evidence_ids"].append("other")
        self.assertEqual(routed["source"]["goal_id"], "stage-two")
        self.assertEqual(routed["verified_facts"][0]["evidence_ids"], ["catalog"])

    def test_brief_rejects_unknown_or_conflicting_evidence(self):
        routed = request()
        routed["verified_facts"][0]["evidence_ids"] = ["missing"]
        with self.assertRaisesRegex(RoutingError, "unknown evidence"):
            route_task(routed)
        routed = request()
        handoff = completed_handoff()
        handoff["evidence_refs"][0]["id"] = "catalog"
        handoff["verified_facts"][0]["evidence_ids"] = ["catalog"]
        handoff["validation_results"][0]["evidence_ids"] = ["catalog"]
        with self.assertRaisesRegex(RoutingError, "conflicts"):
            build_brief(routed, route_task(routed), handoff=handoff)

    def test_routing_rejects_unsafe_file_and_url_evidence_locators(self):
        routed = request()
        routed["evidence_refs"][0]["locator"] = "../../outside"
        with self.assertRaisesRegex(RoutingError, "escapes the project root"):
            route_task(routed)

        routed = request()
        routed["evidence_refs"][0].update({"kind": "url", "locator": "http://example.test/evidence"})
        with self.assertRaisesRegex(RoutingError, "safe https URL"):
            route_task(routed)

    def test_request_evidence_schema_matches_the_eight_item_brief_budget(self):
        routed = request()
        routed["evidence_refs"] = [
            {
                "id": f"evidence-{index}", "kind": "file",
                "locator": f"evidence/{index}.txt", "summary": "Bounded evidence.",
            }
            for index in range(16)
        ]
        routed["verified_facts"][0]["evidence_ids"] = ["evidence-0"]
        with self.assertRaisesRegex(RoutingError, "more than maxItems"):
            route_task(routed)

    def test_brief_selects_only_complete_facts_within_eight_evidence_budget(self):
        routed = request()
        handoff = completed_handoff()
        handoff["evidence_refs"].extend(
            {
                "id": f"evidence-{index}", "kind": "file",
                "locator": f"evidence/{index}.txt", "summary": "Bounded evidence.",
            }
            for index in range(16)
        )
        handoff["verified_facts"].extend(
            {"statement": f"Verified fact {index}.", "evidence_ids": [f"evidence-{index}"]}
            for index in range(16)
        )
        brief = build_brief(routed, route_task(routed), handoff=handoff)
        referenced = {
            evidence_id
            for fact in brief["verified_facts"]
            for evidence_id in fact["evidence_ids"]
        }
        self.assertLessEqual(len(brief["evidence_refs"]), 8)
        self.assertEqual(referenced, {item["id"] for item in brief["evidence_refs"]})
