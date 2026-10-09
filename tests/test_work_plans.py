from __future__ import annotations

import json
import sqlite3
from collections import UserDict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.state import StateError, StateStore
from tasktra.workplans import WorkPlanError, load_work_plan, normalize_work_plan


class WorkPlanTests(unittest.TestCase):
    def _store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "state.sqlite")
        store.create_goal(title="Goal", description="Plan loading", goal_id="goal-one", acceptance=["Done."])
        store.define_goal_contract("goal-one", {
            "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
            "outcome": "Load plans", "motivation": "Tests.", "author_id": "owner",
            "acceptance_criteria": [{"id": "done", "statement": "Done."}],
            "scope": {"paths": ["."], "exclusions": []},
            "allowed_actions": ["goal-activate"], "allowed_effects": ["read-only"], "prohibited_actions": [],
            "quality_requirements": [], "budgets": {"tokens": 10, "attempts": 1, "elapsed_seconds": 60, "concurrency": 1},
            "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
        }, actor_id="owner")
        return store

    @staticmethod
    def _manifest() -> dict[str, object]:
        return {"kind": "tasktra.work-plan", "version": 1, "goal_id": "goal-one", "units": [
            {"id": "join", "title": "Join", "scope": {"paths": ["src/join"], "exclusions": []}, "prerequisite_ids": ["right", "left"]},
            {"id": "right", "title": "Right", "scope": {"paths": ["src/right"], "exclusions": []}},
            {"id": "left", "title": "Left", "scope": {"paths": ["src/left"], "exclusions": []}},
        ]}

    def test_loader_is_strict_and_canonical(self) -> None:
        first = load_work_plan(json.dumps(self._manifest()))
        second = load_work_plan(json.dumps({**self._manifest(), "units": list(reversed(self._manifest()["units"]))}))
        self.assertEqual(first, second)
        self.assertEqual([unit["id"] for unit in first["units"]], ["join", "left", "right"])
        with self.assertRaises(WorkPlanError) as caught:
            load_work_plan('{"kind":"tasktra.work-plan","kind":"tasktra.work-plan","version":1,"goal_id":"goal-one","units":[]}')
        self.assertEqual(caught.exception.code, "invalid_manifest")
        with self.assertRaises(WorkPlanError) as caught:
            load_work_plan(b"x" * (64 * 1024 + 1))
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertEqual(normalize_work_plan(UserDict(self._manifest()))["goal_id"], "goal-one")
        for payload, code in (
            ('{"kind":"tasktra.work-plan","version":true,"goal_id":"goal-one","units":[]}', "unsupported_version"),
            ('{"kind":"tasktra.work-plan","version":1,"goal_id":"goal-one","units":' + "[" * 1200 + "0" + "]" * 1200 + "}", "invalid_manifest"),
            ('{"kind":"tasktra.work-plan","version":1,"goal_id":"goal-one","units":[{"id":"one","title":"\\ud800","scope":{"paths":["src"],"exclusions":[]}}]}', "invalid_manifest"),
        ):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(WorkPlanError) as caught:
                    load_work_plan(payload)
                self.assertEqual(caught.exception.code, code)

    def test_forward_graph_apply_retry_and_stale_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            manifest = load_work_plan(json.dumps(self._manifest()))
            before = {item.name: item.read_bytes() for item in Path(directory).iterdir()}
            preview = store.preview_work_plan(manifest)
            after = {item.name: item.read_bytes() for item in Path(directory).iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(preview["dependency_waves"], [["left", "right"], ["join"]])
            self.assertEqual(preview["counts"], {"units": 3, "edges": 2, "create": 3, "unchanged": 0, "conflict": 0})
            applied = store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
            self.assertFalse(applied["read_only"])
            self.assertEqual(store.work_dependencies("goal-one", "join")["units"][0]["prerequisites"][0]["id"], "left")
            with self.assertRaises(WorkPlanError) as caught:
                store.apply_work_plan(manifest, expected_preview_sha256=preview["preview_sha256"])
            self.assertEqual(caught.exception.code, "stale_preview")
            retry = store.preview_work_plan(manifest)
            events = store.status()["events"]
            result = store.apply_work_plan(manifest, expected_preview_sha256=retry["preview_sha256"])
            self.assertEqual(result["counts"]["create"], 0)
            self.assertEqual(store.status()["events"], events)

    def test_invalid_graph_rolls_back_and_legacy_create_remains_available(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_work_unit(goal_id="goal-one", title="Legacy", work_unit_id="legacy", scope={"paths": ["src/legacy"], "exclusions": []})
            manifest = load_work_plan(json.dumps({
                "kind": "tasktra.work-plan", "version": 1, "goal_id": "goal-one", "units": [
                    {"id": "first", "title": "First", "scope": {"paths": ["src/first"], "exclusions": []}, "prerequisite_ids": ["second"]},
                    {"id": "second", "title": "Second", "scope": {"paths": ["src/second"], "exclusions": []}, "prerequisite_ids": ["first"]},
                ],
            }))
            events = store.status()["events"]
            with self.assertRaises(WorkPlanError) as caught:
                store.preview_work_plan(manifest)
            self.assertEqual(caught.exception.code, "cycle")
            self.assertIsNone(store.get_work_unit("first"))
            self.assertEqual(store.status()["events"], events)

    def test_contract_requirement_and_ledger_errors_are_coded(self) -> None:
        with TemporaryDirectory() as directory:
            plain = StateStore(Path(directory) / "plain.sqlite")
            plain.create_goal(title="Plain", description="No contract", goal_id="goal-one")
            manifest = load_work_plan(json.dumps(self._manifest()))
            with self.assertRaises(WorkPlanError) as caught:
                plain.preview_work_plan(manifest)
            self.assertEqual(caught.exception.code, "contract_required")
            store = self._store(directory)
            with store._connection() as connection:
                store._prepare_write(connection)
                connection.execute("UPDATE goal_contracts SET version='v999' WHERE goal_id='goal-one'")
            with self.assertRaisesRegex(StateError, "mismatched version"):
                store.attest_ledger(actor_id="owner")
            with self.assertRaises(WorkPlanError) as caught:
                store.preview_work_plan(manifest)
            self.assertEqual(caught.exception.code, "stored_contract_invalid")
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE goals SET title='Tampered' WHERE id='goal-one'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(WorkPlanError) as caught:
                store.preview_work_plan(manifest)
            self.assertEqual(caught.exception.code, "ledger_integrity")

    def test_preview_of_missing_runtime_is_coded_and_noncreating(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "state.sqlite"
            with self.assertRaises(WorkPlanError) as caught:
                StateStore(path).preview_work_plan(load_work_plan(json.dumps(self._manifest())))
            self.assertEqual(caught.exception.code, "runtime_mismatch")
            self.assertFalse(path.exists())

    def test_unrelated_legacy_definition_does_not_take_manifest_bounds(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Legacy", description="Legacy compatibility", goal_id="goal-one", acceptance=["Done."])
            store.create_work_unit(goal_id="goal-one", title="L" * 241, work_unit_id="legacy", scope={"paths": ["."], "exclusions": []})
            store.define_goal_contract("goal-one", {
                "kind": "tasktra.authority-envelope", "version": 1, "goal_id": "goal-one",
                "outcome": "Load", "motivation": "Compatibility.", "author_id": "owner",
                "acceptance_criteria": [{"id": "done", "statement": "Done."}],
                "scope": {"paths": ["."], "exclusions": []}, "allowed_actions": ["goal-activate"],
                "allowed_effects": ["read-only"], "prohibited_actions": [], "quality_requirements": [],
                "budgets": {"tokens": 10, "attempts": 1, "elapsed_seconds": 60, "concurrency": 1},
                "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
            }, actor_id="owner")
            report = store.preview_work_plan(load_work_plan(json.dumps(self._manifest())))
            self.assertEqual(report["counts"]["create"], 3)


if __name__ == "__main__":
    unittest.main()
