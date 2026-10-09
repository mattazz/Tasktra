"""End-to-end atomic work-plan behavior against the execution ledger."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from tasktra.autonomy import AutonomyStore, LOCAL_REVERSIBLE_WRITE
from tasktra.authority import authority_envelope_sha256
from tasktra.workplans import WorkPlanError, normalize_work_plan
from tests import test_stage3_autonomy as stage3
from tests import test_stage3_checkpoints as checkpoints


class WorkPlanIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store, self.contract, self.digest = self._new_store()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _new_store(
        self, *, checkpoints: list[str] | None = None, scope: dict | None = None,
    ) -> tuple[AutonomyStore, dict, str]:
        path = Path(self.directory.name) / f"state-{len(list(Path(self.directory.name).glob('*.sqlite')))}.sqlite"
        store = AutonomyStore(path)
        contract = stage3.envelope()
        contract["checkpoints"] = checkpoints or []
        if scope is not None:
            contract["scope"] = scope
        contract["budgets"].update({"attempts": 10, "concurrency": 2})
        digest = authority_envelope_sha256(contract)
        store.create_goal(goal_id="goal-1", title="Goal", description="Goal", acceptance=["Done."])
        store.define_goal_contract("goal-1", contract, actor_id="owner", at=stage3.NOW)
        return store, contract, digest

    @staticmethod
    def _plan(units: list[dict]) -> dict:
        return {"kind": "tasktra.work-plan", "version": 1, "goal_id": "goal-1", "units": units}

    def _activate_for_execution(self, store: AutonomyStore, digest: str) -> None:
        expiry = stage3.NOW + timedelta(days=1)
        store.record_transition_approval(
            goal_id="goal-1", action="goal-activate", effect=LOCAL_REVERSIBLE_WRITE,
            envelope_sha256=digest, approver_id="human", performer_id="owner",
            valid_until=expiry, at=stage3.NOW,
        )
        store.activate_goal("goal-1", actor_id="owner", envelope_sha256=digest, at=stage3.NOW)
        for action in ("work-claim", "work-complete"):
            store.record_transition_approval(
                goal_id="goal-1", action=action, effect=LOCAL_REVERSIBLE_WRITE,
                envelope_sha256=digest, approver_id="steward", approver_kind="steward",
                performer_id="worker", valid_until=expiry, at=stage3.NOW,
            )

    @staticmethod
    def _claim(store: AutonomyStore, digest: str, *, seconds: int = 0) -> dict | None:
        return store.claim_next_work(
            goal_id="goal-1", performer_id="worker", envelope_sha256=digest,
            lease_seconds=30, repository="repo", revision="revision", branch="main",
            workspace="workspace", at=stage3.NOW + timedelta(seconds=seconds),
        )

    @staticmethod
    def _finish(store: AutonomyStore, claim: dict, *, seconds: int) -> None:
        store.finish_attempt(
            attempt_id=claim["attempt_id"], performer_id="worker", lease_token=claim["lease_token"],
            outcome="success", workflow=checkpoints.complete_workflow("goal-1", claim["work_unit_id"]),
            at=stage3.NOW + timedelta(seconds=seconds),
        )

    @staticmethod
    def _ledger_snapshot(store: AutonomyStore) -> dict[str, list[tuple]]:
        connection = sqlite3.connect(store.path)
        try:
            return {
                "units": connection.execute("SELECT id, status, current_attempt_id FROM work_units ORDER BY id").fetchall(),
                "edges": connection.execute("SELECT work_unit_id, prerequisite_id FROM work_unit_dependencies ORDER BY 1,2").fetchall(),
                "events": connection.execute("SELECT sequence, event_type, event_hash FROM audit_events ORDER BY sequence").fetchall(),
                "seals": connection.execute("SELECT table_name, row_id, version, row_hash FROM authority_seals ORDER BY 1,2,3").fetchall(),
            }
        finally:
            connection.close()

    @staticmethod
    def _authority_execution_counts(store: AutonomyStore) -> dict[str, int]:
        connection = sqlite3.connect(store.path)
        try:
            return {
                "approvals": connection.execute(
                    "SELECT count(*) FROM transition_approvals WHERE goal_id='goal-1'"
                ).fetchone()[0],
                "attempts": connection.execute(
                    "SELECT count(*) FROM work_attempts a JOIN work_units u ON u.id=a.work_unit_id "
                    "WHERE u.goal_id='goal-1'"
                ).fetchone()[0],
                "effect_intents": connection.execute(
                    "SELECT count(*) FROM effect_intents WHERE goal_id='goal-1'"
                ).fetchone()[0],
                "effect_receipts": connection.execute(
                    "SELECT count(*) FROM effect_receipts r JOIN effect_intents i ON i.idempotency_key=r.intent_key "
                    "WHERE i.goal_id='goal-1'"
                ).fetchone()[0],
            }
        finally:
            connection.close()

    def _assert_preview_rejected_without_mutation(
        self, store: AutonomyStore, manifest: dict, code: str,
    ) -> None:
        before_bytes = store.path.read_bytes()
        before_ledger = self._ledger_snapshot(store)
        with self.assertRaises(WorkPlanError) as caught:
            store.preview_work_plan(manifest)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(store.path.read_bytes(), before_bytes)
        self.assertEqual(self._ledger_snapshot(store), before_ledger)

    def test_plan_loading_is_authority_neutral_then_executes_fan_out_fan_in(self) -> None:
        manifest = self._plan([
            {"id": "join", "title": "Join", "scope": {"paths": ["src/join"], "exclusions": []}, "prerequisite_ids": ["left", "right"]},
            {"id": "right", "title": "Right", "scope": {"paths": ["src/right"], "exclusions": []}, "prerequisite_ids": ["root"]},
            {"id": "root", "title": "Root", "scope": {"paths": ["src/root"], "exclusions": []}},
            {"id": "left", "title": "Left", "scope": {"paths": ["src/left"], "exclusions": []}, "prerequisite_ids": ["root"]},
        ])
        execution_counts = self._authority_execution_counts(self.store)
        preview = self.store.preview_work_plan(manifest)
        applied = self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])

        self.assertEqual(self.store.get_goal("goal-1")["status"], "planned")
        self.assertEqual(self._authority_execution_counts(self.store), execution_counts)
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_attempts"], 0)
        self.assertEqual(self.store.budget_summary("goal-1")["consumed_tokens"], 0)
        self.assertTrue(all(self.store.get_work_unit(identifier)["current_attempt_id"] is None for identifier in ("root", "left", "right", "join")))
        self.assertEqual(preview["dependency_waves"], [["root"], ["left", "right"], ["join"]])
        self.assertEqual(applied["counts"], {"units": 4, "edges": 4, "create": 4, "unchanged": 0, "conflict": 0})

        self._activate_for_execution(self.store, self.digest)
        root = self._claim(self.store, self.digest)
        self.assertIsNotNone(root)
        assert root is not None
        self.assertEqual(root["work_unit_id"], "root")
        self._finish(self.store, root, seconds=1)
        branches = [self._claim(self.store, self.digest, seconds=1), self._claim(self.store, self.digest, seconds=1)]
        self.assertEqual({claim["work_unit_id"] for claim in branches if claim is not None}, {"left", "right"})
        for claim in branches:
            assert claim is not None
            self._finish(self.store, claim, seconds=2)
        joined = self._claim(self.store, self.digest, seconds=2)
        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertEqual(joined["work_unit_id"], "join")
        self._finish(self.store, joined, seconds=3)
        self.assertEqual(
            [item["id"] for item in self.store.work_dependencies("goal-1", "join")["units"][0]["prerequisites"]],
            ["left", "right"],
        )
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_mutable_execution_changes_do_not_stale_definition_preview(self) -> None:
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="existing", title="Existing",
            scope={"paths": ["src/existing"], "exclusions": []},
        )
        self._activate_for_execution(self.store, self.digest)
        manifest = self._plan([
            {"id": "later", "title": "Later", "scope": {"paths": ["src/later"], "exclusions": []}},
        ])
        preview = self.store.preview_work_plan(manifest)
        claimed = self._claim(self.store, self.digest)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self._finish(self.store, claimed, seconds=1)

        applied = self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(applied["counts"]["create"], 1)
        self.assertEqual(self.store.get_work_unit("later")["status"], "planned")
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_concurrent_identical_applies_create_one_graph_and_one_stale_result(self) -> None:
        manifest = self._plan([
            {"id": "after", "title": "After", "scope": {"paths": ["src/after"], "exclusions": []}, "prerequisite_ids": ["before"]},
            {"id": "before", "title": "Before", "scope": {"paths": ["src/before"], "exclusions": []}},
        ])
        preview = self.store.preview_work_plan(manifest)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def apply_from_separate_connection() -> None:
            barrier.wait()
            try:
                result = AutonomyStore(self.store.path).apply_work_plan(
                    manifest, expected_preview_sha256=preview["preview_sha256"],
                )
                outcomes.append(("applied", result["counts"]["create"]))
            except WorkPlanError as error:
                outcomes.append(("error", error.code))

        threads = [threading.Thread(target=apply_from_separate_connection) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "concurrent work-plan apply did not finish")

        self.assertCountEqual(outcomes, [("applied", 2), ("error", "stale_preview")])
        self.assertEqual(self._ledger_snapshot(self.store)["edges"], [("after", "before")])
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_competing_definition_and_reached_checkpoint_reject_old_previews_without_batch_rows(self) -> None:
        manifest = self._plan([
            {"id": "batch", "title": "Batch", "scope": {"paths": ["src/batch"], "exclusions": []}},
        ])
        preview = self.store.preview_work_plan(manifest)
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="competing", title="Competing",
            scope={"paths": ["src/competing"], "exclusions": []},
        )
        before = self._ledger_snapshot(self.store)
        with self.assertRaises(WorkPlanError) as caught:
            self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertIsNone(self.store.get_work_unit("batch"))
        self.assertEqual(self._ledger_snapshot(self.store), before)

        checkpoint_store, _, checkpoint_digest = self._new_store(checkpoints=["first"])
        checkpoint_store.create_work_unit(
            goal_id="goal-1", work_unit_id="first-work", title="First work",
            scope={"paths": ["src/first"], "exclusions": []}, checkpoint_id="first",
        )
        self._activate_for_execution(checkpoint_store, checkpoint_digest)
        checkpoint_manifest = self._plan([
            {"id": "late-first", "title": "Late first", "scope": {"paths": ["src/late"], "exclusions": []}, "checkpoint_id": "first"},
        ])
        checkpoint_preview = checkpoint_store.preview_work_plan(checkpoint_manifest)
        claim = self._claim(checkpoint_store, checkpoint_digest)
        self.assertIsNotNone(claim)
        assert claim is not None
        self._finish(checkpoint_store, claim, seconds=1)
        with self.assertRaises(WorkPlanError) as caught:
            checkpoint_store.apply_work_plan(checkpoint_manifest, expected_preview_sha256=checkpoint_preview["preview_sha256"])
        self.assertEqual(caught.exception.code, "checkpoint_reached")
        self.assertIsNone(checkpoint_store.get_work_unit("late-first"))
        self.assertTrue(checkpoint_store.verify_audit()["ok"])

    def test_external_scope_and_checkpoint_previews_reject_without_mutation(self) -> None:
        cases: list[tuple[str, AutonomyStore, dict, str]] = [
            (
                "missing prerequisite", self.store,
                self._plan([{"id": "missing-dependent", "title": "Missing dependent", "scope": {"paths": ["src/missing"], "exclusions": []}, "prerequisite_ids": ["absent"]}]),
                "missing_prerequisite",
            ),
        ]

        other_contract = stage3.envelope()
        other_contract["goal_id"] = "goal-2"
        self.store.create_goal(goal_id="goal-2", title="Other", description="Other", acceptance=["Done."])
        self.store.define_goal_contract("goal-2", other_contract, actor_id="owner", at=stage3.NOW)
        self.store.create_work_unit(
            goal_id="goal-2", work_unit_id="other-unit", title="Other unit",
            scope={"paths": ["src/other"], "exclusions": []},
        )
        cases.append((
            "cross goal prerequisite", self.store,
            self._plan([{"id": "cross-dependent", "title": "Cross dependent", "scope": {"paths": ["src/cross"], "exclusions": []}, "prerequisite_ids": ["other-unit"]}]),
            "cross_goal_prerequisite",
        ))

        bounded_store, _, _ = self._new_store(scope={"paths": ["src/allowed"], "exclusions": []})
        cases.append((
            "outside envelope scope", bounded_store,
            self._plan([{"id": "outside", "title": "Outside", "scope": {"paths": ["src/outside"], "exclusions": []}}]),
            "scope_outside_envelope",
        ))

        checkpoint_store, _, _ = self._new_store(checkpoints=["first", "second"])
        cases.append((
            "later prerequisite checkpoint", checkpoint_store,
            self._plan([
                {"id": "first-dependent", "title": "First dependent", "scope": {"paths": ["src/first"], "exclusions": []}, "checkpoint_id": "first", "prerequisite_ids": ["second-prerequisite"]},
                {"id": "second-prerequisite", "title": "Second prerequisite", "scope": {"paths": ["src/second"], "exclusions": []}, "checkpoint_id": "second"},
            ]),
            "checkpoint_invalid",
        ))

        for label, store, manifest, code in cases:
            with self.subTest(label=label):
                self._assert_preview_rejected_without_mutation(store, manifest, code)

    def test_contract_replacement_stales_preview_without_batch_insert(self) -> None:
        manifest = self._plan([
            {"id": "batch", "title": "Batch", "scope": {"paths": ["src/batch"], "exclusions": []}},
        ])
        preview = self.store.preview_work_plan(manifest)
        replacement = copy.deepcopy(self.contract)
        replacement["motivation"] = "Replacement contract invalidates the preview binding."
        self.store.define_goal_contract("goal-1", replacement, actor_id="owner", at=stage3.NOW + timedelta(seconds=1))
        before_bytes = self.store.path.read_bytes()
        before_ledger = self._ledger_snapshot(self.store)

        with self.assertRaises(WorkPlanError) as caught:
            self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertIsNone(self.store.get_work_unit("batch"))
        self.assertEqual(self.store.path.read_bytes(), before_bytes)
        self.assertEqual(self._ledger_snapshot(self.store), before_ledger)

    def test_mapping_normalization_enforces_total_unit_scope_and_prerequisite_bounds(self) -> None:
        unit = lambda identifier: {
            "id": identifier, "title": "Bounded", "scope": {"paths": ["src"], "exclusions": []},
        }
        cases = [
            (
                "unit count", self._plan([unit(f"unit-{index}") for index in range(257)]), "limit_exceeded",
            ),
            (
                "scope entries", self._plan([{
                    "id": "wide-scope", "title": "Wide scope",
                    "scope": {"paths": [f"scope-{index}" for index in range(65)], "exclusions": []},
                }]), "limit_exceeded",
            ),
            (
                "prerequisites", self._plan([{
                    "id": "many-prerequisites", "title": "Many prerequisites",
                    "scope": {"paths": ["src"], "exclusions": []},
                    "prerequisite_ids": [f"prerequisite-{index}" for index in range(65)],
                }]), "limit_exceeded",
            ),
            (
                "canonical mapping bytes", self._plan([{
                    "id": f"large-{index}", "title": "x" * 240,
                    "scope": {"paths": ["src"], "exclusions": []},
                } for index in range(256)]), "input_too_large",
            ),
        ]
        for label, manifest, code in cases:
            with self.subTest(label=label):
                with self.assertRaises(WorkPlanError) as caught:
                    normalize_work_plan(manifest)
                self.assertEqual(caught.exception.code, code)

    def test_unchanged_only_retries_remain_byte_stable_after_checkpoint_or_pause(self) -> None:
        checkpoint_store, _, checkpoint_digest = self._new_store(checkpoints=["first"])
        checkpoint_store.create_work_unit(
            goal_id="goal-1", work_unit_id="first-work", title="First work",
            scope={"paths": ["src/first"], "exclusions": []}, checkpoint_id="first",
        )
        self._activate_for_execution(checkpoint_store, checkpoint_digest)
        exact_checkpoint = self._plan([{
            "id": "first-work", "title": "First work", "scope": {"paths": ["src/first"], "exclusions": []}, "checkpoint_id": "first",
        }])
        checkpoint_preview = checkpoint_store.preview_work_plan(exact_checkpoint)
        claim = self._claim(checkpoint_store, checkpoint_digest)
        self.assertIsNotNone(claim)
        assert claim is not None
        self._finish(checkpoint_store, claim, seconds=1)
        checkpoint_bytes = checkpoint_store.path.read_bytes()
        checkpoint_result = checkpoint_store.apply_work_plan(
            exact_checkpoint, expected_preview_sha256=checkpoint_preview["preview_sha256"],
        )
        self.assertEqual(checkpoint_result["counts"]["create"], 0)
        self.assertEqual(checkpoint_store.path.read_bytes(), checkpoint_bytes)
        self._assert_preview_rejected_without_mutation(checkpoint_store, self._plan([{
            "id": "late-first", "title": "Late first", "scope": {"paths": ["src/late"], "exclusions": []}, "checkpoint_id": "first",
        }]), "checkpoint_reached")

        paused_store, _, paused_digest = self._new_store()
        paused_store.create_work_unit(
            goal_id="goal-1", work_unit_id="existing", title="Existing",
            scope={"paths": ["src/existing"], "exclusions": []},
        )
        self._activate_for_execution(paused_store, paused_digest)
        paused_store.pause_goal("goal-1", actor_id="owner", at=stage3.NOW + timedelta(seconds=1))
        exact_paused = self._plan([{
            "id": "existing", "title": "Existing", "scope": {"paths": ["src/existing"], "exclusions": []},
        }])
        paused_preview = paused_store.preview_work_plan(exact_paused)
        paused_bytes = paused_store.path.read_bytes()
        paused_result = paused_store.apply_work_plan(exact_paused, expected_preview_sha256=paused_preview["preview_sha256"])
        self.assertEqual(paused_result["counts"]["create"], 0)
        self.assertEqual(paused_store.path.read_bytes(), paused_bytes)
        self._assert_preview_rejected_without_mutation(paused_store, self._plan([{
            "id": "paused-new", "title": "Paused new", "scope": {"paths": ["src/new"], "exclusions": []},
        }]), "goal_not_accepting_work")

    def test_dependency_insert_failure_rolls_back_units_edges_events_and_seals(self) -> None:
        self.store.create_work_unit(
            goal_id="goal-1", work_unit_id="existing", title="Existing",
            scope={"paths": ["src/existing"], "exclusions": []},
        )
        manifest = self._plan([
            {"id": "batch-fail", "title": "Batch fail", "scope": {"paths": ["src/batch"], "exclusions": []}, "prerequisite_ids": ["existing"]},
        ])
        preview = self.store.preview_work_plan(manifest)
        with self.store._connection() as connection:
            connection.execute(
                "CREATE TRIGGER abort_work_plan_dependency BEFORE INSERT ON work_unit_dependencies "
                "WHEN NEW.work_unit_id = 'batch-fail' BEGIN SELECT RAISE(ABORT, 'forced dependency failure'); END"
            )
        before = self._ledger_snapshot(self.store)
        with self.assertRaises(WorkPlanError) as caught:
            self.store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(caught.exception.code, "ledger_failure")
        self.assertEqual(self._ledger_snapshot(self.store), before)
        self.assertIsNone(self.store.get_work_unit("batch-fail"))
        with self.store._connection() as connection:
            connection.execute("DROP TRIGGER abort_work_plan_dependency")
        self.assertTrue(self.store.verify_audit()["ok"])


if __name__ == "__main__":
    unittest.main()
