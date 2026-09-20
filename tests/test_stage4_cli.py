"""Black-box coverage for local-only protocol-v2 provider-effect controls."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore
from tasktra.cli import _capabilities, build_parser, main
from tasktra.config import load_project_config
from tasktra.providers import FakeProvider, ProviderHealth, ProviderRegistry
from tests.approval_helpers import v3_approval_kwargs


def run_cli(*arguments: str):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


SCOPE = {
    "provider": "github", "host": "github.com", "container": "acme",
    "resource_kind": "issue", "resource": "17", "ref": None,
}
DESCRIPTOR = {
    "provider": "github", "capability": "issue-comment", "action": "remote-comment",
    "effect_class": "external-communication", "resource_scope": SCOPE,
    "protocol_version": 2,
}


def envelope():
    return {
        "kind": "tasktra.authority-envelope", "version": 2, "goal_id": "goal-one",
        "outcome": "Record a provider handoff", "motivation": "CLI lifecycle test", "author_id": "owner",
        "acceptance_criteria": [{"id": "done", "statement": "Done"}],
        "scope": {"paths": ["."], "exclusions": []}, "resource_scopes": [SCOPE],
        "allowed_actions": ["goal-activate", "work-claim", "remote-comment"],
        "allowed_effects": ["local-reversible-write", "external-communication"],
        "prohibited_actions": [], "quality_requirements": [],
        "budgets": {"tokens": 20, "attempts": 2, "elapsed_seconds": 600, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": [], "escalation_conditions": [],
    }


class ProviderEffectCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.assertEqual(run_cli("init", "--root", str(self.root), "--apply")[0], 0)
        self.now = datetime.now(timezone.utc)
        config = load_project_config(self.root)
        self.store = AutonomyStore(config.database_path(self.root))
        self.store.create_goal(goal_id="goal-one", title="Goal", description="Goal", acceptance=["Done"])
        self.contract = envelope()
        self.digest = authority_envelope_sha256(self.contract)
        self.store.define_goal_contract("goal-one", self.contract, actor_id="owner", at=self.now)
        expires = self.now + timedelta(minutes=10)
        self.store.record_transition_approval(goal_id="goal-one", action="goal-activate", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="owner", valid_until=expires, at=self.now)
        self.store.activate_goal("goal-one", actor_id="owner", envelope_sha256=self.digest, at=self.now)
        self.store.create_work_unit(goal_id="goal-one", work_unit_id="unit-one", title="Unit", scope={"paths": ["src"], "exclusions": []})
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="work-claim", effect="local-reversible-write", envelope_sha256=self.digest, approver_id="human", performer_id="worker", valid_until=expires, at=self.now)
        provider_kwargs = v3_approval_kwargs(
            approval_id="provider-cli-v3", goal_id="goal-one", work_unit_id="unit-one",
            action="remote-comment", effect="external-communication",
            scope={"paths": ["."], "exclusions": []}, resource_scope=SCOPE,
            envelope_sha256=self.digest, approver_id="human", performer_id="worker",
            valid_until=expires, attested_at=self.now,
        )
        self.store.record_transition_approval(goal_id="goal-one", work_unit_id="unit-one", action="remote-comment", effect="external-communication", envelope_sha256=self.digest, approver_id="human", performer_id="worker", resource_scope=SCOPE, valid_until=expires, at=self.now, **provider_kwargs)
        self.claim = self.store.claim_next_work(goal_id="goal-one", performer_id="worker", envelope_sha256=self.digest, lease_seconds=600, repository="repo", revision="abc", branch="main", workspace="work", at=self.now)
        self.descriptor_path = self._json("descriptor.json", DESCRIPTOR)
        self.request_path = self._json("request.json", {"body": "local lifecycle only"})
        self.observation_path = self._json("observation.json", {"checked": True})
        self.environment = {"TASKTRA_LEASE_TOKEN": self.claim["lease_token"]}

    def tearDown(self):
        self.directory.cleanup()

    def _json(self, name: str, value: dict) -> str:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def _prepare_args(self, key: str) -> tuple[str, ...]:
        return (
            "effect", "--root", str(self.root), "provider-prepare", key,
            "--goal-id", "goal-one", "--work-unit-id", "unit-one",
            "--descriptor", self.descriptor_path, "--request", self.request_path,
            "--work-attempt-id", self.claim["attempt_id"], "--envelope-sha256", self.digest,
            "--actor", "worker",
        )

    def test_cli_can_prepare_but_cannot_bypass_the_authority_bound_executor(self):
        with patch.dict(os.environ, self.environment, clear=False):
            code, prepared = run_cli(*self._prepare_args("comment-one"))
            self.assertEqual((code, prepared["effect"]["status"]), (0, "pending"))
            self.assertNotIn(self.claim["lease_token"], json.dumps(prepared))
            code, inspected = run_cli("effect", "--root", str(self.root), "inspect", "comment-one")
            self.assertEqual((code, inspected["effect"]["status"]), (0, "pending"))
        for unsafe in ("provider-begin", "provider-receipt", "provider-retry"):
            with self.subTest(command=unsafe), self.assertRaises(SystemExit):
                build_parser().parse_args(("effect", unsafe))

    def test_cli_cannot_assert_absence_to_unlock_a_retry(self):
        with patch.dict(os.environ, self.environment, clear=False):
            self.assertEqual(run_cli(*self._prepare_args("retry-one"))[0], 0)
            with self.assertRaises(SystemExit) as denied:
                main(("effect", "--root", str(self.root), "provider-reconcile", "retry-one", "--resolution", "absent", "--observation", self.observation_path, "--actor", "worker"))
        self.assertEqual(denied.exception.code, 2)
        self.assertEqual(self.store.inspect_effect("retry-one")["status"], "pending")

    def test_cli_rejects_legacy_human_approval_without_v3_ceremony(self):
        approval = {
            "kind": "tasktra.transition-approval", "version": 2,
            "approval_id": "transition-cli-scope", "goal_id": "goal-one", "work_unit_id": "unit-one",
            "action": "remote-comment", "effect": "external-communication",
            "scope": {"paths": ["src"], "exclusions": []}, "resource_scope": SCOPE,
            "envelope_sha256": self.digest, "decision": "approved",
            "approver": {"kind": "human", "id": "reviewer"}, "performer_id": "worker",
            "authority_clause": "Human approved the exact provider scope", "evidence": [],
            "valid_until": (self.now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), "revoked_at": None,
        }
        path = self._json("approval.json", approval)
        code, recorded = run_cli("approval", "--root", str(self.root), "record", path)
        self.assertEqual(code, 2)
        self.assertIn("v3 local-human-ceremony", recorded["error"])

    def test_capabilities_uses_injected_provider_health_without_a_probe(self):
        registry = ProviderRegistry()
        registry.register_provider("github", discovery=FakeProvider(ProviderHealth("github", "degraded", "GitHub rate limit is reduced")), operations={})
        registry.register_provider("jira", discovery=FakeProvider(ProviderHealth("jira", "available", "Jira connector is configured")), operations={})
        report = _capabilities(type("Args", (), {"root": str(self.root)})(), provider_registry=registry)
        by_id = {item["id"]: item for item in report["capabilities"]}
        self.assertEqual(by_id["github-cli-adapter"]["state"], "degraded")
        self.assertTrue(by_id["github-cli-adapter"]["available"])
        self.assertEqual(by_id["jira-connector-adapter"]["state"], "available")
        self.assertTrue(report["local_work_can_continue"])
        self.assertEqual(report["provider_health_source"], "embedded-host")
        self.assertFalse(report["provider_health_is_authority"])

    def test_capabilities_accepts_one_invocation_bounded_host_health_snapshot(self):
        snapshot = {
            "kind": "tasktra.provider-health-report", "version": 1,
            "providers": [
                {"provider": "github", "state": "degraded", "summary": "Authentication needs refresh",
                 "operations": [{"effect_class": "read-only", "request_kind": "github-issue-get"}]},
                {"provider": "jira", "state": "available", "summary": "Connected by the Codex host",
                 "operations": [{"effect_class": "external-communication", "request_kind": "jira-comment"}]},
            ],
        }
        path = self._json("provider-health.json", snapshot)
        code, report = run_cli("capabilities", "--root", str(self.root), "--provider-health", path)
        self.assertEqual(code, 0)
        self.assertEqual(report["provider_health_source"], "host-reported-snapshot")
        self.assertFalse(report["provider_health_is_authority"])
        by_id = {item["id"]: item for item in report["capabilities"]}
        self.assertEqual(by_id["github-cli-adapter"]["state"], "degraded")
        self.assertTrue(by_id["jira-connector-adapter"]["available"])

        snapshot["providers"].append(dict(snapshot["providers"][0]))
        duplicate = self._json("provider-health-duplicate.json", snapshot)
        code, rejected = run_cli("capabilities", "--root", str(self.root), "--provider-health", duplicate)
        self.assertEqual(code, 2)
        self.assertIn("duplicate provider", rejected["error"])

        oversized = self.root / "provider-health-oversized.json"
        oversized.write_bytes(b" " * (64 * 1024 + 1))
        code, rejected = run_cli("capabilities", "--root", str(self.root), "--provider-health", str(oversized))
        self.assertEqual(code, 2)
        self.assertIn("exceeds", rejected["error"])

    def test_prepare_requires_a_live_lease(self):
        with patch.dict(os.environ, {}, clear=True):
            code, missing_lease = run_cli(*self._prepare_args("missing-lease"))
        self.assertEqual(code, 2)
        self.assertIn("lease token environment variable is unset", missing_lease["error"])
