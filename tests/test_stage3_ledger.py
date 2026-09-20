from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tasktra.autonomy import AutonomyStore
from tasktra.authority import authority_envelope_sha256
from tasktra.state import StateError, StateStore


def envelope(goal_id="goal-one"):
    return {"kind": "tasktra.authority-envelope", "version": 1, "goal_id": goal_id,
            "outcome": "Ship safely", "motivation": "A bounded test ledger", "author_id": "steward",
            "acceptance_criteria": [{"id": "tests-pass", "statement": "pass"}],
            "scope": {"paths": ["src"], "exclusions": []}, "allowed_actions": ["goal-activate", "goal-resume"],
            "allowed_effects": ["read-only", "local-reversible-write"], "prohibited_actions": [], "quality_requirements": [],
            "budgets": {"tokens": 10, "attempts": 2, "elapsed_seconds": 30, "concurrency": 1},
            "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": []}


class Stage3LedgerTests(unittest.TestCase):
    def test_root_scope_contains_project_paths_but_respects_exclusions(self):
        self.assertTrue(StateStore._scope_within_contract({"paths": ["src/tasktra"], "exclusions": []}, {"paths": ["."], "exclusions": []}))
        self.assertFalse(StateStore._scope_within_contract({"paths": ["src/private"], "exclusions": []}, {"paths": ["."], "exclusions": ["src/private"]}))
        self.assertFalse(StateStore._scope_within_contract({"paths": ["."], "exclusions": []}, {"paths": ["."], "exclusions": ["private"]}))
        self.assertTrue(StateStore._scope_within_contract({"paths": ["."], "exclusions": ["private"]}, {"paths": ["."], "exclusions": ["private"]}))

    def test_human_exact_bound_activation_and_expiry(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            contract = store.define_goal_contract("goal-one", envelope(), actor_id="steward", at="2026-01-01T00:00:00Z")
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                envelope_sha256=contract["envelope_sha256"], approver_id="human", performer_id="runner",
                valid_until="2026-01-01T00:01:00Z", at="2026-01-01T00:00:00Z")
            store.activate_goal("goal-one", actor_id="runner", at="2026-01-01T00:00:30Z")
            self.assertEqual(store.get_goal("goal-one")["status"], "active")
            with self.assertRaisesRegex(StateError, "expired|no current"):
                store.check_authorization(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                    envelope_sha256=contract["envelope_sha256"], performer_id="runner", at="2026-01-01T00:01:00Z")

    def test_audit_detects_tamper_and_immutable_trigger(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"; store = StateStore(path)
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one")
            self.assertTrue(store.verify_audit()["ok"])
            connection = sqlite3.connect(path)
            try:
                audit = connection.execute("SELECT goal_id,work_unit_id FROM audit_events").fetchone()
                self.assertEqual((audit[0], audit[1]), ("goal-one", None))
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM audit_events")
            finally:
                connection.close()

    def test_contract_prohibitions_and_acceptance_cannot_be_bypassed(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            bad = envelope(); bad["acceptance_criteria"][0]["statement"] = "different"
            with self.assertRaisesRegex(StateError, "acceptance"):
                store.define_goal_contract("goal-one", bad, actor_id="steward")
            contract = envelope(); contract["allowed_actions"] = ["goal-resume"]; contract["prohibited_actions"] = ["goal-activate"]
            current = store.define_goal_contract("goal-one", contract, actor_id="steward")
            with self.assertRaisesRegex(StateError, "not allowed"):
                store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                    envelope_sha256=current["envelope_sha256"], approver_id="human", performer_id="runner",
                    valid_until="2026-01-01T00:01:00Z", at="2026-01-01T00:00:00Z")
            with self.assertRaisesRegex(StateError, "effect is not allowed"):
                store.record_transition_approval(goal_id="goal-one", action="goal-resume", effect="deployment",
                    envelope_sha256=current["envelope_sha256"], approver_id="human", performer_id="runner",
                    valid_until="2026-01-01T00:01:00Z", at="2026-01-01T00:00:00Z")

    def test_authority_envelope_cannot_be_replaced_after_activation(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            current = store.define_goal_contract("goal-one", envelope(), actor_id="steward")
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                envelope_sha256=current["envelope_sha256"], approver_id="human", performer_id="runner",
                valid_until="2027-01-01T00:00:00Z")
            store.activate_goal("goal-one", actor_id="runner")
            with self.assertRaisesRegex(StateError, "only be defined or redefined while.*planned"):
                store.define_goal_contract("goal-one", envelope(), actor_id="steward")

    def test_audit_detects_direct_tampering_of_authority_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            contract = store.define_goal_contract("goal-one", envelope(), actor_id="steward")
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                envelope_sha256=contract["envelope_sha256"], approver_id="human", performer_id="runner",
                valid_until="2027-01-01T00:00:00Z")
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE goal_contracts SET contract=? WHERE goal_id=?", ('{"tampered":true}', "goal-one"))
                connection.execute("UPDATE transition_approvals SET performer_id=? WHERE id=(SELECT id FROM transition_approvals LIMIT 1)", ("attacker",))
                connection.commit()
            finally:
                connection.close()
            report = store.verify_audit()
            self.assertFalse(report["ok"])
            self.assertTrue(any("authority row tampered in goal_contracts" in issue for issue in report["issues"]))
            self.assertTrue(any("authority row tampered in transition_approvals" in issue for issue in report["issues"]))

    def test_resume_is_active_and_emergency_controls_are_idempotent(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            contract = store.define_goal_contract("goal-one", envelope(), actor_id="steward")
            for action in ("goal-activate", "goal-resume"):
                store.record_transition_approval(goal_id="goal-one", action=action, effect="local-reversible-write",
                    envelope_sha256=contract["envelope_sha256"], approver_id="human", performer_id="runner",
                    valid_until="2027-01-01T00:00:00Z")
            store.activate_goal("goal-one", actor_id="runner")
            store.pause_goal("goal-one", actor_id="safety")
            store.resume_goal("goal-one", actor_id="runner")
            self.assertEqual(store.get_goal("goal-one")["status"], "active")
            self.assertTrue(store.set_emergency_stop(actor_id="safety", reason="halt"))
            self.assertFalse(store.set_emergency_stop(actor_id="safety", reason="repeat"))
            with self.assertRaisesRegex(StateError, "human actor"):
                store.clear_emergency_stop(actor_id="safety", approver_kind="steward")
            self.assertTrue(store.clear_emergency_stop(actor_id="safety", approver_kind="human"))
            self.assertFalse(store.clear_emergency_stop(actor_id="safety", approver_kind="human"))

    def test_v2_migration_imports_legacy_events_into_chain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT, work_unit_id TEXT, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
                connection.execute("INSERT INTO events(goal_id,work_unit_id,event_type,payload,created_at) VALUES(NULL,NULL,'legacy.event','{}','2026-01-01T00:00:00Z')")
                connection.execute("PRAGMA user_version=2"); connection.commit()
            finally: connection.close()
            StateStore(path).migrate()
            report = StateStore(path).verify_audit()
            self.assertFalse(report["ok"])
            self.assertFalse(any("audit hash mismatch" in issue for issue in report["issues"]))
            self.assertTrue(any("unsealed" in issue for issue in report["issues"]))

    def test_old_schema_reads_require_explicit_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one")
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(StateError, "requires migration"):
                store.get_goal("goal-one")
            store.migrate()
            self.assertEqual(store.get_goal("goal-one")["id"], "goal-one")

    def test_contracted_work_unit_scope_and_terminal_goal_are_enforced(self):
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            store.define_goal_contract("goal-one", envelope(), actor_id="steward")
            with self.assertRaisesRegex(StateError, "explicit closed scope"):
                store.create_work_unit(goal_id="goal-one", title="Implement")
            with self.assertRaisesRegex(StateError, "outside the authority"):
                store.create_work_unit(goal_id="goal-one", title="Implement", scope={"paths": ["private"], "exclusions": []})
            store.stop_goal("goal-one", actor_id="safety")
            with self.assertRaisesRegex(StateError, "planned or active"):
                store.create_work_unit(goal_id="goal-one", title="Implement", scope={"paths": ["src"], "exclusions": []})

    def test_pause_closes_interrupted_attempt_with_outcome(self):
        with TemporaryDirectory() as directory:
            store = AutonomyStore(Path(directory) / "state.sqlite")
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            contract_value = envelope()
            contract_value["allowed_actions"].append("work-claim")
            digest = authority_envelope_sha256(contract_value)
            store.define_goal_contract("goal-one", contract_value, actor_id="steward", at="2026-01-01T00:00:00Z")
            store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write",
                envelope_sha256=digest, approver_id="human", performer_id="runner",
                valid_until="2027-01-01T00:00:00Z", at="2026-01-01T00:00:00Z")
            store.activate_goal("goal-one", actor_id="runner", at="2026-01-01T00:00:00Z")
            store.create_work_unit(goal_id="goal-one", work_unit_id="work-one", title="Implement",
                                   scope={"paths": ["src"], "exclusions": []})
            store.record_transition_approval(goal_id="goal-one", work_unit_id="work-one", action="work-claim",
                effect="local-reversible-write", envelope_sha256=digest, approver_id="steward",
                approver_kind="steward", performer_id="runner", valid_until="2027-01-01T00:00:00Z",
                at="2026-01-01T00:00:00Z")
            claim = store.claim_next_work(goal_id="goal-one", performer_id="runner", envelope_sha256=digest,
                token_reservation=3, lease_seconds=3600, repository="repo", revision="abc",
                branch="main", workspace="work", at="2026-01-01T00:00:00Z")
            self.assertIsNotNone(claim)
            store.pause_goal("goal-one", actor_id="safety", at="2026-01-01T00:00:10Z")
            connection = sqlite3.connect(store.path)
            try:
                attempt = connection.execute("SELECT status,ended_at,outcome_class,outcome_json,tokens_reserved FROM work_attempts WHERE id=?", (claim["attempt_id"],)).fetchone()
            finally:
                connection.close()
            self.assertEqual(attempt[0], "paused")
            self.assertEqual(attempt[1], "2026-01-01T00:00:10Z")
            self.assertEqual(attempt[2], "paused")
            self.assertIn("safety", attempt[3])
            self.assertEqual(attempt[4], 0)

    def test_migrated_v3_authority_rows_require_human_attestation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = StateStore(path)
            store.create_goal(title="Ship", description="Ledger", goal_id="goal-one", acceptance=["pass"])
            store.define_goal_contract("goal-one", envelope(), actor_id="steward")
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE authority_seals")
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            finally:
                connection.close()
            store.migrate()
            self.assertFalse(store.verify_audit()["ok"])
            attested = store.attest_ledger(actor_id="human")
            self.assertGreater(attested["sealed_row_count"], 0)
            self.assertTrue(store.verify_audit()["ok"])
