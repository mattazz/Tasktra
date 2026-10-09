"""Lifecycle parent bindings keep otherwise identical snapshots distinct."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.migrations import (
    MigrationError,
    apply_migration_plan,
    migration_plan_digest,
    preview_migration_snapshot,
    rollback_migration,
)
from tasktra.upgrades import _migration_preview, upgrade_plan_digest


LEGACY_EMPTY_DIGEST = "61d2837bd55e353490ab5ba2a81ef761e5243bcaa655dc3bf03d6df3bfc55ab6"
LEGACY_PATH_DIGEST = "f6f88be61d63ff979622c406e71ea5610b856d43344d718eb8873d28e019f946"


def migration_preview(*, parent: str | None = None) -> dict[str, object]:
    preview: dict[str, object] = {
        "ok": True, "changes": [], "blockers": [], "mutation": "none",
        "snapshot_paths": ["data.txt"],
    }
    if parent is not None:
        preview["upgrade_plan_sha256"] = parent
    return preview


def lifecycle_plan(*, runtime_from: int, runtime_to: int) -> dict[str, object]:
    return {
        "action": "upgrade-preview", "mutation": "none", "ok": True, "conflicts": [],
        "current": {"runtime_schema_version": runtime_from, "runtime_state_schema_version": runtime_from},
        "target": {"runtime_schema_version": runtime_to, "packs": []},
        "managed_writes": [], "migration_steps": [], "validation_commands": [],
    }


class UpgradeSnapshotIdentityTests(unittest.TestCase):
    def test_legacy_preview_digests_remain_frozen_and_bound_parent_changes_identity(self) -> None:
        empty = {"ok": True, "changes": [], "blockers": [], "mutation": "none"}
        self.assertEqual(migration_plan_digest(empty), LEGACY_EMPTY_DIGEST)
        self.assertEqual(migration_plan_digest(migration_preview()), LEGACY_PATH_DIGEST)
        parent_only = {**empty, "upgrade_plan_sha256": "d" * 64}
        self.assertNotEqual(migration_plan_digest(parent_only), LEGACY_EMPTY_DIGEST)

        old = lifecycle_plan(runtime_from=10, runtime_to=11)
        new = lifecycle_plan(runtime_from=11, runtime_to=12)
        old_preview = _migration_preview(old, ("data.txt",))
        new_preview = _migration_preview(new, ("data.txt",))

        self.assertEqual(old_preview["snapshot_paths"], new_preview["snapshot_paths"])
        self.assertNotEqual(upgrade_plan_digest(old), upgrade_plan_digest(new))
        self.assertNotEqual(migration_plan_digest(old_preview), migration_plan_digest(new_preview))
        self.assertEqual(
            migration_plan_digest(old_preview),
            migration_plan_digest(_migration_preview(old, ("data.txt",))),
        )

    def test_parent_binding_validation_fails_read_only_and_rejects_unknown_fields(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory)
            data = project / "data.txt"
            data.write_text("before\n", encoding="utf-8")
            before = data.read_bytes()
            invalid_parents: list[object] = [None, 1, "a" * 63, "A" * 64, "g" * 64]
            for parent in invalid_parents:
                with self.subTest(parent=parent):
                    plan = migration_preview()
                    plan["upgrade_plan_sha256"] = parent
                    with self.assertRaisesRegex(MigrationError, "upgrade_plan_sha256"):
                        preview_migration_snapshot(project, plan)
                    self.assertEqual(data.read_bytes(), before)
            unknown = migration_preview(parent="a" * 64)
            unknown["unexpected"] = True
            with self.assertRaisesRegex(MigrationError, "missing or unknown"):
                preview_migration_snapshot(project, unknown)
            self.assertEqual(data.read_bytes(), before)

    def test_bound_snapshots_preserve_consecutive_parent_plans_and_readers(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory)
            data = project / "data.txt"
            data.write_text("first\n", encoding="utf-8")
            old_upgrade = lifecycle_plan(runtime_from=10, runtime_to=11)
            first_plan = _migration_preview(old_upgrade, ("data.txt",))
            first_digest = migration_plan_digest(first_plan)
            first_marker = project / "first-verifier-ran"

            def verify_first() -> dict[str, object]:
                first_marker.write_text("verified\n", encoding="utf-8")
                return {"ok": True, "marker": "first"}

            first = apply_migration_plan(
                project, project, first_plan, expected_plan_sha256=first_digest, confirmed=True,
                verifier=verify_first,
            )
            first_snapshot = first.snapshot_path.read_bytes()
            first_receipt = first.snapshot_path.with_name("receipt.json").read_bytes()
            self.assertTrue(first_marker.exists())
            self.assertEqual(first.verification["marker"], "first")

            data.write_text("second\n", encoding="utf-8")
            new_upgrade = lifecycle_plan(runtime_from=11, runtime_to=12)
            second_plan = _migration_preview(new_upgrade, ("data.txt",))
            second_digest = migration_plan_digest(second_plan)
            second_marker = project / "second-verifier-ran"

            def verify_second() -> dict[str, object]:
                second_marker.write_text("verified\n", encoding="utf-8")
                return {"ok": True, "marker": "second"}

            second = apply_migration_plan(
                project, project, second_plan, expected_plan_sha256=second_digest, confirmed=True,
                verifier=verify_second,
            )

            self.assertNotEqual(first.plan_sha256, second.plan_sha256)
            self.assertNotEqual(first.before_sha256, second.before_sha256)
            self.assertTrue(second_marker.exists())
            self.assertEqual(second.verification["marker"], "second")
            self.assertEqual(first.snapshot_path.read_bytes(), first_snapshot)
            self.assertEqual(first.snapshot_path.with_name("receipt.json").read_bytes(), first_receipt)

            rollback_migration(project, first.plan_sha256, expected_before_sha256=first.before_sha256)
            self.assertEqual(data.read_text(encoding="utf-8"), "first\n")
            rollback_migration(project, second.plan_sha256, expected_before_sha256=second.before_sha256)
            self.assertEqual(data.read_text(encoding="utf-8"), "second\n")

    def test_stale_unbound_digest_fails_before_snapshot_or_verifier(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory)
            data = project / "data.txt"
            data.write_text("before\n", encoding="utf-8")
            upgrade = lifecycle_plan(runtime_from=11, runtime_to=12)
            plan = _migration_preview(upgrade, ("data.txt",))
            verifier_called = False

            def verifier() -> dict[str, object]:
                nonlocal verifier_called
                verifier_called = True
                return {"ok": True}

            with self.assertRaisesRegex(MigrationError, "preview digest changed"):
                apply_migration_plan(
                    project,
                    project,
                    plan,
                    expected_plan_sha256=LEGACY_PATH_DIGEST,
                    confirmed=True,
                    verifier=verifier,
                )

            self.assertFalse(verifier_called)
            self.assertEqual(data.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((project / ".tasktra" / "upgrades").exists())

    def test_same_bound_plan_with_changed_prestate_still_rejects_before_verification(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory)
            data = project / "data.txt"
            data.write_text("first\n", encoding="utf-8")
            plan = migration_preview(parent="c" * 64)
            digest = migration_plan_digest(plan)
            first = apply_migration_plan(
                project, project, plan, expected_plan_sha256=digest, confirmed=True,
            )
            snapshot = first.snapshot_path.read_bytes()
            receipt = first.snapshot_path.with_name("receipt.json").read_bytes()
            data.write_text("second\n", encoding="utf-8")
            verifier_called = False

            def verifier() -> dict[str, object]:
                nonlocal verifier_called
                verifier_called = True
                return {"ok": True}

            with self.assertRaises(MigrationError):
                apply_migration_plan(
                    project, project, plan, expected_plan_sha256=digest, confirmed=True, verifier=verifier,
                )

            self.assertFalse(verifier_called)
            self.assertEqual(first.snapshot_path.read_bytes(), snapshot)
            self.assertEqual(first.snapshot_path.with_name("receipt.json").read_bytes(), receipt)
            self.assertEqual(data.read_text(encoding="utf-8"), "second\n")

    def test_legacy_snapshot_rolls_back_with_its_historical_identity(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory)
            data = project / "data.txt"
            data.write_text("legacy\n", encoding="utf-8")
            plan = migration_preview()
            digest = migration_plan_digest(plan)
            execution = apply_migration_plan(
                project, project, plan, expected_plan_sha256=digest, confirmed=True,
            )
            data.write_text("changed\n", encoding="utf-8")

            result = rollback_migration(project, digest, expected_before_sha256=execution.before_sha256)

            self.assertTrue(result["ok"])
            self.assertEqual(data.read_text(encoding="utf-8"), "legacy\n")


if __name__ == "__main__":
    unittest.main()
