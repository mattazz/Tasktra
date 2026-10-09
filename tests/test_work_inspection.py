from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from tasktra.state import StateError, StateStore
from tasktra.work_inspection import inspect_work_unit
from tests import test_stage4_effect_ledger as effect_fixture


class WorkInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.sqlite")
        self.store.create_goal(goal_id="goal", title="Goal", description="Goal", acceptance=["done"])
        self.store.create_work_unit(goal_id="goal", work_unit_id="first", title="private title")
        self.store.create_work_unit(goal_id="goal", work_unit_id="selected", title="also private", prerequisite_ids=("first",))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def inspect(self, **kwargs: object) -> dict:
        return inspect_work_unit(
            self.store, project_root=Path(self.directory.name) / "project root", goal_id="goal",
            work_unit_id="selected", **kwargs,
        )

    def test_closed_shape_and_shared_structural_row(self) -> None:
        report = self.inspect(at="2030-01-01T00:00:00Z")
        self.assertEqual(list(report), [
            "kind", "version", "schema_version", "goal_id", "work_unit_id", "root", "read_only",
            "authority_evaluated", "claimability_evaluated", "notice", "capture", "unit", "goal_context",
            "attempts", "activity", "evidence_index", "action_candidates",
        ])
        self.assertEqual(report["unit"]["incomplete_direct_prerequisite_ids"], ["first"])
        self.assertEqual(report["attempts"], {
            "items": [], "limit": 20, "before_attempt_no": None, "returned": 0,
            "has_more": False, "next_before_attempt_no": None,
        })
        self.assertNotIn("private title", str(report))

    def test_invalid_identity_bounds_and_cross_goal_fail_closed(self) -> None:
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 51}, {"before_sequence": 0},
                       {"before_sequence": True}, {"before_attempt_no": 0}, {"before_attempt_no": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StateError):
                self.inspect(**kwargs)
        self.store.create_goal(goal_id="other", title="Other", description="Other", acceptance=["done"])
        with self.assertRaises(StateError):
            inspect_work_unit(self.store, project_root=Path(self.directory.name), goal_id="other", work_unit_id="selected")

    def test_unrelated_integrity_detail_is_not_reflected(self) -> None:
        self.store.create_goal(goal_id="private-goal", title="Private", description="Private", acceptance=["done"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("DROP TRIGGER authority_seals_no_update")
            connection.execute("UPDATE authority_seals SET row_hash='tampered' WHERE table_name='goals' AND row_id='private-goal'")
            connection.commit()
        with self.assertRaisesRegex(StateError, "^unable to read work-unit inspection state$"):
            self.inspect()

    def test_rebound_provider_intent_suppresses_prior_attempt_token(self) -> None:
        case = effect_fixture.ProviderEffectLedgerTests()
        with patch("tasktra.autonomy.secrets.token_urlsafe", return_value="z" * 32):
            case.setUp()
        self.addCleanup(case.tearDown)
        key = case.claim["lease_token"]
        operation = effect_fixture.descriptor()
        case.store.prepare_provider_effect(
            idempotency_key=key, goal_id="goal-one", work_unit_id="unit-one", operation_descriptor=operation,
            request={"body": "fixture"}, envelope_sha256=case.digest, performer_id="worker",
            work_attempt_id=case.claim["attempt_id"], lease_token=key, at=effect_fixture.NOW,
        )
        dispatched = case.store.begin_provider_effect_dispatch(
            idempotency_key=key, operation_descriptor=operation, performer_id="worker", lease_token=key,
            at=effect_fixture.NOW,
        )
        case.mark_indeterminate(key, dispatched)
        case.store._record_adapter_provider_reconciliation(
            idempotency_key=key, resolution="absent", observation={"checked": True}, performer_id="worker",
            at=effect_fixture.NOW,
        )
        later = effect_fixture.NOW + timedelta(seconds=31)
        case.store.recover_expired_leases(goal_id="goal-one", at=later)
        replacement = case.store.claim_next_work(
            goal_id="goal-one", performer_id="worker", envelope_sha256=case.digest, lease_seconds=30,
            repository="repo", revision="abc", branch="main", workspace="work", lease_token="y" * 32, at=later,
        )
        case.store.retry_provider_effect(
            idempotency_key=key, performer_id="worker", work_attempt_id=replacement["attempt_id"],
            lease_token="y" * 32, at=later,
        )
        report = inspect_work_unit(case.store, project_root=Path(case.directory.name), goal_id="goal-one", work_unit_id="unit-one", at=later)
        self.assertEqual(report["evidence_index"]["provider_effects"]["items"], [])
        self.assertEqual(report["evidence_index"]["provider_effects"]["redacted_items"], 1)
        self.assertNotIn(key, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
