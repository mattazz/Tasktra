"""Stage 5 specialist catalog contract acceptance tests."""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from tasktra.compiler import load_catalog


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "catalog" / "specialist-roles.toml"
EXPECTED_FAMILIES = {
    "architecture": {"architecture-reviewer", "system-designer", "domain-modeler"},
    "quality": {"code-reviewer", "security-reviewer", "performance-reviewer", "test-strategist"},
    "delivery": {"issue-triager", "work-selector", "pr-scanner", "pr-reviewer", "release-writer"},
    "product": {"product-analyst", "ux-accessibility-reviewer", "research-analyst"},
    "operations": {"deployment-reviewer", "incident-responder"},
    "knowledge": {"documentation-curator", "lesson-curator"},
    "software-development": {"application-implementer", "frontend-specialist", "backend-api-specialist", "data-migration-specialist", "integration-specialist", "test-automation-specialist", "end-to-end-evaluator", "reliability-observability-specialist", "developer-experience-specialist", "refactoring-specialist"},
}
CONTRACT_FIELDS = {"trigger", "responsibility", "inputs", "outputs", "stop_conditions", "validation_expectations", "evidence_policy"}


class SpecialistContractTests(unittest.TestCase):
    def test_all_29_stage5_domains_load_with_their_existing_families(self):
        catalog = load_catalog(ROOT / "catalog")
        roles = tomllib.loads(SOURCE.read_text(encoding="utf-8"))["role"]
        self.assertEqual(sum(map(len, EXPECTED_FAMILIES.values())), 29)
        self.assertEqual({role["id"] for role in roles}, set().union(*EXPECTED_FAMILIES.values()))
        for family, identifiers in EXPECTED_FAMILIES.items():
            self.assertEqual({role["id"] for role in roles if role["family"] == family}, identifiers)
            for identifier in identifiers:
                document = catalog.roles[identifier]
                self.assertEqual(document.family, family)
                self.assertEqual(document.source.resolve(), SOURCE.resolve())

    def test_every_specialist_has_the_complete_bounded_work_contract(self):
        roles = tomllib.loads(SOURCE.read_text(encoding="utf-8"))["role"]
        for role in roles:
            with self.subTest(role=role["id"]):
                self.assertTrue(CONTRACT_FIELDS.issubset(role), role)
                for field in ("trigger", "responsibility", "evidence_policy"):
                    self.assertIsInstance(role[field], str)
                    self.assertTrue(role[field].strip())
                for field in ("inputs", "outputs", "stop_conditions", "validation_expectations"):
                    self.assertIsInstance(role[field], list)
                    self.assertTrue(role[field])
                    self.assertTrue(all(isinstance(item, str) and item.strip() for item in role[field]))
                self.assertIn("bounded", role["evidence_policy"].casefold())


if __name__ == "__main__":
    unittest.main()
