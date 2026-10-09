"""Focused regression coverage for the read-only work-selection explanation."""

from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyError, AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra.state import StateError
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints


NOW = stage3.NOW


class WorkSelectionTests(unittest.TestCase):
    def setUp(self):
        self._fixture = stage3.AutonomyTests()
        self._fixture.setUp()
        self.directory = self._fixture.directory
        self.store = self._fixture.store
        self.digest = self._fixture.digest

    def tearDown(self):
        self._fixture.tearDown()

    def test_preview_selects_the_same_authorized_unit_as_claim(self):
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="unit-0", title="Unavailable",
            scope={"paths": ["src/unavailable"], "exclusions": []},
        )
        preview = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            at=NOW,
        )
        self.assertEqual(preview["selected_work_unit_id"], "unit-1")
        unavailable = next(row for row in preview["candidates"] if row["work_unit_id"] == "unit-0")
        self.assertIn("approval.unavailable", unavailable["reason_codes"])
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.assertEqual(claim["work_unit_id"], preview["selected_work_unit_id"])

    def test_full_queue_selects_beyond_page_and_can_inspect_one_unit(self):
        expiry = NOW + timedelta(days=1)
        for identifier in ("unit-a", "unit-b", "unit-z"):
            self.store.create_work_unit(
                goal_id="goal-1", work_unit_id=identifier, title=identifier,
                scope={"paths": [f"src/{identifier}"], "exclusions": []},
            )
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="unit-z", action="work-claim",
            effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
            approver_id="steward", approver_kind="steward", performer_id="other",
            valid_until=expiry, at=NOW,
        )
        preview = self.store.explain_next_work(
            goal_id="goal-1", performer_id="other", envelope_sha256=self.digest,
            limit=1, offset=0, at=NOW,
        )
        self.assertEqual(preview["selected_work_unit_id"], "unit-z")
        self.assertEqual(preview["total"], 4)
        self.assertEqual(len(preview["candidates"]), 1)
        inspected = self.store.explain_next_work(
            goal_id="goal-1", performer_id="other", envelope_sha256=self.digest,
            work_unit_id="unit-a", at=NOW,
        )
        self.assertEqual(inspected["selected_work_unit_id"], "unit-z")
        self.assertEqual(inspected["candidates"][0]["work_unit_id"], "unit-a")
        self.assertIn("approval.unavailable", inspected["candidates"][0]["reason_codes"])

    def test_preview_is_read_only_and_empty_queue_is_explained(self):
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="permanent", at=NOW,
        )
        path = Path(self.store.path)
        before = path.read_bytes()
        preview = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, at=NOW,
        )
        self.assertEqual(before, path.read_bytes())
        self.assertIsNone(preview["selected_work_unit_id"])
        self.assertIn("queue.no_claimable_work", preview["goal"]["reason_codes"])
        self.assertTrue(preview["read_only"])

    def test_invalid_paging_and_unknown_inspected_unit_fail_closed(self):
        with self.assertRaisesRegex(AutonomyError, "limit"):
            self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, limit=0, at=NOW)
        with self.assertRaisesRegex(AutonomyError, "offset"):
            self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, offset=1_000_001, at=NOW)
        with self.assertRaisesRegex(AutonomyError, "unknown work unit"):
            self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, work_unit_id="missing", at=NOW)

    def test_unconfigured_goal_explains_missing_setup_without_writing(self):
        with TemporaryDirectory() as directory:
            store = AutonomyStore(Path(directory) / "state.sqlite")
            store.create_goal(goal_id="planned-goal", title="Planned", description="Planned")
            preview = store.explain_next_work(
                goal_id="planned-goal", performer_id="worker", envelope_sha256="0" * 64, at=NOW,
            )
        self.assertFalse(preview["goal"]["eligible"])
        self.assertEqual(
            preview["goal"]["reason_codes"],
            ["goal.lifecycle_not_active", "goal.budgets_missing", "goal.contract_missing"],
        )

    def test_approval_variants_and_wrong_envelope_fail_closed(self):
        expiry = NOW + timedelta(days=1)
        for identifier, performer, valid_until in (
            ("unit-null", "null-worker", expiry),
            ("unit-expired", "expired-worker", NOW),
            ("unit-wrong", "another-worker", expiry),
            ("unit-revoked", "revoked-worker", expiry),
        ):
            self.store.create_work_unit(
                goal_id="goal-1", work_unit_id=identifier, title=identifier,
                scope={"paths": [f"src/{identifier}"], "exclusions": []},
            )
            self.store.record_transition_approval(
                goal_id="goal-1", work_unit_id=identifier, action="work-claim",
                effect=LOCAL_REVERSIBLE_WRITE, envelope_sha256=self.digest,
                approver_id=f"steward-{identifier}", approver_kind="steward",
                performer_id=performer, valid_until=valid_until, at=NOW,
                approval_id=f"approval-{identifier}",
            )
        # A legacy or externally-corrupted approval with no expiry must not
        # silently become perpetual authority, even if its state seal matches.
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE transition_approvals SET valid_until=NULL WHERE id='approval-unit-null'")
        self.store.revoke_transition_approval("approval-unit-revoked", actor_id="safety", at=NOW)
        current = self.store.explain_next_work(
            goal_id="goal-1", performer_id="null-worker", envelope_sha256=self.digest, at=NOW,
        )
        self.assertIsNone(current["selected_work_unit_id"])
        for performer, unit in (("null-worker", "unit-null"), ("expired-worker", "unit-expired"), ("revoked-worker", "unit-revoked"), ("wrong-worker", "unit-wrong")):
            with self.subTest(performer=performer):
                preview = self.store.explain_next_work(
                    goal_id="goal-1", performer_id=performer, envelope_sha256=self.digest,
                    work_unit_id=unit, at=NOW,
                )
                self.assertIn("approval.unavailable", preview["candidates"][0]["reason_codes"])
        mismatch = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256="f" * 64, at=NOW,
        )
        self.assertIsNone(mismatch["selected_work_unit_id"])
        self.assertIn("authorization.envelope_mismatch", mismatch["candidates"][0]["reason_codes"])

    def test_expired_lease_still_blocks_preview_resources(self):
        self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=1, repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        preview = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            at=NOW + timedelta(seconds=2),
        )
        self.assertIn("budget.concurrency_exhausted", preview["goal"]["reason_codes"])
        self.assertIn("candidate.lease_held", preview["candidates"][0]["reason_codes"])

    def test_expired_lease_retains_token_and_elapsed_reservations_until_recovery(self):
        self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=60, token_reservation=20,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        before = self.store.path.read_bytes()
        preview = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            token_reservation=1, at=NOW + timedelta(seconds=61),
        )
        self.assertIsNone(preview["selected_work_unit_id"])
        self.assertTrue({"budget.tokens_exhausted", "budget.elapsed_exhausted", "budget.concurrency_exhausted"}.issubset(preview["goal"]["reason_codes"]))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unfinished_goal_dependency_is_explained_without_mutation(self):
        contract = stage3.envelope()
        contract["goal_id"] = "dependent"
        contract["dependencies"] = ["goal-1"]
        self.store.create_goal(goal_id="dependent", title="Dependent", description="Delivery", acceptance=["Done."])
        self.store.define_goal_contract("dependent", contract, actor_id="owner", at=NOW)
        before = self.store.path.read_bytes()
        preview = self.store.explain_next_work(
            goal_id="dependent", performer_id="worker",
            envelope_sha256=authority_envelope_sha256(contract), at=NOW,
        )
        self.assertIsNone(preview["selected_work_unit_id"])
        self.assertIn("goal.dependencies_incomplete", preview["goal"]["reason_codes"])
        self.assertIn("goal.lifecycle_not_active", preview["goal"]["reason_codes"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_claim_keeps_concurrency_short_circuit_before_lease_validation(self):
        self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.assertIsNone(self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            lease_seconds=0, repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        ))

    def test_checkpoint_and_retry_wait_are_explained(self):
        fixture = checkpoints.CheckpointTests()
        fixture.setUp()
        try:
            preview = fixture.store.explain_next_work(
                goal_id="goal-one", performer_id="worker", envelope_sha256=fixture.digest,
                at=checkpoints.NOW,
            )
            self.assertEqual(preview["selected_work_unit_id"], "unit-first")
            later = next(item for item in preview["candidates"] if item["work_unit_id"] == "unit-second")
            self.assertIn("candidate.checkpoint_not_current", later["reason_codes"])
        finally:
            fixture.tearDown()
        claim = self.store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest,
            repository="repo", revision="abc", branch="main", workspace="work", at=NOW,
        )
        self.store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="transient", at=NOW + timedelta(seconds=1),
        )
        retry = self.store.explain_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, at=NOW,
        )
        self.assertIn("candidate.retry_wait", retry["candidates"][0]["reason_codes"])

    def test_preview_never_marks_exhaustion_but_claim_marks_only_prefix(self):
        for identifier in ("unit-a", "unit-z", "unit-zz"):
            self.store.create_work_unit(goal_id="goal-1", work_unit_id=identifier, title=identifier,
                                        scope={"paths": [f"src/{identifier}"], "exclusions": []})
        with self.store._connection() as connection:
            self.store._prepare_write(connection)
            connection.execute("UPDATE work_units SET attempt_count=2 WHERE id='unit-a'")
            connection.execute("UPDATE work_units SET attempt_count=2 WHERE id='unit-zz'")
        self.store.record_transition_approval(
            goal_id="goal-1", work_unit_id="unit-z", action="work-claim", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=self.digest, approver_id="steward-z", approver_kind="steward", performer_id="other",
            valid_until=NOW + timedelta(days=1), at=NOW,
        )
        self.store.explain_next_work(goal_id="goal-1", performer_id="other", envelope_sha256=self.digest, at=NOW)
        self.assertEqual(self.store.get_work_unit("unit-a")["status"], "planned")
        claim = self.store.claim_next_work(goal_id="goal-1", performer_id="other", envelope_sha256=self.digest,
                                           repository="repo", revision="abc", branch="main", workspace="work", at=NOW)
        self.assertEqual(claim["work_unit_id"], "unit-z")
        self.assertEqual(self.store.get_work_unit("unit-a")["status"], "exhausted")
        self.assertEqual(self.store.get_work_unit("unit-z")["status"], "leased")
        self.assertEqual(self.store.get_work_unit("unit-zz")["status"], "planned")

    def test_tampering_is_rejected_and_wal_snapshot_sees_committed_rows(self):
        connection = sqlite3.connect(self.store.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            connection.execute("BEGIN")
            connection.execute("SELECT count(*) FROM work_units").fetchone()
            # Keep a connection open so the writer cannot checkpoint the WAL
            # simply by closing its last connection.
            self.store.create_work_unit(goal_id="goal-1", work_unit_id="unit-wal", title="Wal",
                                        scope={"paths": ["src/wal"], "exclusions": []})
            wal_path = Path(str(self.store.path) + "-wal")
            self.assertGreater(wal_path.stat().st_size, 0)
            before_database, before_wal = self.store.path.read_bytes(), wal_path.read_bytes()
            visible = self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, at=NOW)
            self.assertIn("unit-wal", [row["work_unit_id"] for row in visible["candidates"]])
            self.assertEqual(self.store.path.read_bytes(), before_database)
            self.assertEqual(wal_path.read_bytes(), before_wal)
            connection.rollback()
            connection.execute("UPDATE work_units SET status='complete' WHERE id='unit-wal'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StateError, "authoritative state is unsealed or tampered"):
            self.store.explain_next_work(goal_id="goal-1", performer_id="worker", envelope_sha256=self.digest, at=NOW)
