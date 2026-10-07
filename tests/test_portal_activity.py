import hashlib
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.execution import ExecutionStore
from tasktra.portal_activity import _CACHE, agent_activity


USAGE = {"input_tokens": 8, "cached_input_tokens": 3, "cache_write_input_tokens": 0,
         "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 10}


class PortalActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".tasktra").mkdir()
        (self.root / ".tasktra" / "project.toml").write_text('[project]\nname="activity"\nconfig_version=1\n', encoding="utf-8")
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.store = ExecutionStore(self.root)
        self.store.plan("work-one", "worker", None, None)
        self.store.start("work-one", "codex", "local", "thread-one", turn_id="turn-one")
        self.rollout = self.sessions / "rollout-one.jsonl"
        self._set_source()

    def tearDown(self) -> None:
        _CACHE.clear()
        self.temp.cleanup()

    def _set_source(self) -> None:
        digest = hashlib.sha256(str(self.rollout.resolve()).encode("utf-8")).hexdigest()
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute("UPDATE execution SET source_path_sha256=? WHERE work_id='work-one'", (digest,))
            connection.commit()
        finally:
            connection.close()

    def _write(self, events: list[dict]) -> None:
        self.rollout.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

    def _meta(self) -> dict:
        return {"type": "session_meta", "payload": {"id": "thread-one", "cwd": str(self.root)}}

    def test_allowlists_only_public_assistant_channels_and_tool_metadata(self) -> None:
        self._write([
            self._meta(),
            {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "message", "role": "assistant", "channel": "analysis", "content": "hidden-analysis"}}},
            {"timestamp": "2026-01-01T00:01:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "message", "role": "user", "channel": "commentary", "content": "hidden-user"}}},
            {"timestamp": "2026-01-01T00:01:30Z", "type": "response_item", "payload": {"turn_id": "other-turn", "item": {"type": "message", "role": "assistant", "channel": "commentary", "content": "other-turn-output"}}},
            {"timestamp": "2026-01-01T00:02:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "message", "role": "assistant", "channel": "commentary", "content": [{"type": "output_text", "text": "Safe progress"}]}}},
            {"timestamp": "2026-01-01T00:03:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "function_call", "name": "rg", "call_id": "call-one", "status": "in_progress", "arguments": "secret-argument"}}},
            {"timestamp": "2026-01-01T00:04:00Z", "type": "token_usage_record", "payload": {"thread_id": "thread-one", "turn_id": "turn-one", "response_id": "response", "usage": USAGE}},
        ])
        value = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        rendered = json.dumps(value)
        self.assertTrue(value["available"])
        self.assertEqual(value["phase"], "tool_running")
        self.assertEqual([item["kind"] for item in value["events"]], ["progress", "tool"])
        self.assertEqual(value["usage"]["total_tokens"], 10)
        for secret in ("hidden-analysis", "hidden-user", "secret-argument", "other-turn-output"):
            self.assertNotIn(secret, rendered)

    def test_rejects_unregistered_source_and_wrong_thread_or_cwd(self) -> None:
        self._write([{"type": "session_meta", "payload": {"id": "wrong", "cwd": str(self.root)}}])
        value = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertEqual((value["available"], value["source"], value["coverage"]["reason"]), (False, "none", "unverified-rollout"))
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute("UPDATE execution SET source_path_sha256=NULL WHERE work_id='work-one'")
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(agent_activity(self.root, "work-one", sessions_root=self.sessions)["available"])

    def test_growing_rollout_uses_cached_incremental_read_and_marks_partial_line(self) -> None:
        self._write([self._meta(), {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "message", "role": "assistant", "channel": "commentary", "content": "First"}}}])
        first = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": "2026-01-01T00:01:00Z", "type": "response_item", "payload": {"turn_id": "turn-one", "item": {"type": "message", "role": "assistant", "channel": "final", "content": "Final"}}}) + "\n")
            handle.write('{"type":"response_item"')
        second = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertEqual([item["text"] for item in second["events"]], ["First", "Final"])
        self.assertEqual(second["coverage"]["reason"], "growing-rollout")
        self.assertLessEqual(_CACHE[(str(self.root.resolve()), "work-one", str(self.sessions))]["offset"], self.rollout.stat().st_size)

    def test_oversized_line_is_skipped_without_exposure(self) -> None:
        self.rollout.write_bytes((json.dumps(self._meta()) + "\n").encode() + b"x" * (256 * 1024 + 5) + b"\n")
        value = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertTrue(value["available"])
        self.assertEqual(value["coverage"]["reason"], "oversized-rollout-event")


    def test_native_direct_payload_uses_turn_context_and_turn_cwd(self) -> None:
        self._write([
            self._meta(),
            {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}},
            {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "native visible"}]}},
            {"type": "turn_context", "payload": {"turn_id": "other", "cwd": str(self.root)}},
            {"timestamp": "2026-01-01T00:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "other hidden"}]}},
        ])
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertEqual([item["text"] for item in result["events"]], ["native visible"])
        _CACHE.clear()
        self._write([self._meta(), {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.sessions)}}, {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "blocked"}}])
        self.assertFalse(agent_activity(self.root, "work-one", sessions_root=self.sessions)["available"])


    def test_lost_record_clears_turn_context_before_bare_message(self) -> None:
        self.rollout.write_bytes((json.dumps(self._meta()) + "\n" + json.dumps({"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}) + "\n" + "x" * (256 * 1024 + 5) + "\n" + json.dumps({"timestamp": "2026-01-01T00:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "must-not-leak"}}) + "\n").encode())
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertNotIn("must-not-leak", json.dumps(result))


    def test_malformed_turn_context_clears_previous_turn(self) -> None:
        self._write([self._meta(), {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"type": "turn_context", "payload": {"turn_id": None}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "must-not-leak-malformed"}}])
        self.assertNotIn("must-not-leak-malformed", json.dumps(agent_activity(self.root, "work-one", sessions_root=self.sessions)))


    def test_native_final_answer_phase_is_public_final(self) -> None:
        self._write([self._meta(), {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": "public completion"}}])
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertEqual([(item["kind"], item["text"]) for item in result["events"]], [("final", "public completion")])

    def test_untrusted_discriminators_and_recursive_json_are_unavailable_or_skipped(self) -> None:
        nested = "[" * 1100 + "]" * 1100
        self.rollout.write_text(json.dumps(self._meta()) + "\n" + nested + "\n" + json.dumps({"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}) + "\n" + json.dumps({"type": "response_item", "payload": {"type": ["message"], "role": "assistant", "phase": {"bad": "value"}, "content": "must-not-expose"}}) + "\n", encoding="utf-8")
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertTrue(result["available"])
        self.assertNotIn("must-not-expose", json.dumps(result))

    def test_same_size_session_header_rewrite_invalidates_cached_binding(self) -> None:
        self._write([self._meta(), {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "first"}}])
        self.assertTrue(agent_activity(self.root, "work-one", sessions_root=self.sessions)["available"])
        original_size = self.rollout.stat().st_size
        rewritten = {"type": "session_meta", "payload": {"id": "thread-two", "cwd": str(self.root)}}
        self._write([rewritten, {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "other"}}])
        self.assertEqual(self.rollout.stat().st_size, original_size)
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertFalse(result["available"])
        self.assertNotIn("other", json.dumps(result))

    def test_growing_rewrite_invalidates_cached_binding(self) -> None:
        self._write([self._meta(), {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "first"}}])
        self.assertTrue(agent_activity(self.root, "work-one", sessions_root=self.sessions)["available"])
        rewritten = {"type": "session_meta", "payload": {"id": "thread-two", "cwd": str(self.root)}}
        self._write([rewritten, {"type": "turn_context", "payload": {"turn_id": "turn-one", "cwd": str(self.root)}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "replacement"}}, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "appended"}}])
        result = agent_activity(self.root, "work-one", sessions_root=self.sessions)
        self.assertFalse(result["available"])
        self.assertNotIn("replacement", json.dumps(result))
        self.assertNotIn("appended", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
