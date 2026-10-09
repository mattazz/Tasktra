"""Cross-boundary acceptance checks for the offline operator cockpit.

These tests intentionally use a disposable SQLite ledger and the rendered
browser helper.  They exercise the snapshot boundary rather than repeating the
unit tests for the capture and view modules.
"""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from tasktra.cockpit_view import CSS, JAVASCRIPT, render
from tasktra.dependency_impact import dependency_impact
from tasktra.operator_cockpit import capture_operator_cockpit, export_operator_cockpit
from tasktra.overview import orchestration_overview
from tasktra.state import StateError, StateStore


class OperatorCockpitIntegrationTests(unittest.TestCase):
    """Acceptance checks spanning the read-only ledger, capture, and browser model."""

    def _store(self, directory: str) -> StateStore:
        store = StateStore(Path(directory) / "runtime.sqlite")
        store.create_goal(
            goal_id="goal-cockpit", title="Cockpit goal", description="Synthetic integration fixture",
            priority=7, acceptance=["Capture the verified snapshot"],
        )
        # The production invariant is a coherent snapshot under a concurrent
        # WAL writer.  Enable WAL before the reader starts; capture itself must
        # remain mode=ro and therefore may not change this runtime setting.
        connection = sqlite3.connect(store.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        finally:
            connection.close()
        return store

    @staticmethod
    def _database_bytes(store: StateStore) -> dict[str, bytes]:
        """Include WAL/SHM when SQLite has created them, not just main DB bytes."""
        return {
            path.name: path.read_bytes()
            for path in (store.path, store.path.with_name(f"{store.path.name}-wal"), store.path.with_name(f"{store.path.name}-shm"))
            if path.exists()
        }

    def _assert_cold_read_did_not_mutate_durable_ledger(
        self, store: StateStore, before: dict[str, bytes], after: dict[str, bytes],
    ) -> None:
        """Allow SQLite's documented empty-WAL/SHM reader coordination only.

        A normal ``mode=ro`` reader may create a SHM index and an empty WAL on
        Windows.  Those files contain no committed database transaction.  The
        main DB and a pre-existing WAL remain the durable-state oracle.
        """
        main = store.path.name
        wal = f"{main}-wal"
        shm = f"{main}-shm"
        self.assertEqual(after[main], before[main])
        self.assertTrue(set(after).difference(before).issubset({wal, shm}), after)
        if wal in before:
            self.assertEqual(after.get(wal), before[wal])
        elif wal in after:
            self.assertEqual(after[wal], b"")

    @staticmethod
    def _provenance(root: Path) -> dict[str, object]:
        return {
            "package_version": "test",
            "package_kind": "source-checkout",
            "package_path": str(root / "src" / "tasktra"),
            "loaded_source_checkout": str(root),
            "project_source_path": str(root / "src" / "tasktra"),
            "source_matches_project": True,
            "foreign_source_checkout": False,
            "supported_runtime_schema": 12,
            "python_executable": sys.executable,
        }

    def _capture(self, store: StateStore, root: Path) -> dict[str, object]:
        return capture_operator_cockpit(
            store,
            project_root=root,
            project_name="Cockpit integration",
            source_provenance=self._provenance(root),
            page_size=3,
            at="2030-01-02T03:04:05Z",
        )

    @staticmethod
    def _add_graph(store: StateStore) -> None:
        # The diamond catches incorrect first-path-wins distance calculations.
        for unit_id, prerequisites in (
            ("root", ()),
            ("left", ("root",)),
            ("right", ("root",)),
            ("anchor", ("root", "left", "right")),
            ("child-a", ("anchor",)),
            ("child-b", ("anchor",)),
            ("join", ("child-a", "child-b")),
        ):
            store.create_work_unit(
                goal_id="goal-cockpit", work_unit_id=unit_id, title=f"Unit {unit_id}",
                scope={"paths": ["private-not-in-cockpit"], "exclusions": []}, prerequisite_ids=prerequisites,
            )

    @staticmethod
    def _goal(snapshot: dict[str, object]) -> dict[str, object]:
        return next(goal for goal in snapshot["goals"] if goal["id"] == "goal-cockpit")  # type: ignore[index,return-value]

    @staticmethod
    def _browser_helper(payload: dict[str, object], helper: str) -> dict[str, object]:
        """Execute the exact shipped inline JS without a browser or npm package."""
        node = shutil.which("node")
        if node is None:
            raise unittest.SkipTest("Node.js is unavailable for cockpit JS parity")
        with TemporaryDirectory() as directory:
            program = Path(directory) / "cockpit-helper.js"
            program.write_text(
                "global.window = {};\n" + JAVASCRIPT + "\n"
                "const fs = require('fs');\n"
                "const input = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
                "const result = input.helper === 'impact'\n"
                "  ? window.TasktraCockpit.dependencyImpact(input.goal, input.work_unit_id, input.direction, input.limit, input.offset)\n"
                "  : window.TasktraCockpit.expandGuidance(input.snapshot, input.template_id, input.values);\n"
                "process.stdout.write(JSON.stringify(result));\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [str(node), str(program)], input=json.dumps({"helper": helper, **payload}),
                text=True, encoding="utf-8", capture_output=True, check=False, timeout=20,
            )
            if completed.returncode:
                raise AssertionError(f"cockpit JavaScript helper failed: {completed.stderr}")
            return json.loads(completed.stdout)

    def test_capture_is_a_verified_read_only_snapshot_with_authoritative_fields(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            before = self._database_bytes(store)

            snapshot = self._capture(store, root)

            self._assert_cold_read_did_not_mutate_durable_ledger(store, before, self._database_bytes(store))
            self.assertTrue(snapshot["read_only"])
            self.assertFalse(snapshot["claimability_evaluated"])
            self.assertEqual(snapshot["capture"]["captured_at"], "2030-01-02T03:04:05Z")  # type: ignore[index]
            self.assertIn("state_manifest_sha256", snapshot["capture"])  # type: ignore[operator]
            self.assertEqual(snapshot["completeness"]["work_units_captured"], 7)  # type: ignore[index]
            self.assertNotIn("private-not-in-cockpit", json.dumps(snapshot))

            overview = orchestration_overview(store, goal_id="goal-cockpit")
            goal = self._goal(snapshot)
            self.assertEqual(goal["id"], overview["goal"]["id"])  # type: ignore[index]
            self.assertEqual(goal["progress"], overview["goal"]["progress"])  # type: ignore[index]
            self.assertEqual(goal["attention"], overview["goal"]["attention"])  # type: ignore[index]
            self.assertTrue(goal["completeness"]["graph_complete"])  # type: ignore[index]

    def test_capture_rejects_tamper_cross_goal_and_cycle_without_side_effects(self) -> None:
        for label, statement, params in (
            ("cross goal", "INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)", ("anchor", "foreign")),
            ("cycle", "INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)", ("root", "anchor")),
        ):
            with self.subTest(label=label), TemporaryDirectory() as directory:
                    root = Path(directory)
                    store = self._store(directory)
                    store.create_goal(goal_id="other-goal", title="Other", description="Other fixture")
                    self._add_graph(store)
                    store.create_work_unit(goal_id="other-goal", work_unit_id="foreign", title="Foreign")
                    # Deliberately bypass the state API.  Capture must validate the
                    # seal and graph before it derives or writes an artifact.
                    connection = sqlite3.connect(store.path)
                    try:
                        if label == "cross goal":
                            connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute(statement, params)
                        connection.commit()
                    finally:
                        connection.close()
                    before = self._database_bytes(store)
                    with self.assertRaises(StateError):
                        self._capture(store, root)
                    self._assert_cold_read_did_not_mutate_durable_ledger(store, before, self._database_bytes(store))

    def test_capture_absent_runtime_is_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "does-not-exist.sqlite")
            with self.assertRaisesRegex(StateError, "does not exist"):
                self._capture(store, root)
            self.assertFalse(store.path.exists())

    def test_capture_uses_one_wal_snapshot_while_a_real_writer_commits(self) -> None:
        """A writer may finish during capture, but cannot create a mixed artifact."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            reached_graph = threading.Event()
            release_capture = threading.Event()
            result: list[dict[str, object]] = []
            errors: list[BaseException] = []
            original = StateStore._work_dependency_graph_in_transaction

            def held_graph(connection: sqlite3.Connection, goal_id: str, **kwargs: object) -> object:
                graph = original(connection, goal_id, **kwargs)
                if goal_id == "goal-cockpit":
                    reached_graph.set()
                    if not release_capture.wait(timeout=10):
                        raise TimeoutError("capture was not released")
                return graph

            def capture() -> None:
                try:
                    result.append(self._capture(store, root))
                except BaseException as error:
                    errors.append(error)

            with patch.object(StateStore, "_work_dependency_graph_in_transaction", staticmethod(held_graph)):
                reader = threading.Thread(target=capture)
                reader.start()
                self.assertTrue(reached_graph.wait(timeout=10), "capture never reached its graph read")
                writer_errors: list[BaseException] = []

                def write() -> None:
                    try:
                        store.create_work_unit(
                            goal_id="goal-cockpit", work_unit_id="committed-during-capture",
                            title="Committed during snapshot",
                        )
                    except BaseException as error:
                        writer_errors.append(error)

                writer = threading.Thread(target=write)
                writer.start()
                writer.join(timeout=10)
                release_capture.set()
                reader.join(timeout=10)
                self.assertFalse(writer.is_alive(), "WAL writer was blocked by cockpit read")
                self.assertEqual(writer_errors, [])
            self.assertFalse(reader.is_alive(), "capture did not complete")
            self.assertEqual(errors, [])
            captured = result[0]
            goal = self._goal(captured)
            self.assertEqual(captured["aggregates"]["work_units"]["total"], 7)  # type: ignore[index]
            self.assertEqual(goal["progress"]["work"]["total"], 7)  # type: ignore[index]
            self.assertEqual(goal["completeness"]["work_units_captured"], 7)  # type: ignore[index]
            self.assertEqual(len(goal["work_units"]), 7)  # type: ignore[arg-type]
            self.assertEqual(self._capture(store, root)["aggregates"]["work_units"]["total"], 8)  # type: ignore[index]

    def test_browser_dependency_impact_matches_python_across_pages_and_directions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            snapshot = self._capture(store, root)
            goal = self._goal(snapshot)
            for direction, limit, offset in (
                ("both", 2, 0), ("both", 2, 2), ("prerequisites", 20, 0), ("dependents", 1, 1),
            ):
                with self.subTest(direction=direction, limit=limit, offset=offset):
                    expected = dependency_impact(
                        store, goal_id="goal-cockpit", work_unit_id="anchor",
                        direction=direction, limit=limit, offset=offset,
                    )
                    actual = self._browser_helper({
                        "goal": goal, "work_unit_id": "anchor", "direction": direction,
                        "limit": limit, "offset": offset,
                    }, "impact")
                    self.assertTrue(actual.pop("available"))
                    for key in ("anchor", "summary", "direction", "relations", "total", "limit", "offset", "next_offset"):
                        self.assertEqual(actual[key], expected[key], key)

    def test_browser_impact_traverses_a_1100_unit_chain_iteratively(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            store.create_goal(goal_id="goal-deep", title="Deep graph", description="Iteration safety")
            store.create_work_unit(goal_id="goal-deep", work_unit_id="chain-0000", title="root")
            connection = sqlite3.connect(store.path)
            try:
                seed = connection.execute(
                    "SELECT goal_id,status,scope,checkpoint_id,created_at,updated_at "
                    "FROM work_units WHERE id='chain-0000'"
                ).fetchone()
                assert seed is not None
                connection.executemany(
                    "INSERT INTO work_units(id,goal_id,title,status,scope,checkpoint_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [(f"chain-{index:04d}", seed[0], f"chain {index}", seed[1], seed[2], seed[3], seed[4], seed[5])
                     for index in range(1, 1101)],
                )
                connection.executemany(
                    "INSERT INTO work_unit_dependencies(work_unit_id,prerequisite_id) VALUES(?,?)",
                    [(f"chain-{index:04d}", f"chain-{index - 1:04d}") for index in range(1, 1101)],
                )
                connection.commit()
            finally:
                connection.close()
            store.attest_ledger(actor_id="fixture-steward")
            snapshot = self._capture(store, root)
            deep = next(goal for goal in snapshot["goals"] if goal["id"] == "goal-deep")  # type: ignore[index]
            expected = dependency_impact(
                store, goal_id="goal-deep", work_unit_id="chain-1100",
                direction="prerequisites", limit=3, offset=1097,
            )
            actual = self._browser_helper({
                "goal": deep, "work_unit_id": "chain-1100", "direction": "prerequisites",
                "limit": 3, "offset": 1097,
            }, "impact")
            self.assertTrue(actual.pop("available"))
            self.assertEqual(actual["summary"], expected["summary"])
            self.assertEqual(actual["relations"], expected["relations"])
            self.assertEqual(actual["next_offset"], expected["next_offset"])

    def test_guidance_uses_one_captured_source_binding_and_quotes_powershell(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            snapshot = self._capture(store, root)
            # The browser only receives compact templates/context.  It must not
            # use a goal-local command or silently drop the source binding.
            snapshot["project"]["root"] = r"C:\O'Hare\tasktra"  # type: ignore[index]
            snapshot["guidance_context"]["cwd"] = r"C:\O'Hare\tasktra"  # type: ignore[index]
            snapshot["guidance_context"]["env"] = {"PYTHONPATH": r"C:\O'Hare\tasktra\src"}  # type: ignore[index]
            command = self._browser_helper({
                "snapshot": snapshot, "template_id": "work-impact",
                "values": {"goal_id": "goal-cockpit", "work_unit_id": "anchor", "limit": 20, "offset": 0},
            }, "guidance")
            context = snapshot["guidance_context"]  # type: ignore[assignment]
            self.assertEqual(command["argv"][:3], context["argv_prefix"])  # type: ignore[index]
            self.assertEqual(command["argv"][3:], [  # type: ignore[index]
                "work", "--root", r"C:\O'Hare\tasktra", "impact", "goal-cockpit", "anchor",
                "--direction", "both", "--limit", "20", "--offset", "0",
            ])
            self.assertIn("$env:PYTHONPATH='C:\\O''Hare\\tasktra\\src';", command["command_text"])
            self.assertIn("'C:\\O''Hare\\tasktra'", command["command_text"])

    def test_ten_thousand_units_remain_compact_and_do_not_duplicate_guidance(self) -> None:
        """The global unit bound is a real snapshot cap, not a UI-only promise."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            goal_ids = [f"stress-goal-{index}" for index in range(5)]
            for identifier in goal_ids:
                store.create_goal(goal_id=identifier, title=identifier, description="Compact-data stress fixture")
            connection = sqlite3.connect(store.path)
            try:
                goal = connection.execute(
                    "SELECT status,priority,authority,acceptance,budget_tokens,created_at,updated_at "
                    "FROM goals WHERE id='goal-cockpit'"
                ).fetchone()
                assert goal is not None
                created_at, updated_at = goal[5], goal[6]
                units = [
                    (f"stress-{goal_index}-{unit_index:04d}", f"stress-goal-{goal_index}", "compact", "planned",
                     '{"paths":["."],"exclusions":[]}', None, created_at, updated_at)
                    for goal_index in range(5) for unit_index in range(2000)
                ]
                connection.executemany(
                    "INSERT INTO work_units(id,goal_id,title,status,scope,checkpoint_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", units,
                )
                connection.commit()
            finally:
                connection.close()
            store.attest_ledger(actor_id="fixture-steward")
            snapshot = self._capture(store, root)
            encoded = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
            self.assertEqual(snapshot["completeness"]["work_units_total"], 10_000)  # type: ignore[index]
            self.assertEqual(snapshot["completeness"]["work_units_captured"], 10_000)  # type: ignore[index]
            self.assertEqual(len(snapshot["completeness"]["truncated_goal_ids"]), 0)  # type: ignore[index]
            self.assertEqual(encoded.count('"guidance_context"'), 1)
            self.assertEqual(encoded.count('"guidance_templates"'), 1)
            self.assertLess(len(encoded.encode("utf-8")), 20 * 1024 * 1024)
            self.assertLess(len(render(snapshot)), 24 * 1024 * 1024)

    def test_more_than_two_thousand_attention_goals_fails_before_any_export(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            # Use one legitimate write transaction so every synthetic goal has
            # lifecycle/audit evidence; all intentionally have missing work
            # and contracts, making the attention cap unambiguous.
            with store._connection() as connection:
                store._prepare_write(connection)
                seed = connection.execute(
                    "SELECT status,priority,authority,acceptance,budget_tokens,created_at,updated_at "
                    "FROM goals WHERE id='goal-cockpit'"
                ).fetchone()
                assert seed is not None
                for index in range(2000):
                    goal_id = f"attention-cap-{index:04d}"
                    connection.execute(
                        "INSERT INTO goals(id,title,description,status,priority,authority,acceptance,budget_tokens,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (goal_id, goal_id, "Attention cap fixture", *seed),
                    )
                    connection.execute(
                        "INSERT INTO budgets(goal_id,total_tokens,updated_at) VALUES(?,?,?)",
                        (goal_id, seed[4], seed[6]),
                    )
                    store._append_event_in_transaction(connection, "goal.created", goal_id=goal_id, payload={"title": goal_id})
            output = root / "must-not-exist.html"
            with self.assertRaisesRegex(StateError, "more than 2000 attention"):
                self._capture(store, root)
            self.assertFalse(output.exists())

    def test_renderer_is_deterministic_and_keeps_malicious_snapshot_text_in_data(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            snapshot = self._capture(store, root)
            attack = '</script><img src=x onerror="window.pwned=1">&\u2028cockpit-escape-marker'
            snapshot["project"]["name"] = attack  # type: ignore[index]
            self._goal(snapshot)["work_units"][0]["title"] = attack  # type: ignore[index]
            first = render(snapshot)
            self.assertEqual(first, render(snapshot))
            self.assertNotIn(b"</script><img", first)
            self.assertIn(b"\\u003c/script\\u003e\\u003cimg", first)
            style_hash = b64encode(sha256(CSS.encode("utf-8")).digest())
            script_hash = b64encode(sha256(JAVASCRIPT.encode("utf-8")).digest())
            self.assertIn(b"style-src 'sha256-" + style_hash + b"'", first)
            self.assertIn(b"script-src 'sha256-" + script_hash + b"'", first)
            self.assertNotIn(b"unsafe-inline", first)
            self.assertNotIn(b"nonce=", first)

    def test_caps_and_unavailable_exclusive_publication_fail_without_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            with patch("tasktra.operator_cockpit.MAX_SNAPSHOT_BYTES", 1):
                with self.assertRaisesRegex(StateError, "snapshot exceeds"):
                    self._capture(store, root)
            snapshot = self._capture(store, root)
            html_cap_output = root / "html-cap.html"
            with patch("tasktra.operator_cockpit.MAX_HTML_BYTES", 1):
                with self.assertRaisesRegex(StateError, "HTML exceeds"):
                    export_operator_cockpit(snapshot, html_cap_output)
            self.assertFalse(html_cap_output.exists())
            unsupported_output = root / "unsupported.html"
            with patch("tasktra.operator_cockpit.os.link", side_effect=OSError("unsupported")):
                with self.assertRaisesRegex(StateError, "exclusive publication failed"):
                    export_operator_cockpit(snapshot, unsupported_output)
            self.assertFalse(unsupported_output.exists())
            self.assertEqual(list(root.glob(".unsupported.html.*")), [])

    def test_export_is_exclusive_and_does_not_clobber_a_competing_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(directory)
            self._add_graph(store)
            snapshot = self._capture(store, root)
            output = root / "cockpit.html"
            start = threading.Barrier(2)
            outcomes: list[object] = []

            def export() -> None:
                try:
                    start.wait(timeout=10)
                    outcomes.append(export_operator_cockpit(snapshot, output))
                except BaseException as error:
                    outcomes.append(error)

            first, second = threading.Thread(target=export), threading.Thread(target=export)
            first.start(); second.start()
            first.join(timeout=15); second.join(timeout=15)
            self.assertFalse(first.is_alive() or second.is_alive(), "export race did not complete")
            successes = [item for item in outcomes if not isinstance(item, BaseException)]
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            self.assertEqual(len(successes), 1, outcomes)
            self.assertEqual(len(failures), 1, outcomes)
            self.assertTrue(output.is_file())
            rendered = output.read_bytes()
            self.assertEqual(rendered, render(snapshot))
            self.assertEqual(list(root.glob(".cockpit.html.*")), [], "owned temporary links leaked")


if __name__ == "__main__":
    unittest.main()
