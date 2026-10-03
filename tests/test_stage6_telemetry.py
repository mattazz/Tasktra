"""Stage 6 privacy, bounds, and local-only telemetry acceptance tests."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest

from tasktra.telemetry import TelemetryError, TelemetryRecord, TelemetryStore


def record(event_id: str = "event-001") -> TelemetryRecord:
    return TelemetryRecord.create(
        event_id=event_id, recorded_at="2026-09-20T12:00:00Z", role="implementer",
        model_tier="balanced", tools=("rg", "unittest"), elapsed_ms=125, retries=1,
        evidence_reused=True, validation_outcome="passed", human_interventions=0,
        final_outcome="succeeded", input_tokens=42, output_tokens=17,
    )


def append_in_process(root, start, result, identifiers, max_records, max_file_bytes):
    try:
        store = TelemetryStore(root, enabled=True, max_records=max_records, max_file_bytes=max_file_bytes)
        read = store._read_records

        def slow_read():
            records = read()
            # Widen the previous read/replace race without requiring concurrent
            # readers inside the region that is now protected by the lock.
            time.sleep(0.02)
            return records

        store._read_records = slow_read
        start.wait(15)
        result.send([store.append(record(identifier)) for identifier in identifiers])
    finally:
        result.close()


class TelemetryTests(unittest.TestCase):
    def concurrent_appends(self, root, identifiers, *, max_records=128, max_file_bytes=8192):
        context = multiprocessing.get_context("spawn")
        start = context.Barrier(len(identifiers))
        processes = []
        receivers = []
        try:
            for batch in identifiers:
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(
                    target=append_in_process,
                    args=(root, start, sender, batch, max_records, max_file_bytes),
                )
                process.start()
                sender.close()
                processes.append(process)
                receivers.append(receiver)
            results = []
            for receiver in receivers:
                self.assertTrue(receiver.poll(20), "telemetry writer did not finish")
                results.extend(receiver.recv())
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            return results
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(10)
                process.close()
            for receiver in receivers:
                receiver.close()

    def test_concurrent_processes_preserve_distinct_events_and_initial_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            identifiers = [[f"event-{index:03d}"] for index in range(4)]
            self.assertEqual(self.concurrent_appends(temporary, identifiers), [True] * 4)
            records = TelemetryStore(temporary).records()
            self.assertEqual({item.event_id for item in records}, {batch[0] for batch in identifiers})

    def test_concurrent_retention_respects_record_and_byte_caps(self):
        for max_records, max_bytes in ((3, 8192), (128, 4097)):
            with self.subTest(max_records=max_records), tempfile.TemporaryDirectory() as temporary:
                batches = [[f"event-{worker * 10 + index:03d}" for index in range(8)] for worker in range(3)]
                self.assertTrue(all(self.concurrent_appends(
                    temporary, batches, max_records=max_records, max_file_bytes=max_bytes,
                )))
                store = TelemetryStore(temporary, max_records=max_records, max_file_bytes=max_bytes)
                records = store.records()
                self.assertGreater(len(records), 0)
                self.assertLessEqual(len(records), max_records)
                self.assertLessEqual(store.path.stat().st_size, max_bytes)
                self.assertEqual(len(records), len({item.event_id for item in records}))

    def test_concurrent_duplicate_event_is_idempotent_and_conflicting_values_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            results = self.concurrent_appends(temporary, [["event-001"], ["event-001"]])
            self.assertEqual(sorted(results), [False, True])
            store = TelemetryStore(temporary, enabled=True)
            self.assertEqual(len(store.records()), 1)
            with self.assertRaisesRegex(TelemetryError, "different measurements"):
                store.append({**record().as_dict(), "elapsed_ms": 99})
            self.assertEqual(store.records(), (record(),))

    def test_legacy_records_remain_readable_and_lock_is_not_an_export_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TelemetryStore(temporary, enabled=True)
            store.directory.mkdir(parents=True)
            legacy = record().as_dict()
            del legacy["schema_version"]
            store.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            self.assertFalse(store.append(record()))
            self.assertEqual(store.records(), (record(),))
            for target in (store.lock_path, store.directory / ".." / "telemetry" / store.lock_path.name):
                with self.subTest(target=target), self.assertRaisesRegex(TelemetryError, "differ"):
                    store.export_sanitized(target)

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
