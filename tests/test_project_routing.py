"""Project-owned coordinator routes and specialist discovery."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.compiler import Catalog, CatalogError, compile_catalog, load_catalog
from tasktra.config import ConfigError, ProjectRoute, initialize_project, load_project_config


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def project(root: Path, routes: str) -> Path:
    profile = initialize_project(root, name="Motif fixture")
    profile.write_text(profile.read_text(encoding="utf-8") + routes, encoding="utf-8")
    agent = root / ".codex" / "agents" / "graphic-designer.toml"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        'name = "graphic-designer"\n'
        'description = "Create and integrate characters, poses, backgrounds, and interface artwork."\n'
        'model = "gpt-6-sol"\n'
        'model_reasoning_effort = "high"\n'
        'developer_instructions = "Use the art direction and integration guide."\n',
        encoding="utf-8",
    )
    return agent


ROUTES = """
[[routing.routes]]
id = "art-concept"
trigger = "Create a character concept, including a draft for review"
role = "graphic-designer"
skills = ["imagegen"]
boundary = "Return a draft for review; do not register or publish it."

[[routing.routes]]
id = "art-critique"
trigger = "Review existing character artwork"
role = "graphic-designer"
boundary = "Return rubric findings without generation or edits."

[[routing.routes]]
id = "art-integration"
trigger = "Implement approved artwork in the product"
role = "graphic-designer"
boundary = "Export, register, assign, and verify the rendered surfaces."

[[routing.routes]]
id = "diagnose-study"
trigger = "Diagnose broken study behavior"
roles = ["tester", "implementer"]
skills = ["diagnosing-bugs"]

[[routing.routes]]
id = "fixed-base-review"
trigger = "Review code changes since a fixed revision"
role = "reviewer"
skills = ["code-review"]
boundary = "Return independent findings; make no edits."

[[routing.routes]]
id = "documentation"
trigger = "Write substantive technical documentation"
role = "writer"
skills = ["engineering:documentation"]

