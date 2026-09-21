"""Stage 6 privacy, bounds, and local-only telemetry acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tasktra.telemetry import TelemetryError, TelemetryRecord, TelemetryStore


def record(event_id: str = "event-001") -> TelemetryRecord:
    return TelemetryRecord.create(
        event_id=event_id, recorded_at="2026-09-20T12:00:00Z", role="implementer",
        model_tier="balanced", tools=("rg", "unittest"), elapsed_ms=125, retries=1,
        evidence_reused=True, validation_outcome="passed", human_interventions=0,
        final_outcome="succeeded", input_tokens=42, output_tokens=17,
    )


class TelemetryTests(unittest.TestCase):
    def test_collection_is_disabled_and_local_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TelemetryStore(temporary)
            self.assertFalse(store.append(record()))
            self.assertEqual(store.records(), ())
            self.assertFalse((Path(temporary) / ".tasktra" / "telemetry").exists())

    def test_enabled_store_is_bounded_and_keeps_newest_records_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TelemetryStore(temporary, enabled=True, max_records=2, max_file_bytes=8_192)
            for identifier in ("event-001", "event-002", "event-003"):
                self.assertTrue(store.append(record(identifier)))
            self.assertEqual([item.event_id for item in store.records()], ["event-002", "event-003"])
            self.assertLessEqual(store.path.stat().st_size, store.max_file_bytes)

    def test_sanitized_export_is_explicit_canonical_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TelemetryStore(temporary, enabled=True)
            store.append(record())
            destination = store.export_sanitized(".tasktra/exports/telemetry.json")
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["records"][0]["event_id"], "event-001")
            with self.assertRaisesRegex(TelemetryError, "escapes"):
                store.export_sanitized(Path(temporary).parent / "outside.json")

    def test_closed_record_shape_rejects_prompts_code_and_credential_shapes(self):
        value = record().as_dict()
        for key, content in (("prompt", "do the thing"), ("source", "def secret(): pass"), ("api_key", "sk-not-safe")):
            with self.subTest(key=key):
                unsafe = dict(value)
                unsafe[key] = content
                with self.assertRaisesRegex(TelemetryError, "does not permit"):
                    TelemetryRecord.from_mapping(unsafe)
        with self.assertRaisesRegex(TelemetryError, "non-credential"):
            TelemetryRecord.create(
                event_id="event-002", recorded_at="2026-09-20T12:00:00Z", role="implementer",
                model_tier="sk-abcdefghijklmnop", final_outcome="succeeded",
            )
        with self.assertRaisesRegex(TelemetryError, "registered non-credential"):
            TelemetryRecord.create(
                event_id="event-002", recorded_at="2026-09-20T12:00:00Z", role="implementer",
                model_tier="balanced", tools=("unregistered-tool-fixture",),
                final_outcome="succeeded",
            )

    def test_oversized_or_linked_storage_is_rejected_before_io(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as elsewhere:
            store = TelemetryStore(temporary, enabled=True, max_file_bytes=8_192)
            with self.assertRaisesRegex(TelemetryError, "schema version"):
                TelemetryRecord.from_mapping({**record().as_dict(), "schema_version": 2})
            link = Path(temporary) / ".tasktra"
            try:
                link.symlink_to(elsewhere, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable on this platform")
            with self.assertRaisesRegex(TelemetryError, "symbolic link or reparse point"):
                store.append(record())


if __name__ == "__main__":
    unittest.main()
