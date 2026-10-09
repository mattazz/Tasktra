"""Focused resilience checks for the closed execution-recovery adapter."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import timedelta
from hashlib import sha256
import sqlite3
import unittest
from unittest.mock import patch

from tasktra.autonomy import AutonomyError, AutonomyStore
from tasktra.execution_recovery import reconcile_codex_run, unresolved_codex_runs
from tasktra import execution_recovery as recovery_module
from tests import test_codex_runs_capacity as capacity_fixture
from tests import test_execution_recovery_integration as integration_fixture


NOW = capacity_fixture.NOW


@contextmanager
def fixture():
    case = capacity_fixture.CodexCapacityTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def observation(run: dict, *, kind: str = "running", parent: str = "/root", target: str | None = None) -> dict:
    name = target or parent + "/" + run["requested_task_name"]
    return {
        "kind": "tasktra.codex-host-tree-observation", "version": 1,
        "source": "collaboration.list_agents", "captured_at": "2030-01-01T00:00:00Z",
        "parent_canonical_name": parent, "observed_agent_names": [name],
        "target": {"canonical_name": name, "agent_id": None,
                   "status": {"kind": kind, "source_shape": "completed-object" if kind == "completed" else "running-string"}},
    }


def reconcile(store, run: dict, *, actor: str, kind: str = "completed", result: bytes | None = b"result"):
    return reconcile_codex_run(
        store, run["run_id"], actor, observation(run, kind=kind),
        result if kind == "completed" else None, at="2030-01-01T00:00:00Z",
    )


def _database_bytes(path):
    return tuple(candidate.read_bytes() for candidate in (path, path.with_name(path.name + "-wal")) if candidate.exists())


class ExecutionRecoveryResilienceTests(unittest.TestCase):
    def test_two_store_barriers_classify_conflicts_and_running_completed_orders(self):
        with fixture() as case:
            case.add_unit("unit-one")
            run = case.prepare(case.claim(), "resilience-digest")
            results = case.race([
                lambda store: reconcile(store, run, actor="first", result=b"first result"),
                lambda store: reconcile(store, run, actor="second", result=b"second result"),
            ])
            winners = [item for item in results if isinstance(item, dict)]
            conflicts = [item for item in results if isinstance(item, AutonomyError)]
            self.assertEqual((len(winners), len(conflicts)), (1, 1))
            self.assertIn("idempotency_conflict", str(conflicts[0]))
            shown = case.store.get_codex_run(run["run_id"])
            self.assertEqual(shown["result"]["sha256"], winners[0]["result"]["sha256"])
            with case.store._readonly_connection() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_starts").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_finishes").fetchone()[0], 1)

        with fixture() as case:
            case.add_unit("unit-one")
            run = case.prepare(case.claim(), "resilience-identity")
            alternate = observation(run, parent="/alternate")
            results = case.race([
                lambda store: reconcile_codex_run(store, run["run_id"], "root-observer", observation(run), at="2030-01-01T00:00:00Z"),
                lambda store: reconcile_codex_run(store, run["run_id"], "alternate-observer", alternate, at="2030-01-01T00:00:00Z"),
            ])
            winners = [item for item in results if isinstance(item, dict)]
            conflicts = [item for item in results if isinstance(item, AutonomyError)]
            self.assertEqual((len(winners), len(conflicts)), (1, 1))
            self.assertIn("idempotency_conflict", str(conflicts[0]))
            self.assertEqual(case.store.get_codex_run(run["run_id"])["state"], "started")

        for completed_first in (False, True):
            with self.subTest(completed_first=completed_first), fixture() as case:
                case.add_unit("unit-one")
                run = case.prepare(case.claim(), "resilience-order-" + str(completed_first).lower())
                first, second = AutonomyStore(case.path), AutonomyStore(case.path)
                if completed_first:
                    winner = reconcile(first, run, actor="completed-first")
                    follower = reconcile(second, run, actor="running-second", kind="running", result=None)
                    self.assertTrue(follower["ignored_stale_observation"])
                    self.assertEqual((winner["mutation"], follower["mutation"]), ("applied", "none"))
                else:
                    first_result = reconcile(first, run, actor="running-first", kind="running", result=None)
                    winner = reconcile(second, run, actor="completed-second")
                    self.assertEqual((first_result["mutation"], winner["mutation"]), ("applied", "applied"))
                shown = case.store.get_codex_run(run["run_id"])
                self.assertEqual(shown["state"], "finished")
                with case.store._readonly_connection() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_starts").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_finishes").fetchone()[0], 1)

    def test_unresolved_keyset_goal_filter_limit_and_fixed_capture(self):
        with fixture() as case:
            case.config = capacity_fixture.ProjectConfig(name="resilience", concurrency_limit=5)
            claims, runs = [], []
            for goal_id, unit_id in (("goal-one", "unit-one"), ("goal-two", "unit-two"), ("goal-three", "unit-three")):
                if goal_id != "goal-one":
                    case.seed_goal(goal_id)
                case.add_unit(unit_id, goal_id)
                claim = case.claim(goal_id=goal_id)
                claims.append(claim)
                runs.append(case.prepare(claim, "resilience-page-" + goal_id))

            at = "2030-01-01T00:00:15Z"
            all_items = unresolved_codex_runs(case.store, limit=3, at=at)
            ordered = [item["run_id"] for item in all_items["items"]]
            self.assertEqual(ordered, sorted(ordered))
            self.assertIsNone(all_items["next_after_run_id"])
            for item, claim in zip(sorted(all_items["items"], key=lambda value: value["goal_id"]), sorted(claims, key=lambda value: value["goal_id"])):
                self.assertEqual(item["reason"], "prepared-unobserved")
                self.assertEqual(item["age_seconds"], 15)
                self.assertEqual(item["parent"], {"status": "leased", "is_current": True, "is_live": True, "work_unit_status": "leased"})
                self.assertEqual(item["attempt"]["status"], "leased")
                self.assertEqual(item["attempt"]["is_live"], item["parent"]["is_live"])
                self.assertEqual(item["goal_id"], claim["goal_id"])

            first = unresolved_codex_runs(case.store, limit=1, at=at)
            second = unresolved_codex_runs(case.store, limit=1, after_run_id=first["next_after_run_id"], at=at)
            third = unresolved_codex_runs(case.store, limit=1, after_run_id=second["next_after_run_id"], at=at)
            self.assertEqual([first["items"][0]["run_id"], second["items"][0]["run_id"], third["items"][0]["run_id"]], ordered)
            self.assertIsNone(third["next_after_run_id"])
            self.assertEqual([item["goal_id"] for item in unresolved_codex_runs(case.store, goal_id="goal-two", limit=1, at=at)["items"]], ["goal-two"])
            for invalid in (0, 101):
                with self.assertRaises(AutonomyError):
                    unresolved_codex_runs(case.store, limit=invalid, at=at)

    def test_unresolved_read_preserves_wal_bytes_and_uses_verified_snapshot_during_completion(self):
        with fixture() as case:
            case.add_unit("unit-one")
            claim = case.claim()
            run = case.prepare(claim, "resilience-wal")
            with closing(sqlite3.connect(case.path)) as keeper:
                self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                keeper.execute("BEGIN")
                keeper.execute("SELECT count(*) FROM goals").fetchone()
                case.store.create_goal(goal_id="wal-write", title="WAL", description="WAL", acceptance=["Done"])
                before = _database_bytes(case.path)
                unresolved_codex_runs(case.store, at="2030-01-01T00:00:00Z")
                self.assertEqual(_database_bytes(case.path), before)
                original = recovery_module._base_projection

                def complete_after_snapshot(store, connection, row, *, captured):
                    reconcile(case.store, run, actor="snapshot-completer")
                    return original(store, connection, row, captured=captured)

                with patch.object(recovery_module, "_base_projection", side_effect=complete_after_snapshot):
                    prior = unresolved_codex_runs(case.store, at="2030-01-01T00:00:00Z")
                self.assertEqual([item["run_id"] for item in prior["items"]], [run["run_id"]])
                self.assertEqual(unresolved_codex_runs(case.store, at="2030-01-01T00:00:00Z")["items"], [])

    def test_originating_tokens_at_supported_lengths_are_rejected_and_never_persisted(self):
        tokens = ("x" * 32, "λ" * 64, "密" * 512)
        for token in tokens:
            with self.subTest(length=len(token), token_kind=token[:1]), fixture() as case:
                case.token = token
                case.add_unit("unit-one")
                run = case.prepare(case.claim(), "resilience-token-" + str(len(token)))
                carriers = [("result", "observer", observation(run, kind="completed"), token.encode("utf-8"))]
                if token.isascii():
                    base = observation(run); base["parent_canonical_name"] = "/" + token
                    carriers.extend([
                        ("observer", token, observation(run), None),
                        ("parent", "observer", base, None),
                        ("names", "observer", {**observation(run), "observed_agent_names": ["/root/" + token]}, None),
                        ("target", "observer", observation(run, target="/root/" + token), None),
                    ])
                for carrier, actor, payload, result in carriers:
                    with self.subTest(carrier=carrier):
                        before = _database_bytes(case.path)
                        with self.assertRaises(AutonomyError) as raised:
                            reconcile_codex_run(case.store, run["run_id"], actor, payload, result, at="2030-01-01T00:00:00Z")
                        self.assertNotIn(token, str(raised.exception))
                        self.assertEqual(_database_bytes(case.path), before)
                        self.assertNotIn(token.encode("utf-8"), b"".join(_database_bytes(case.path)))

    def test_exact_completed_result_is_digest_only_in_database_wal_audit_and_projection(self):
        marker = b"private-result-marker::c0c9d5b7::\xe2\x98\x83::end"
        with fixture() as case:
            case.add_unit("unit-one")
            run = case.prepare(case.claim(), "resilience-private-result")
            result = reconcile(case.store, run, actor="observer", result=marker)
            self.assertEqual(result["result"]["sha256"], sha256(marker).hexdigest())
            shown = case.store.get_codex_run(run["run_id"])
            self.assertNotIn(marker.decode("utf-8"), repr(result))
            self.assertNotIn(marker.decode("utf-8"), repr(shown))
            with case.store._readonly_connection() as connection:
                audit = "\n".join(str(row[0]) for row in connection.execute("SELECT payload FROM audit_events"))
            self.assertNotIn(marker.decode("utf-8"), audit)
            self.assertNotIn(marker, b"".join(_database_bytes(case.path)))


if __name__ == "__main__":
    unittest.main()