[[routing.routes]]
id = "explicit-eli5"
trigger = "User explicitly invokes eli5"
skills = ["eli5"]
"""


class ProjectRoutingTests(unittest.TestCase):
    def test_motif_routes_compile_and_preserve_custom_agent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = project(root, ROUTES)
            original = agent.read_bytes()
            args = ("compile", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog")
            adoption_code, adoption = invoke("adopt", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog")
            self.assertEqual(adoption_code, 0)
            planned = {item["path"]: item for item in adoption["managed_writes"]}
            self.assertIn("AGENTS.md", planned)
            code, report = invoke(*args)
            self.assertEqual((code, report["ok"]), (0, True))
            instructions = (root / "AGENTS.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
            for content in (instructions, claude):
                self.assertIn("Create a character concept, including a draft for review → specialist `graphic-designer`; skill `imagegen`", content)
                self.assertIn("Return a draft for review; do not register or publish it.", content)
                self.assertIn("Review existing character artwork → specialist `graphic-designer`", content)
                self.assertIn("Return rubric findings without generation or edits.", content)
                self.assertIn("Export, register, assign, and verify the rendered surfaces.", content)
                self.assertIn("User explicitly invokes eli5", content)
                self.assertIn("Diagnose broken study behavior → specialists `tester`, `implementer` as needed; skill `diagnosing-bugs`", content)
                self.assertIn("explicit-only triggers", content)
                self.assertIn("no-subagent choices", content)
                self.assertIn("deterministic tools for mechanical work", content)
                self.assertIn("runtime delegation limits", content)
            self.assertIn("`.codex/agents/graphic-designer.toml`", instructions)
            self.assertEqual(planned["AGENTS.md"]["sha256"], sha256(instructions.encode("utf-8")).hexdigest())
            self.assertEqual(agent.read_bytes(), original)
            manifest = json.loads((root / ".tasktra/generated/manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn(".codex/agents/graphic-designer.toml", [item["path"] for item in manifest["files"]])
            self.assertIn(".codex/agents/graphic-designer.toml", report["project_owned"])
            self.assertEqual(invoke(*args, "--check")[0], 0)
            upgrade_code, upgrade = invoke(
                "upgrade", "--root", str(root), "preview", "--catalog", str(CATALOG), "--trust-catalog"
            )
            self.assertEqual(upgrade_code, 0)
            upgrade_writes = {item["path"]: item for item in upgrade["managed_writes"]}
            self.assertEqual(upgrade_writes["AGENTS.md"]["sha256"], sha256(instructions.encode("utf-8")).hexdigest())

            profile = root / ".tasktra/project.toml"
            profile.write_text(profile.read_text(encoding="utf-8").replace(
                "Return a draft for review; do not register or publish it.",
                "Return one draft for review; do not register or publish it.",
            ), encoding="utf-8")
            code, drift = invoke(*args, "--check")
            self.assertEqual(code, 1)
            self.assertIn("AGENTS.md", drift["changed"])
            self.assertIn("CLAUDE.md", drift["changed"])
            self.assertEqual(invoke(*args)[0], 0)
            self.assertEqual(invoke(*args, "--check")[0], 0)
            self.assertEqual(agent.read_bytes(), original)

    def test_no_routes_keeps_legacy_entrypoint(self):
        catalog = load_catalog(CATALOG)
        without = compile_catalog(catalog)
        with_empty = compile_catalog(catalog, project_routes=())
        self.assertEqual(without, with_empty)
        self.assertNotIn("Project routes", without.files[next(path for path in without.files if str(path) == "AGENTS.md")])

    def test_custom_agent_is_default_without_a_route_and_description_drift_is_visible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = project(root, "")
            original = agent.read_bytes()
            args = ("compile", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog")
            adoption_code, adoption = invoke("adopt", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog")
            self.assertEqual(adoption_code, 0)
            adoption_writes = {item["path"]: item for item in adoption["managed_writes"]}
            code, result = invoke(*args)
            self.assertEqual((code, result["ok"]), (0, True))
            instructions = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Use the matching specialist", instructions)
            self.assertIn("when the host permits delegation", instructions)
            self.assertIn("`graphic-designer` (`.codex/agents/graphic-designer.toml`): Create and integrate characters", instructions)
            self.assertEqual(adoption_writes["AGENTS.md"]["sha256"], sha256(instructions.encode("utf-8")).hexdigest())
            self.assertEqual(agent.read_bytes(), original)
            manifest = json.loads((root / ".tasktra/generated/manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn(".codex/agents/graphic-designer.toml", [item["path"] for item in manifest["files"]])
            self.assertEqual(invoke(*args, "--check")[0], 0)
            upgrade_code, upgrade = invoke(
                "upgrade", "--root", str(root), "preview", "--catalog", str(CATALOG), "--trust-catalog"
            )
            self.assertEqual(upgrade_code, 0)
            upgrade_writes = {item["path"]: item for item in upgrade["managed_writes"]}
            self.assertEqual(upgrade_writes["AGENTS.md"]["sha256"], sha256(instructions.encode("utf-8")).hexdigest())

            agent.write_bytes(original.replace(b"interface artwork", b"product artwork"))
            code, drift = invoke(*args, "--check")
            self.assertEqual(code, 1)
            self.assertIn("AGENTS.md", drift["changed"])
            self.assertIn("CLAUDE.md", drift["changed"])
            self.assertEqual(invoke(*args)[0], 0)
            self.assertEqual(invoke(*args, "--check")[0], 0)

    def test_disabled_catalog_role_file_is_not_auto_opted_in(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "")
            stale = root / ".codex/agents/frontend-specialist.toml"
            stale.write_text('name = "frontend-specialist"\ndescription = "Build interfaces."\n', encoding="utf-8")
            projection = compile_catalog(load_catalog(CATALOG), project_root=root)
            instructions = projection.files[next(path for path in projection.files if str(path) == "AGENTS.md")]
            self.assertNotIn("`frontend-specialist` (`.codex/agents/frontend-specialist.toml`)", instructions)

    def test_catalog_agent_files_do_not_consume_custom_agent_limit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "")
            catalog = load_catalog(CATALOG)
            roles = dict(catalog.roles)
            for index in range(65):
                identifier = f"catalog-role-{index}"
                roles[identifier] = catalog.roles["scout"]
                (root / ".codex/agents" / f"{identifier}.toml").write_text(
                    f'name = "{identifier}"\n', encoding="utf-8"
                )
            extended = Catalog(catalog.version, roles, catalog.skills, catalog.packs, catalog.codex_model_policy)
            projection = compile_catalog(extended, project_root=root)
            instructions = projection.files[next(path for path in projection.files if str(path) == "AGENTS.md")]
            self.assertIn("`graphic-designer` (`.codex/agents/graphic-designer.toml`)", instructions)

    def test_invalid_custom_role_disabled_pack_and_disabled_skill_fail(self):
        catalog = load_catalog(CATALOG)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CatalogError, "custom role file is missing"):
                compile_catalog(catalog, project_routes=(ProjectRoute("art", "Draw art", "graphic-designer"),), project_root=root)
            project(root, ROUTES)
            with self.assertRaisesRegex(CatalogError, "requires an enabled pack"):
                compile_catalog(catalog, project_routes=(ProjectRoute("web", "Build web UI", "frontend-specialist"),), project_root=root)
            with self.assertRaisesRegex(CatalogError, "requires an enabled pack"):
                compile_catalog(catalog, project_routes=(ProjectRoute("discover", "Plan idea", skills=("tasktra-discovery",)),), project_root=root)
            bad_name = root / ".codex/agents/graphic-designer.toml"
            bad_name.write_text('name = "other"\n', encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "must declare name"):
                compile_catalog(catalog, project_routes=(ProjectRoute("art", "Draw art", "graphic-designer"),), project_root=root)
            bad_name.write_text('name = "graphic-designer"\n', encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "needs a description"):
                compile_catalog(catalog, project_root=root)

    def test_duplicate_and_invalid_declarations_fail_config_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = initialize_project(root)
            profile.write_text(profile.read_text(encoding="utf-8") + """
