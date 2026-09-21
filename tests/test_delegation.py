import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from tasktra.compiler import CatalogError, compile_catalog, load_catalog
from tasktra.config import ConfigError, ProjectConfig, load_project_config
from tasktra.delegation import DelegationError, delegation_plan, projection_overrides


ROOT = Path(__file__).resolve().parents[1]


def request():
    return {
        "kind": "tasktra.routing-request", "version": 1, "task_id": "delegation-test",
        "source": {"goal_id": "goal", "work_unit_id": "unit"}, "objective": "Inspect.",
        "primary_signal": "inspect", "signals": ["inspect"], "constraints": [],
        "verified_facts": [], "evidence_refs": [],
    }


class DelegationTests(unittest.TestCase):
    def test_config_overrides_are_immutable_and_reject_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tasktra").mkdir()
            (root / ".tasktra/project.toml").write_text(
                """[project]\nname = "test"\nconfig_version = 1\n\n[agents.codex.model_tiers]\nfast = "project-fast"\n\n[agents.codex.roles.scout]\nmodel = "inherit"\nreasoning_effort = "inherit"\n""",
                encoding="utf-8",
            )
            config = load_project_config(root)
            with self.assertRaises(TypeError):
                config.codex_tier_models["fast"] = "mutated"
            self.assertEqual(config.codex_role_overrides["scout"]["model"], "inherit")
            (root / ".tasktra/project.toml").write_text(
                """[project]\nname = "test"\nconfig_version = 1\n\n[agents.codex.roles.scout]\nsandbox_mode = "danger-full-access"\n""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_project_config(root)

    def test_precedence_inherit_and_unknown_role_fail_closed(self):
        catalog = load_catalog(ROOT / "catalog")
        config = ProjectConfig(
            name="test", codex_tier_models={"fast": "project-fast"},
            codex_role_overrides={"scout": {"model": "inherit", "reasoning_effort": "inherit"}},
        )
        policy, overrides = projection_overrides(catalog, config)
        agent = compile_catalog(catalog, codex_model_policy=policy, codex_role_overrides=overrides).files[
            PurePosixPath(".codex/agents/scout.toml")
        ]
        self.assertNotIn("model =", agent)
        self.assertNotIn("model_reasoning_effort", agent)
        self.assertIn('sandbox_mode = "read-only"', agent)
        bad = ProjectConfig(name="test", codex_role_overrides={"missing-role": {"model": "x"}})
        with self.assertRaisesRegex(DelegationError, "unknown role"):
            projection_overrides(catalog, bad)
        with self.assertRaisesRegex(DelegationError, "unknown role"):
            delegation_plan(catalog, bad, request())

    def test_delegation_plan_is_host_required_and_read_only(self):
        catalog = load_catalog(ROOT / "catalog")
        plan = delegation_plan(catalog, ProjectConfig(name="test"), request())
        self.assertEqual(plan["mutation"], "none")
        self.assertEqual(plan["availability"], "host-unverified")
        self.assertEqual(plan["dispatch"], "codex-host-required")
        self.assertEqual(plan["agent"]["agent_type"], "scout")
