from datetime import datetime, timedelta, timezone
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from tasktra.execution import ExecutionStore
from tasktra.portal import make_portal_server, portal_snapshot
from tasktra.state import SCHEMA_VERSION, StateStore


def _project(root: Path) -> None:
    (root / ".tasktra").mkdir()
    (root / ".tasktra" / "project.toml").write_text(
        "[project]\nname = \"Portal fixture\"\nconfig_version = 1\n",
        encoding="utf-8",
    )


class PortalSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        _project(self.root)
        self.path = self.root / ".tasktra" / "runtime" / "tasktra.sqlite"
        self.store = StateStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_runtime_never_creates_a_database(self) -> None:
        snapshot = portal_snapshot(self.root)
        self.assertFalse(snapshot["runtime"]["available"])
        self.assertFalse(self.path.exists())
        self.assertEqual(snapshot["summary"]["jobs"], 0)

    def test_populated_snapshot_exposes_only_safe_status_fields(self) -> None:
        self.store.create_goal(
            goal_id="goal-one", title="Ship portal", description="Read status only",
            acceptance=["safe output"], budget_tokens=100,
        )
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Build portal")
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE work_units SET status='leased',lease_holder='lease-worker',lease_expires_at=?,attempt_count=2,last_outcome_class='retry' WHERE id='work-one'",
                (expired,),
            )
            connection.execute("UPDATE budgets SET consumed_tokens=12,reserved_tokens=7 WHERE goal_id='goal-one'")
            connection.commit()
        finally:
            connection.close()
        execution = ExecutionStore(self.root)
        execution.plan("work-one", "implementer", "gpt-6", "high")
        snapshot = portal_snapshot(self.root)
        self.assertTrue(snapshot["runtime"]["available"])
        self.assertEqual(snapshot["summary"]["goals"], 1)
        self.assertEqual(snapshot["summary"]["running_jobs"], 0)
        goal = snapshot["goals"][0]
        self.assertEqual(goal["progress_percent"], 0.0)
        self.assertEqual(goal["budget"], {"total_tokens": 100, "consumed_tokens": 12, "reserved_tokens": 7})
        job = snapshot["jobs"][0]
        self.assertTrue(job["lease_stale"])
        self.assertEqual(job["owner_id"], "lease-worker")
        agent = snapshot["agents"][0]
        self.assertEqual(agent["state"], "leased")
        self.assertNotIn("dispatched", agent["state"])
        rendered = json.dumps(snapshot)
        self.assertNotIn("lease_token_hash", rendered)
        self.assertNotIn("approval", rendered)
        self.assertNotIn(str(self.path), rendered)

    def test_old_or_malformed_runtime_is_reported_without_migration(self) -> None:
        self.path.parent.mkdir()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("CREATE TABLE old_runtime (id INTEGER)")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()
        before = self.path.read_bytes()
        snapshot = portal_snapshot(self.root)
        self.assertFalse(snapshot["runtime"]["available"])
        self.assertIn("requires migration", snapshot["runtime"]["message"])
        self.assertEqual(before, self.path.read_bytes())
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()

    def test_execution_ledger_planned_and_observed_rows_are_distinct(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        ledger = ExecutionStore(self.root)
        ledger.plan("work-one", "implementer", "gpt-6", "high")
        ledger.plan("outside-work", "reviewer", "gpt-6", "high")
        ledger.start("outside-work", "codex", "local", "thread-one", agent_id="agent-one")
        snapshot = portal_snapshot(self.root)
        planned = next(agent for agent in snapshot["agents"] if agent["work_id"] == "work-one")
        observed = next(agent for agent in snapshot["agents"] if agent["work_id"] == "outside-work")
        self.assertEqual((planned["state"], planned["goal_id"]), ("planned", "goal-one"))
        self.assertEqual((observed["state"], observed["goal_id"], observed["id"]), ("started", None, "outside-work"))

    def test_execution_only_project_is_visible_without_creating_state(self) -> None:
        ledger = ExecutionStore(self.root)
        ledger.plan("outside-work", "reviewer", None, None)
        snapshot = portal_snapshot(self.root)
        self.assertFalse(snapshot["runtime"]["available"])
        self.assertEqual((snapshot["summary"]["agents"], snapshot["agents"][0]["work_id"]), (1, "outside-work"))
        self.assertFalse(self.path.exists())

    def test_running_agents_respect_fresh_linked_leases(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        for work_id in ("expired", "fresh", "complete"):
            self.store.create_work_unit(goal_id="goal-one", work_unit_id=work_id, title=work_id)
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE work_units SET status='leased',lease_expires_at=? WHERE id='expired'", (expired,))
            connection.execute("UPDATE work_units SET status='leased',lease_expires_at=? WHERE id='fresh'", (fresh,))
            connection.execute("UPDATE work_units SET status='complete',lease_expires_at=? WHERE id='complete'", (expired,))
            connection.commit()
        finally:
            connection.close()
        ledger = ExecutionStore(self.root)
        for work_id in ("expired", "fresh", "complete"):
            ledger.plan(work_id, "worker", None, None)
            ledger.start(work_id, "codex", "local", f"thread-{work_id}")
        snapshot = portal_snapshot(self.root)
        self.assertEqual((snapshot["summary"]["running_jobs"], snapshot["summary"]["running_agents"]), (1, 1))
        agents = {agent["work_id"]: agent for agent in snapshot["agents"]}
        self.assertTrue(agents["expired"]["lease_stale"])
        self.assertFalse(agents["fresh"]["lease_stale"])
        self.assertTrue(agents["complete"]["lease_stale"])

    def test_stage_receipt_uses_parent_work_lease_and_paused_parent_is_not_running(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE work_units SET status='leased',lease_holder='worker-one',lease_expires_at=? WHERE id='work-one'",
                (fresh,),
            )
            connection.commit()
        ledger = ExecutionStore(self.root)
        ledger.plan("work-one", "coordinator", None, None, attribution_reason="parent-attribution")
        ledger.plan("work-one-implement", "implementer", None, None, parent_work_id="work-one")
        ledger.start("work-one-implement", "codex", "local", "thread-one")
        snapshot = portal_snapshot(self.root)
        self.assertEqual((snapshot["summary"]["agents"], snapshot["summary"]["running_agents"]), (1, 1))
        agent = snapshot["agents"][0]
        self.assertEqual((agent["work_id"], agent["parent_work_id"], agent["goal_id"]),
                         ("work-one-implement", "work-one", "goal-one"))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE work_units SET status='paused' WHERE id='work-one'")
            connection.commit()
        paused = portal_snapshot(self.root)
        self.assertEqual(paused["summary"]["running_agents"], 0)
        self.assertEqual(paused["agents"][0]["work_id"], "work-one-implement")
        self.assertTrue(paused["agents"][0]["lease_stale"])

    def test_fresh_lease_derives_one_agent_only_without_execution_record(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE work_units SET status='leased',lease_holder='worker-one',lease_expires_at=? WHERE id='work-one'", (fresh,))
            connection.commit()
        finally:
            connection.close()
        snapshot = portal_snapshot(self.root)
        self.assertEqual((snapshot["summary"]["agents"], snapshot["summary"]["running_agents"]), (1, 1))
        self.assertEqual((snapshot["agents"][0]["id"], snapshot["agents"][0]["provenance"]), ("worker-one", "work-lease"))

    def test_current_lease_replaces_planned_or_terminal_execution_receipt(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        for work_id in ("planned", "terminal", "expired"):
            self.store.create_work_unit(goal_id="goal-one", work_unit_id=work_id, title=work_id)
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        connection = sqlite3.connect(self.path)
        try:
            for work_id, expiry in (("planned", fresh), ("terminal", fresh), ("expired", expired)):
                connection.execute(
                    "UPDATE work_units SET status='leased',lease_holder=?,lease_expires_at=? WHERE id=?",
                    (f"{work_id}-worker", expiry, work_id),
                )
            connection.commit()
        finally:
            connection.close()
        ledger = ExecutionStore(self.root)
        ledger.plan("planned", "worker", "gpt-6", "high")
        ledger.plan("expired", "worker", "gpt-6", "high")
        ledger.plan("terminal", "worker", "gpt-6", "high")
        ledger.start("terminal", "codex", "local", "thread-terminal")
        ledger.finish("terminal", "succeeded", "unknown-usage")
        snapshot = portal_snapshot(self.root)
        agents = {agent["work_id"]: agent for agent in snapshot["agents"]}
        for work_id in ("planned", "terminal", "expired"):
            self.assertEqual((agents[work_id]["state"], agents[work_id]["provenance"]), ("leased", "work-lease"))
            self.assertIsNone(agents[work_id]["model"])
            self.assertIsNone(agents[work_id]["total_tokens"])
        self.assertTrue(agents["expired"]["lease_stale"])
        self.assertEqual((snapshot["summary"]["agents"], snapshot["summary"]["running_agents"]), (3, 2))

    def test_unavailable_optional_ledger_does_not_hide_runtime_workers(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE work_units SET status='leased',lease_holder='worker-one',lease_expires_at=? WHERE id='work-one'",
                (fresh,),
            )
            connection.commit()
        ledger_path = self.path.with_name("agent-execution.sqlite")
        for mode in ("old-schema", "corrupt", "directory"):
            with self.subTest(mode=mode):
                if mode == "old-schema":
                    with closing(sqlite3.connect(ledger_path)) as ledger:
                        ledger.execute("CREATE TABLE execution (work_id TEXT PRIMARY KEY)")
                        ledger.commit()
                elif mode == "corrupt":
                    ledger_path.write_bytes(b"not a sqlite database")
                else:
                    ledger_path.mkdir()
                snapshot = portal_snapshot(self.root)
                self.assertTrue(snapshot["runtime"]["available"])
                self.assertEqual(snapshot["summary"]["running_jobs"], 1)
                self.assertEqual(snapshot["summary"]["running_agents"], 1)
                self.assertEqual(snapshot["summary"]["agents"], 1)
                self.assertEqual(snapshot["agents"][0]["id"], "worker-one")
                self.assertEqual(snapshot["agents"][0]["provenance"], "work-lease")
                self.assertEqual(len(snapshot["warnings"]), 1)
                if mode == "directory":
                    ledger_path.rmdir()
                else:
                    ledger_path.unlink()

    def test_verified_rollout_observation_takes_precedence(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        ledger = ExecutionStore(self.root)
        ledger.plan("work-one", "worker", None, None)
        ledger.start("work-one", "codex", "local", "thread-one", agent_id="host-agent",
                     turn_id="turn-one", observed_model="host-model", observed_effort="medium")
        rollout = self.root / "rollout.jsonl"
        usage = {"input_tokens": 1, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                 "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 2}
        rollout.write_text("".join(json.dumps(event) + "\n" for event in (
            {"type": "session_meta", "payload": {"id": "thread-one", "source": {"subagent": {"thread_spawn": {"agent_path": "rollout-agent"}}}}},
            {"type": "turn_context", "payload": {"turn_id": "turn-one", "model": "rollout-model", "effort": "high"}},
            {"type": "token_usage_record", "payload": {"thread_id": "thread-one", "turn_id": "turn-one", "response_id": "response-one", "usage": usage}},
        )), encoding="utf-8")
        ledger.import_codex_rollout("work-one", rollout)
        agent = portal_snapshot(self.root)["agents"][0]
        self.assertEqual((agent["id"], agent["model"], agent["effort"], agent["provenance"], agent["total_tokens"]),
                         ("rollout-agent", "rollout-model", "high", "rollout-verified", 2))

    def test_verified_rollout_without_context_clears_manual_observation(self) -> None:
        self.store.create_goal(goal_id="goal-one", title="One", description="One")
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Work")
        ledger = ExecutionStore(self.root)
        ledger.plan("work-one", "worker", None, None)
        ledger.start("work-one", "codex", "local", "thread-one", agent_id="manual-agent",
                     observed_model="manual-model", observed_effort="medium")
        rollout = self.root / "rollout.jsonl"
        usage = {"input_tokens": 1, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                 "output_tokens": 1, "reasoning_output_tokens": 0, "total_tokens": 2}
        rollout.write_text("".join(json.dumps(event) + "\n" for event in (
            {"type": "session_meta", "payload": {"id": "thread-one"}},
            {"type": "token_usage_record", "payload": {
                "thread_id": "thread-one", "turn_id": None, "response_id": "response-one", "usage": usage,
            }},
        )), encoding="utf-8")
        ledger.import_codex_rollout("work-one", rollout)
        agent = portal_snapshot(self.root)["agents"][0]
        self.assertEqual((agent["id"], agent["model"], agent["effort"], agent["provenance"]),
                         ("work-one", None, None, "rollout-verified"))


class PortalHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        _project(self.root)
        self.server = make_portal_server(self.root, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, **headers: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers={"Host": f"127.0.0.1:{self.server.server_port}", **headers})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_snapshot_is_local_only_and_no_store(self) -> None:
        status, headers, payload = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(json.loads(payload)["schema_version"], 1)
        self.assertEqual(self.request("HEAD", "/api/snapshot")[2], b"")
        self.assertEqual(self.request("GET", "/api/snapshot", Host="evil.example")[0], 400)
        self.assertEqual(self.request("GET", "/api/snapshot", Origin="http://evil.example")[0], 403)

    def test_only_fixed_paths_and_read_methods_are_available(self) -> None:
        for path in ("/", "/index.html", "/app.js", "/styles.css"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.request("POST", "/api/snapshot")[0], 405)
        self.assertEqual(self.request("POST", "/api/snapshot", Host="evil.example")[0], 400)
        self.assertEqual(self.request("GET", "/api/snapshot?anything=1")[0], 404)
        self.assertEqual(self.request("GET", "/../state.sqlite")[0], 404)
        self.assertEqual(self.request("GET", "/not-a-route")[0], 404)

    def test_invalid_port_is_rejected_before_binding(self) -> None:
        with self.assertRaises(ValueError):
            make_portal_server(self.root, port=-1)
        with self.assertRaises(ValueError):
            make_portal_server(self.root, port=65536)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