[[routing.routes]]
id = "art"
trigger = "Draw art"
role = "graphic-designer"
[[routing.routes]]
id = "art"
trigger = "Review art"
role = "graphic-designer"
""", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "duplicate route id"):
                load_project_config(root)
            profile.write_text(profile.read_text(encoding="utf-8").replace('id = "art"\ntrigger = "Review art"', 'id = "review"\ntrigger = "Draw art"'), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "duplicate route trigger"):
                load_project_config(root)
            profile.write_text(profile.read_text(encoding="utf-8").replace('role = "graphic-designer"', 'role = "../escape"'), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_project_config(root)
            profile.write_text(profile.read_text(encoding="utf-8").replace('role = "../escape"', 'role = "graphic-designer"\nroles = ["tester"]'), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "role or roles"):
                load_project_config(root)

    def test_external_skills_are_host_unverified_and_skill_only_routes_need_no_agent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, ROUTES)
            config = load_project_config(root)
            self.assertEqual(config.routes[-1].role, None)
            projection = compile_catalog(load_catalog(CATALOG), project_routes=config.routes, project_root=root)
            instructions = projection.files[next(path for path in projection.files if str(path) == "AGENTS.md")]
            self.assertIn("Check host availability", instructions)
            self.assertIn("Check skill and tool availability", instructions)
            self.assertIn("skill `diagnosing-bugs`", instructions)
            self.assertIn("skill `engineering:documentation`", instructions)


if __name__ == "__main__":
    unittest.main()
