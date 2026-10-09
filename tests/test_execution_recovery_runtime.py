"""Runtime coverage for the observation-only recovery adapter."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import unittest

from tasktra.compiler import load_catalog
from tasktra.config import load_project_config
from tasktra.execution_recovery import reconcile_codex_run, unresolved_codex_runs, execution_attention_counts
from tests import test_codex_runs_cli as fixture_module

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fixture():
    case = fixture_module.CodexRunCliTests(); case.setUp()
    try: yield case
    finally: case.tearDown()


def prepare(case, key="recovery-runtime"):
    return case.store.prepare_codex_run(
        attempt_id=case.claim["attempt_id"], performer_id="worker", lease_token=case.token,
        catalog=load_catalog(ROOT / "catalog"), config=load_project_config(case.root),
        routing_request=json.loads(case.request.read_text(encoding="utf-8")), idempotency_key=key,
    )


def observation(run, kind="running"):
    name = "/root/" + run["requested_task_name"]
    return {"kind": "tasktra.codex-host-tree-observation", "version": 1,
            "source": "collaboration.list_agents", "captured_at": "2031-01-01T00:00:00Z",
            "parent_canonical_name": "/root", "observed_agent_names": [name],
            "target": {"canonical_name": name, "agent_id": None,
                       "status": {"kind": kind, "source_shape": "running-string" if kind == "running" else "completed-object"}}}


class ExecutionRecoveryRuntimeTests(unittest.TestCase):
    def test_discovery_and_running_then_completed(self):
        with fixture() as case:
            run = prepare(case)
            page = unresolved_codex_runs(case.store, at="2031-01-01T00:00:00Z")
            self.assertEqual(page["items"][0]["reason"], "prepared-unobserved")
            self.assertTrue(page["items"][0]["capacity"]["parent_lease_counted"])
            started = reconcile_codex_run(case.store, run["run_id"], "observer", observation(run), at="2031-01-01T00:00:00Z")
            self.assertEqual((started["state"], started["mutation"]), ("started", "applied"))
            before_retry = sha256(case.store.path.read_bytes()).hexdigest()
            retried = reconcile_codex_run(case.store, run["run_id"], "other", observation(run), at="2031-01-01T00:00:01Z")
            self.assertEqual((retried["mutation"], retried["idempotent"]), ("none", True))
            self.assertEqual(retried["reconciliation_observer"], "other")
            self.assertEqual(retried["recorded_attribution"], {
                "start": {"observed_by": "observer", "recorded_at": "2031-01-01T00:00:00Z"}, "finish": None,
            })
            self.assertEqual(sha256(case.store.path.read_bytes()).hexdigest(), before_retry)
            self.assertEqual(unresolved_codex_runs(case.store)["items"][0]["reason"], "started-unterminated")
            result = b"\r\ncompleted \xe2\x98\x83"
            finished = reconcile_codex_run(case.store, run["run_id"], "finisher", observation(run, "completed"), result, at="2031-01-02T00:00:00Z")
            self.assertEqual(finished["result"]["sha256"], sha256(result).hexdigest())
            self.assertEqual(finished["reconciliation_observer"], "finisher")
            self.assertEqual(finished["recorded_attribution"], {
                "start": {"observed_by": "observer", "recorded_at": "2031-01-01T00:00:00Z"},
                "finish": {"observed_by": "finisher", "recorded_at": "2031-01-02T00:00:00Z"},
            })
            self.assertEqual(unresolved_codex_runs(case.store)["items"], [])

    def test_completed_is_atomic_and_exact_retry_preserves_actor(self):
        with fixture() as case:
            run = prepare(case)
            result = b""
            first = reconcile_codex_run(case.store, run["run_id"], "observer", observation(run, "completed"), result, at="2031-01-01T00:00:00Z")
            second = reconcile_codex_run(case.store, run["run_id"], "other", observation(run, "completed"), result, at="2031-01-02T00:00:00Z")
            self.assertEqual((first["state"], second["mutation"], second["result"]["observed_by"]), ("finished", "none", "observer"))
            self.assertEqual(second["reconciliation_observer"], "other")
            self.assertEqual(second["recorded_attribution"], first["recorded_attribution"])
            with case.store._readonly_connection() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_starts").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM codex_run_finishes").fetchone()[0], 1)

    def test_counts_are_connection_scoped_and_detached_is_stored_status(self):
        with fixture() as case:
            run = prepare(case)
            with case.store._readonly_connection() as connection:
                case.store._prepare_readonly(connection)
                self.assertEqual(execution_attention_counts(connection, "goal-one"),
                                 {"unresolved": 1, "prepared_unobserved": 1, "started_unterminated": 0, "detached_capacity": 0})
            with case.store._connection() as connection:
                case.store._prepare_write(connection)
                connection.execute("UPDATE work_attempts SET status='blocked' WHERE id=?", (case.claim["attempt_id"],))
            with case.store._readonly_connection() as connection:
                case.store._prepare_readonly(connection)
                self.assertEqual(execution_attention_counts(connection)["detached_capacity"], 1)
