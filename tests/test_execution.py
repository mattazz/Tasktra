import json
import sqlite3
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.execution import ExecutionError, ExecutionStore, HostExecutionAdapter


USAGE = {
    "input_tokens": 10,
    "cached_input_tokens": 2,
    "cache_write_input_tokens": 1,
    "output_tokens": 5,
    "reasoning_output_tokens": 3,
    "total_tokens": 15,
}


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ExecutionStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def plan_and_start(self, work_id="work-one", **extra):
        self.store.plan(work_id, "implementer", "gpt-6", "high", **extra)
        return self.store.start(work_id, "codex", "local", "thread-one", agent_id="agent-one")

    def rollout(self, usage=USAGE, *, model="gpt-6", effort="high", turn=None):
        path = self.root / "rollout.jsonl"
        events = [
            {"type": "session_meta", "payload": {"id": "thread-one", "source": {"subagent": {"thread_spawn": {"agent_path": "agent-one"}}}}},
        ]
        if turn is not None:
            events.append({"type": "turn_context", "payload": {"turn_id": turn, "model": model, "effort": effort}})
        events.append({"type": "token_usage_record", "payload": {
            "thread_id": "thread-one", "turn_id": turn, "response_id": "response-one", "usage": usage,
        }})
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return path

    def test_lifecycle_import_and_report_are_json_safe(self):
        self.plan_and_start()
        record = self.store.finish("work-one", "succeeded", rollout_path=self.rollout())
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(record["usage"], USAGE)
        self.assertIsNone(record["observed_model"])
        self.assertEqual(self.store.report()["executions"][0]["outcome"], "succeeded")
        json.dumps(self.store.report())

    def test_start_is_idempotent_but_scope_is_exclusive(self):
        first = self.plan_and_start()
        again = self.store.start("work-one", "codex", "local", "thread-one", agent_id="agent-one")
        self.assertEqual(first, again)
        self.store.plan("work-two", "reviewer", "gpt-6", "high")
        with self.assertRaisesRegex(ExecutionError, "overlaps"):
            self.store.start("work-two", "codex", "local", "thread-one")

    def test_turn_scopes_can_coexist_but_whole_scope_cannot(self):
        self.store.plan("one", "implementer", "gpt-6", "high")
        self.store.start("one", "codex", "local", "thread-one", turn_id="turn-one")
        self.store.plan("two", "implementer", "gpt-6", "high")
        self.store.start("two", "codex", "local", "thread-one", turn_id="turn-two")
        self.store.plan("three", "implementer", "gpt-6", "high")
        with self.assertRaises(ExecutionError):
            self.store.start("three", "codex", "local", "thread-one")

    def test_unknown_usage_requires_reason_and_planned_cannot_finish(self):
        self.store.plan("planned", "implementer", "gpt-6", "high")
        with self.assertRaises(ExecutionError):
            self.store.finish("planned", "failed", "missing-rollout")
        self.plan_and_start()
        with self.assertRaisesRegex(ExecutionError, "unknown usage"):
            self.store.finish("work-one", "failed")
        result = self.store.finish("work-one", "failed", "missing-rollout")
        self.assertEqual(result["unknown_reason"], "missing-rollout")

    def test_profile_rules_parent_and_coordinator_attribution(self):
        with self.assertRaises(ExecutionError):
            self.store.plan("one", "implementer", "gpt-6", "high", requested_model="gpt-7", requested_effort="high")
        with self.assertRaises(ExecutionError):
            self.store.plan("one", "coordinator", "gpt-6", "high")
        with self.assertRaises(ExecutionError):
            self.store.plan("one", "implementer", "gpt-6", "high", parent_work_id="absent")
        self.store.plan("parent", "implementer", "gpt-6", "high")
        self.store.plan("child", "coordinator", "gpt-6", "high", parent_work_id="parent", attribution_reason="delegated")
        coordinator = self.store.plan("coord-unknown", "coordinator", None, None, attribution_reason="aggregated")
        self.assertIsNone(coordinator["configured_model"])
        inherited = self.store.plan("host-inherit", "custom-role", None, None)
        self.assertIsNone(inherited["configured_effort"])
        self.store.start("host-inherit", "codex", "local", "thread-inherit",
                         observed_model="host-model", observed_effort="medium")
        partial = self.store.plan("partial-profile", "custom-role", "pinned-model", None)
        self.assertEqual(partial["configured_model"], "pinned-model")
        self.store.start("partial-profile", "codex", "local", "thread-partial",
                         observed_model="pinned-model", observed_effort="medium")
        with self.assertRaisesRegex(ExecutionError, "fallback_reason"):
            self.store.start("parent", "codex", "local", "thread-parent", observed_model="gpt-7", observed_effort="high")

    def test_reimport_replaces_counters_and_clears_missing_rollout_context(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        self.store.start("work-one", "codex", "local", "thread-one")
        path = self.rollout(turn="turn-one")
        first = self.store.import_codex_rollout("work-one", path)
        self.assertEqual(first["rollout_model"], "gpt-6")
        # Append a second turn without context.  The prior bytes remain
        # untouched, but whole-thread model coverage is now incomplete.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "token_usage_record", "payload": {
                "thread_id": "thread-one", "turn_id": "turn-two", "response_id": "response-two",
                "usage": {**USAGE, "output_tokens": 6, "total_tokens": 16},
            }}) + "\n")
        second = self.store.import_codex_rollout("work-one", path)
        self.assertEqual(second["usage"]["total_tokens"], 31)
        self.assertIsNone(second["rollout_model"])
        self.assertIsNone(second["rollout_effort"])

    def test_database_never_contains_source_path_or_prompt(self):
        self.plan_and_start()
        source = self.rollout()
        self.store.import_codex_rollout("work-one", source)
        raw = self.store.path.read_bytes()
        self.assertNotIn(str(source).encode(), raw)
        self.assertNotIn(b"a private prompt must never be stored", raw)

    def test_ledger_rejects_free_text_paths_and_credential_shaped_values(self):
        with self.assertRaises(ExecutionError):
            self.store.plan("work-one", "implementer", "gpt-6", "high",
                            requested_model="gpt-7", override_reason="a private prompt")
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        started = self.store.start("work-one", "codex", "local", "thread-one", agent_id="/root/private")
        self.assertIsNone(started["agent_id"])
        self.assertEqual(started["asserted_agent_id"], "/root/private")
        with self.assertRaises(ExecutionError):
            self.store.start("work-one", "codex", "local", "sk-abcdefghijklmnop")

    def test_rollout_mismatch_needs_fallback_and_post_finish_refreshes_unknown(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        self.store.start("work-one", "codex", "local", "thread-one", turn_id="turn-one")
        path = self.rollout(model="gpt-7", effort="high", turn="turn-one")
        with self.assertRaisesRegex(ExecutionError, "fallback_reason"):
            self.store.import_codex_rollout("work-one", path)
        self.store.import_codex_rollout("work-one", path, fallback_reason="provider-fallback")
        self.store.finish("work-one", "failed")

    def test_terminal_refresh_clears_or_requires_unknown_reason(self):
        self.plan_and_start()
        self.store.finish("work-one", "failed", "host-no-usage")
        measured = self.store.import_codex_rollout("work-one", self.rollout())
        self.assertIsNone(measured["unknown_reason"])
        self.assertEqual(self.store.import_codex_rollout("work-one", self.rollout())["usage"]["total_tokens"], 15)

    def test_rollout_agent_becomes_public_identity_when_host_did_not_report_one(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        self.store.start("work-one", "codex", "local", "thread-one")
        imported = self.store.import_codex_rollout("work-one", self.rollout())
        self.assertEqual(imported["agent_id"], "agent-one")
        self.assertIsNone(imported["host_agent_id"])

    def test_untouched_report_does_not_create_ledger_and_symlink_runtime_is_rejected(self):
        self.assertEqual(self.store.report()["work_count"], 0)
        self.assertFalse(self.store.path.exists())
        external = self.root / "external"
        external.mkdir()
        link = self.root / ".tasktra"
        try:
            os.symlink(external, link, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        with self.assertRaisesRegex(ExecutionError, "symbolic link"):
            self.store.plan("work-one", "implementer", "gpt-6", "high")

    def test_host_callback_facade(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        host = HostExecutionAdapter(self.store)
        host.started("work-one", provider="codex", host="local", thread_id="thread-one")
        self.assertEqual(host.finished("work-one", "cancelled", unknown_reason="host-no-usage")["state"], "cancelled")
        self.assertEqual(self.store.report()["asserted_executions"], [])

    def test_manual_assertions_are_separate_from_rollout_verified_identity(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        started = self.store.start("work-one", "codex", "local", "thread-one", turn_id="turn-one", agent_id="asserted-agent",
                                   observed_model="asserted-model", observed_effort="high", fallback_reason="host-fallback")
        self.assertEqual(started["start_provenance"], "manual-assertion")
        self.assertIsNone(started["agent_id"])
        imported = self.store.import_codex_rollout("work-one", self.rollout(turn="turn-one"))
        self.assertEqual(imported["agent_id"], "agent-one")
        self.assertEqual(imported["observed_model"], "gpt-6")
        self.assertEqual(imported["asserted_agent_id"], "asserted-agent")
        self.assertEqual(imported["usage_provenance"], "rollout-verified")
        report = self.store.report()
        self.assertEqual(report["asserted_executions"][0]["agent_id"], "asserted-agent")
        self.assertEqual(report["verified_executions"][0]["agent_id"], "agent-one")

    def test_append_only_rejects_rewrite_and_allows_new_response(self):
        self.plan_and_start()
        path = self.rollout()
        first = self.store.import_codex_rollout("work-one", path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "token_usage_record", "payload": {
                "thread_id": "thread-one", "response_id": "response-two",
                "usage": {**USAGE, "input_tokens": 12, "total_tokens": 17},
            }}) + "\n")
        grown = self.store.import_codex_rollout("work-one", path)
        self.assertEqual(grown["response_count"], 2)
        self.assertEqual(grown["usage"]["total_tokens"], 32)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-one"}}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ExecutionError, "prior evidence"):
            self.store.import_codex_rollout("work-one", path)
        self.assertEqual(self.store.get("work-one")["usage"]["total_tokens"], 32)

    def test_append_only_live_rollout_can_gain_first_usage_record(self):
        self.plan_and_start()
        path = self.root / "rollout.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-one"}}) + "\n", encoding="utf-8")
        first = self.store.import_codex_rollout("work-one", path)
        self.assertEqual(first["rollout_schema"], "none")
        self.assertIsNone(first["usage"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "token_usage_record", "payload": {
                "thread_id": "thread-one", "response_id": "response-one", "usage": USAGE,
            }}) + "\n")
        measured = self.store.import_codex_rollout("work-one", path)
        self.assertEqual(measured["rollout_schema"], "token_usage_record")
        self.assertEqual(measured["usage"]["total_tokens"], 15)

    def test_prebaseline_import_cannot_refresh_without_append_proof(self):
        self.plan_and_start()
        path = self.rollout()
        self.store.import_codex_rollout("work-one", path)
        connection = sqlite3.connect(self.store.path)
        try:
            with connection:
                connection.execute("UPDATE execution SET source_bytes=NULL WHERE work_id='work-one'")
        finally:
            connection.close()
        with self.assertRaisesRegex(ExecutionError, "append-only baseline"):
            self.store.import_codex_rollout("work-one", path)

    def test_finish_rollout_failure_is_atomic_and_retry_succeeds(self):
        self.plan_and_start()
        path = self.rollout()
        with self.assertRaisesRegex(ExecutionError, "unknown_reason"):
            self.store.finish("work-one", "succeeded", "not-needed", path)
        self.assertIsNone(self.store.get("work-one")["usage"])
        finished = self.store.finish("work-one", "succeeded", rollout_path=path)
        self.assertEqual(finished["state"], "succeeded")
        self.assertEqual(finished["finish_provenance"], "manual-assertion")

    def test_codex_rollout_cannot_be_imported_for_another_provider(self):
        self.store.plan("work-one", "implementer", "gpt-6", "high")
        self.store.start("work-one", "other-host", "local", "thread-one")
        with self.assertRaisesRegex(ExecutionError, "Codex provider"):
            self.store.import_codex_rollout("work-one", self.rollout())


if __name__ == "__main__":
    unittest.main()
