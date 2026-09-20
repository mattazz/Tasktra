import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.authority import authority_envelope_sha256
from tasktra.operations import export_audit, operational_status
from tasktra.state import StateError, StateStore


class Stage3OperationsTests(unittest.TestCase):
    def test_status_counts_only_current_effective_claim_approvals(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            contract = {
                "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
                "outcome": "Work", "motivation": "Test", "author_id": "owner",
                "acceptance_criteria": [{"id": "done", "statement": "Done"}],
                "scope": {"paths": ["."], "exclusions": []},
                "allowed_actions": ["work-claim"],
                "allowed_effects": ["read-only", "local-reversible-write"],
                "prohibited_actions": [], "quality_requirements": [],
                "budgets": {"tokens": 10, "attempts": 2, "elapsed_seconds": 60, "concurrency": 1},
                "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
            }
            store.create_goal(
                goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done"]
            )
            current = store.define_goal_contract("goal-one", contract, actor_id="owner")
            store.create_work_unit(
                goal_id="goal-one", work_unit_id="unit-one", title="Unit",
                scope={"paths": ["src"], "exclusions": []},
            )
            for approval_id, effect, scope in (
                ("wrong-effect", "read-only", {"paths": ["src"], "exclusions": []}),
                ("too-narrow", "local-reversible-write", {"paths": ["docs"], "exclusions": []}),
            ):
                store.record_transition_approval(
                    goal_id="goal-one", action="work-claim", effect=effect,
                    envelope_sha256=current["envelope_sha256"], approver_id="steward",
                    approver_kind="steward", performer_id="worker", scope=scope,
                    valid_until="2031-01-01T00:00:00Z", approval_id=approval_id,
                )
            status = operational_status(store, goal_id="goal-one")
            self.assertEqual(status["goal"]["missing_claim_approvals"], 1)
            self.assertEqual(status["goal"]["authorized_claim_performers"], [])
            store.record_transition_approval(
                goal_id="goal-one", action="work-claim", effect="local-reversible-write",
                envelope_sha256=current["envelope_sha256"], approver_id="steward",
                approver_kind="steward", performer_id="worker",
                scope={"paths": ["src"], "exclusions": []},
                valid_until="2031-01-01T00:00:00Z", approval_id="valid-claim",
            )
            status = operational_status(store, goal_id="goal-one")
            self.assertEqual(status["goal"]["missing_claim_approvals"], 0)
            self.assertEqual(status["goal"]["authorized_claim_performers"], ["worker"])

            contract["quality_requirements"] = ["new envelope"]
            replacement = store.define_goal_contract("goal-one", contract, actor_id="owner")
            self.assertNotEqual(replacement["envelope_sha256"], current["envelope_sha256"])
            status = operational_status(store, goal_id="goal-one")
            self.assertEqual(status["goal"]["missing_claim_approvals"], 1)
            self.assertEqual(status["goal"]["authorized_claim_performers"], [])

    def test_null_expiry_cannot_become_perpetual_authority_or_appear_current(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            contract = {
                "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
                "outcome": "Work", "motivation": "Test", "author_id": "owner",
                "acceptance_criteria": [{"id": "done", "statement": "Done"}],
                "scope": {"paths": ["."], "exclusions": []},
                "allowed_actions": ["work-claim"],
                "allowed_effects": ["read-only", "local-reversible-write"],
                "prohibited_actions": [], "quality_requirements": [],
                "budgets": {"tokens": 10, "attempts": 2, "elapsed_seconds": 60, "concurrency": 1},
                "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
            }
            store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done"])
            current = store.define_goal_contract("goal-one", contract, actor_id="owner")
            store.create_work_unit(
                goal_id="goal-one", work_unit_id="unit-one", title="Unit",
                scope={"paths": ["src"], "exclusions": []},
            )
            store.record_transition_approval(
                goal_id="goal-one", action="work-claim", effect="local-reversible-write",
                envelope_sha256=current["envelope_sha256"], approver_id="steward",
                approver_kind="steward", performer_id="worker",
                scope={"paths": ["src"], "exclusions": []},
                valid_until="2031-01-01T00:00:00Z", approval_id="corrupted-claim",
            )
            # Simulate a malformed legacy/corrupted row that bypassed the
            # canonical approval validator.
            with store._connection() as connection:
                connection.execute(
                    "UPDATE transition_approvals SET valid_until=NULL WHERE id=?",
                    ("corrupted-claim",),
                )

            status = operational_status(store, goal_id="goal-one")
            self.assertEqual(status["goal"]["missing_claim_approvals"], 1)
            self.assertEqual(status["goal"]["authorized_claim_performers"], [])
            with self.assertRaisesRegex(StateError, "no current approval"):
                store.check_authorization(
                    goal_id="goal-one", action="work-claim",
                    envelope_sha256=current["envelope_sha256"], performer_id="worker",
                    scope={"paths": ["src"], "exclusions": []},
                    effect="local-reversible-write",
                )

    def test_status_does_not_create_a_missing_runtime_database(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite"
            with self.assertRaisesRegex(StateError, "does not exist"):
                operational_status(StateStore(path))
            self.assertFalse(path.exists())

    def test_status_is_concise_by_default_and_detail_is_bounded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(
                goal_id="goal-one", title="Goal", description="Bounded status",
                acceptance=["Done"],
            )
            for identifier in ("unit-one", "unit-two"):
                store.create_work_unit(
                    goal_id="goal-one", work_unit_id=identifier, title=identifier
                )

            before = os.stat(path).st_mtime_ns
            concise = operational_status(store, goal_id="goal-one")
            after = os.stat(path).st_mtime_ns
            self.assertEqual(before, after)
            self.assertIn("summary", concise)
            self.assertIn("goal", concise)
            self.assertNotIn("units", concise["goal"])
            self.assertEqual(concise["goal"]["work_units"], {"planned": 2})
            self.assertIn("emergency_stop", concise["summary"])

            detailed = operational_status(
                store, goal_id="goal-one", detail_limit=1
            )
            self.assertEqual(len(detailed["goal"]["units"]), 1)
            self.assertNotIn("authority", detailed["goal"]["units"][0])
            self.assertIn("checkpoint_id", detailed["goal"]["units"][0])
            with self.assertRaisesRegex(StateError, "detail_limit"):
                operational_status(store, detail_limit=33)

    def test_audit_export_is_ordered_filtered_and_bounded(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(goal_id="goal-one", title="One", description="One")
            store.create_goal(goal_id="goal-two", title="Two", description="Two")
            store.append_event("operator.note", goal_id="goal-one", payload={"message": "reviewed"})

            exported = export_audit(store, goal_id="goal-one", limit=2)
            self.assertEqual(len(exported["events"]), 2)
            self.assertEqual(
                [event["sequence"] for event in exported["events"]],
                sorted(event["sequence"] for event in exported["events"]),
            )
            self.assertTrue(all(event["goal_id"] == "goal-one" for event in exported["events"]))
            self.assertIsInstance(exported["events"][-1]["payload"], dict)
            resumed = export_audit(
                store, after_sequence=exported["next_sequence"], goal_id="goal-one"
            )
            self.assertEqual(resumed["events"], [])
            with self.assertRaisesRegex(StateError, "limit"):
                export_audit(store, limit=201)
            with self.assertRaisesRegex(StateError, "after_sequence"):
                export_audit(store, after_sequence=-1)


if __name__ == "__main__":
    unittest.main()
