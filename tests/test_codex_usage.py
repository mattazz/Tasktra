from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.codex_usage import RolloutUsageError, scan_rollout


def usage(input_tokens: int, output_tokens: int, *, cached: int = 0, cache_write: int = 0,
          reasoning: int = 0) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output_tokens,
    }


class ScanRolloutTests(unittest.TestCase):
    def write_rollout(self, events: list[dict[str, object]]) -> Path:
        path = Path(self.temporary.name) / "rollout.jsonl"
        path.write_bytes(b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events))
        return path

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def meta(thread_id: str = "child") -> dict[str, object]:
        return {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "session_id": "root-host-session",
                "source": {"subagent": {"thread_spawn": {"agent_path": "/root/child"}}},
            },
        }

    def test_scans_child_thread_records_by_thread_id_and_deduplicates_per_turn(self) -> None:
        first = usage(10, 4, cached=8)
        second = usage(7, 3, reasoning=1)
        path = self.write_rollout([
            self.meta(),
            {"type": "turn_context", "payload": {"turn_id": "a", "model": "gpt-test", "effort": "high"}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "session_id": "root-host-session", "turn_id": "a", "response_id": "r", "usage": first}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "session_id": "root-host-session", "turn_id": "a", "response_id": "r", "usage": first}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "b", "response_id": "r", "usage": second}},
        ])

        observation = scan_rollout(path, "child", "a")

        self.assertEqual(observation.usage, first)
        self.assertEqual(observation.response_count, 1)
        self.assertEqual(len(observation.response_fingerprints), 1)
        self.assertRegex(observation.response_fingerprints[0], r"^[0-9a-f]{64}$")
        self.assertEqual((observation.agent_id, observation.model, observation.effort),
                         ("/root/child", "gpt-test", "high"))
        self.assertEqual(observation.source_sha256, sha256(path.read_bytes()).hexdigest())

    def test_duplicate_response_ids_are_scoped_by_turn_and_conflicts_are_rejected(self) -> None:
        path = self.write_rollout([
            self.meta(),
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "a", "response_id": "same", "usage": usage(1, 1)}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "b", "response_id": "same", "usage": usage(2, 1)}},
        ])
        self.assertEqual(scan_rollout(path, "child").usage["total_tokens"], 5)  # type: ignore[index]

        conflicting = self.write_rollout([
            self.meta(),
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "a", "response_id": "same", "usage": usage(1, 1)}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "a", "response_id": "same", "usage": usage(2, 1)}},
        ])
        with self.assertRaisesRegex(RolloutUsageError, "conflicting duplicate"):
            scan_rollout(conflicting, "child")

    def test_model_attribution_needs_complete_consistent_coverage(self) -> None:
        path = self.write_rollout([
            self.meta(),
            {"type": "turn_context", "payload": {"turn_id": "a", "model": "gpt-test", "effort": "high"}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "a", "response_id": "a", "usage": usage(1, 1)}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "b", "response_id": "b", "usage": usage(1, 1)}},
        ])
        observation = scan_rollout(path, "child")
        self.assertIsNone(observation.model)
        self.assertIsNone(observation.effort)

        mixed = self.write_rollout([
            self.meta(),
            {"type": "turn_context", "payload": {"turn_id": "a", "model": "one", "effort": "high"}},
            {"type": "turn_context", "payload": {"turn_id": "b", "model": "two", "effort": "high"}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "a", "response_id": "a", "usage": usage(1, 1)}},
            {"type": "token_usage_record", "payload": {"thread_id": "child", "turn_id": "b", "response_id": "b", "usage": usage(1, 1)}},
        ])
        with self.assertRaisesRegex(RolloutUsageError, "mixed rollout"):
            scan_rollout(mixed, "child")

    def test_legacy_snapshot_is_whole_thread_only_and_uses_last_monotonic_value(self) -> None:
        path = self.write_rollout([
            self.meta(),
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(3, 1)}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(5, 2)}}},
        ])
        observation = scan_rollout(path, "child")
        self.assertEqual((observation.schema, observation.usage["total_tokens"]), ("event_msg/token_count", 7))  # type: ignore[index]
        with self.assertRaisesRegex(RolloutUsageError, "turn_id requires"):
            scan_rollout(path, "child", "a")

    def test_per_response_records_prevent_legacy_fallback(self) -> None:
        path = self.write_rollout([
            self.meta(),
            {"type": "token_usage_record", "payload": {"thread_id": "other", "turn_id": "a", "response_id": "a", "usage": usage(1, 1)}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(5, 1)}}},
        ])
        with self.assertRaisesRegex(RolloutUsageError, "no token_usage_record"):
            scan_rollout(path, "child")

    def test_rejects_bad_identity_counters_and_decreasing_snapshots(self) -> None:
        identity = self.write_rollout([self.meta(), {"type": "session_meta", "payload": {"id": "other"}}])
        with self.assertRaisesRegex(RolloutUsageError, "exactly match"):
            scan_rollout(identity, "child")

        malformed = self.write_rollout([
            self.meta(),
            {"type": "token_usage_record", "payload": {"thread_id": "child", "response_id": "a", "usage": {"input_tokens": 1}}},
        ])
        with self.assertRaisesRegex(RolloutUsageError, "invalid cached_input_tokens"):
            scan_rollout(malformed, "child")

        decreasing = self.write_rollout([
            self.meta(),
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(3, 2)}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(2, 2)}}},
        ])
        with self.assertRaisesRegex(RolloutUsageError, "decrease"):
            scan_rollout(decreasing, "child")

    def test_no_supported_usage_is_an_unknown_observation(self) -> None:
        observation = scan_rollout(self.write_rollout([self.meta()]), "child")
        self.assertEqual((observation.schema, observation.usage, observation.response_count), ("none", None, 0))


if __name__ == "__main__":
    unittest.main()
