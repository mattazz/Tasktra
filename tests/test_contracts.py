import unittest

from tasktra.contracts import ContractError, load_schema, validate, validate_named


class ContractTests(unittest.TestCase):
    def test_goal_contract_accepts_valid_minimum(self):
        validate_named({"id": "g-1", "title": "Title", "description": "Description", "status": "planned", "authority": {}, "acceptance": []}, "goal")

    def test_goal_contract_rejects_unknown_and_missing_fields(self):
        issues = validate({"id": "g-1", "unexpected": True}, load_schema("goal"))
        self.assertTrue(any("missing required" in issue.message for issue in issues))
        self.assertTrue(any("not an allowed" in issue.message for issue in issues))

    def test_unknown_schema_is_an_error(self):
        with self.assertRaises(ContractError):
            load_schema("not-here")

    def test_extended_schema_keywords_are_enforced(self):
        schema = {
            "type": "object",
            "properties": {
                "version": {"const": 1},
                "digest": {"type": "string", "pattern": "^[a-f]{3}$"},
                "items": {"type": "array", "uniqueItems": True},
            },
            "additionalProperties": {"type": "integer", "minimum": 1},
        }
        issues = validate({"version": 2, "digest": "bad!", "items": ["x", "x"], "extra": 0}, schema)
        self.assertEqual(len(issues), 4)

    def test_max_items_is_enforced(self):
        issues = validate([1, 2], {"type": "array", "maxItems": 1, "items": {"type": "integer"}})
        self.assertEqual([issue.message for issue in issues], ["has more than maxItems"])
