from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from tasktra.cli import _catalog_root, _catalog_source_trust, main
from tasktra.state import SCHEMA_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


class CliTests(unittest.TestCase):
    def test_editable_install_finds_its_source_catalog_outside_the_checkout(self):
        with TemporaryDirectory() as directory:
            catalog = _catalog_root(Path(directory), None)
            self.assertEqual(catalog, REPOSITORY_ROOT / "catalog")
            self.assertEqual(
                _catalog_source_trust(catalog, False, allow_source_checkout=True),
                "builtin",
            )

    def test_external_catalog_requires_explicit_instruction_trust(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            code, payload = run_cli(
                "compile", "--root", str(root), "--catalog", str(REPOSITORY_ROOT / "catalog"), "--check"
            )
            self.assertEqual(code, 2)
            self.assertIn("non-builtin pack content", payload["error"])

    def test_init_goal_status_and_doctor(self):
        with TemporaryDirectory() as directory:
            root = str(Path(directory))
            code, payload = run_cli("init", "--root", root, "--name", "Example", "--apply")
            self.assertEqual((code, payload["ok"]), (0, True))
            code, payload = run_cli("goal", "--root", root, "create", "Build", "Make it", "--id", "g1", "--budget", "100")
            self.assertEqual((code, payload["goal"]["status"]), (0, "planned"))
            code, payload = run_cli("status", "--root", root)
            self.assertEqual((code, payload["runtime"]["active_goals"]), (0, 0))
            code, payload = run_cli("doctor", "--root", root)
            self.assertEqual((code, payload["ok"]), (0, True))

    def test_init_refuses_existing_profile(self):
        with TemporaryDirectory() as directory:
            root = str(Path(directory))
            self.assertEqual(run_cli("init", "--root", root, "--apply")[0], 0)
            code, payload = run_cli("init", "--root", root, "--apply")
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])

    def test_doctor_reports_old_schema_without_migrating_it(self):
        import sqlite3

        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            database = root / ".tasktra/runtime/tasktra.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA user_version = 1")
            finally:
                connection.close()
            code, payload = run_cli("doctor", "--root", str(root))
            self.assertEqual(code, 1)
            runtime = next(check for check in payload["checks"] if check["name"] == "runtime_state")
            self.assertIn("explicit migration", runtime["detail"])
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                connection.close()

    def test_validation_is_preview_first_and_runs_only_when_requested(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            profile = root / ".tasktra/project.toml"
            profile.write_text(
                profile.read_text(encoding="utf-8").replace(
                    "commands = []",
                    'commands = [["python", "-c", "print(123)"]]',
                ),
                encoding="utf-8",
            )
            code, payload = run_cli("validate", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertEqual(payload["commands"][0]["status"], "planned")
            code, payload = run_cli("validate", "--root", str(root), "--run")
            self.assertEqual(code, 0)
            self.assertEqual(payload["commands"][0]["status"], "passed")
            self.assertIn("123", payload["commands"][0]["stdout"])

    def test_local_work_item_cli_create_list_show_and_update(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(exist_ok=True)
            code, payload = run_cli(
                "work-item", "--root", str(root), "create", "local-flow", "Local flow",
                "--body", "Implement locally.", "--label", "workflow",
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["item"]["version"], 1)
            code, payload = run_cli("work-item", "--root", str(root), "list")
            self.assertEqual([item["id"] for item in payload["items"]], ["local-flow"])
            code, payload = run_cli("work-item", "--root", str(root), "update", "local-flow", "--expected-version", "1", "--status", "ready")
            self.assertEqual((code, payload["item"]["status"], payload["item"]["version"]), (0, "ready", 2))
            code, payload = run_cli("work-item", "--root", str(root), "show", "local-flow")
            self.assertEqual((code, payload["item"]["title"]), (0, "Local flow"))
            code, payload = run_cli("work-item", "--root", str(root), "update", "local-flow", "--expected-version", "2", "--status", "done")
            self.assertEqual(code, 2)
            self.assertIn("completed workflow", payload["error"])

    def test_handoff_template_and_workflow_cli_lifecycle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_arguments = ("--goal-id", "goal-1", "--work-unit-id", "work-1")
            code, payload = run_cli(
                "handoff", "template", *source_arguments, "--actor-id", "impl-1"
            )
            self.assertEqual((code, payload["action"]), (0, "handoff-template"))
            self.assertEqual(payload["handoff"]["status"]["state"], "partial")
            code, specialist = run_cli(
                "handoff", "template", *source_arguments, "--role", "security-auditor",
                "--actor-id", "audit-1", "--handoff-id", "audit-template",
            )
            self.assertEqual((code, specialist["handoff"]["producer"]["role"]), (0, "security-auditor"))
            template_path = root / "template.json"
            template_path.write_text(json.dumps(payload["handoff"]), encoding="utf-8")
            code, validated = run_cli("handoff", "validate", str(template_path))
            self.assertEqual((code, validated["action"]), (0, "handoff-validate"))

            code, created = run_cli("workflow", "create", *source_arguments)
            self.assertEqual((code, created["action"], created["complete"]), (0, "workflow-create", False))
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(created["workflow"]), encoding="utf-8")
            code, checked = run_cli("workflow", "validate", str(workflow_path))
            self.assertEqual((code, checked["action"], checked["complete"]), (0, "workflow-validate", False))

            for role, actor in (("implementer", "impl-1"), ("tester", "test-1"), ("reviewer", "review-1")):
                code, templated = run_cli(
                    "handoff", "template", *source_arguments, "--role", role,
                    "--actor-id", actor, "--handoff-id", f"{role}-1",
                )
                self.assertEqual(code, 0)
                handoff = templated["handoff"]
                handoff["status"] = {"state": "completed", "summary": f"{role} completed its bounded work."}
                evidence_id = f"{role}-evidence"
                handoff["evidence_refs"] = [{
                    "id": evidence_id, "kind": "command", "locator": f"{role}-check",
                    "summary": f"Recorded {role} evidence.",
                }]
                handoff["verified_facts"] = [{
                    "statement": f"{role} completed its bounded evidence review.",
                    "evidence_ids": [evidence_id],
                }]
                if role in {"tester", "reviewer"}:
                    handoff["validation_results"] = [{
                        "name": f"{role}-review", "outcome": "passed", "detail": f"{role} disposition passed.",
                        "evidence_ids": [evidence_id],
                    }]
                handoff_path = root / f"{role}.json"
                handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
                code, accepted = run_cli("workflow", "accept", str(workflow_path), str(handoff_path))
                self.assertEqual((code, accepted["action"]), (0, "workflow-accept"))
                workflow_path.write_text(json.dumps(accepted["workflow"]), encoding="utf-8")

            self.assertTrue(accepted["complete"])
            self.assertEqual(accepted["completion_token"]["source"], {"goal_id": "goal-1", "work_unit_id": "work-1"})
            code, final = run_cli("workflow", "validate", str(workflow_path))
            self.assertEqual((code, final["complete"]), (0, True))
            self.assertEqual(final["completion_token"], accepted["completion_token"])

    def test_workspace_and_capability_reports_are_read_only_and_local_first(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            code, payload = run_cli("workspace", "--root", str(root), "--read-only")
            self.assertEqual(code, 0)
            self.assertTrue(payload["assessment"]["local_work_can_continue"])
            self.assertEqual(payload["recommendation"]["strategy"], "existing-checkout")
            code, payload = run_cli("capabilities", "--root", str(root))
            self.assertEqual(code, 0)
            capabilities = {item["id"]: item for item in payload["capabilities"]}
            self.assertTrue(capabilities["local-work-items"]["available"])
            self.assertFalse(capabilities["jira-connector-adapter"]["available"])
            self.assertTrue(payload["local_work_can_continue"])

    def test_compile_writes_manifest_and_check_detects_drift(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            arguments = ("compile", "--root", str(root), "--catalog", str(REPOSITORY_ROOT / "catalog"), "--trust-catalog")
            code, payload = run_cli(*arguments)
            self.assertEqual((code, payload["ok"]), (0, True))
            self.assertTrue((root / ".tasktra/tasktra.lock").is_file())
            self.assertTrue((root / ".tasktra/generated/manifest.json").is_file())
            lock = json.loads((root / ".tasktra/tasktra.lock").read_text(encoding="utf-8"))
            self.assertEqual(lock["pack_versions"], {"core": "1.0.0"})
            self.assertEqual(lock["schema_versions"]["runtime"], SCHEMA_VERSION)
            self.assertEqual(run_cli(*arguments, "--check")[0], 0)
            (root / ".tasktra/tasktra.lock").unlink()
            code, payload = run_cli(*arguments, "--check")
            self.assertEqual(code, 1)
            self.assertIn("lockfile is missing", payload["metadata_drift"])
            self.assertEqual(run_cli(*arguments)[0], 0)
            (root / ".codex/agents/scout.toml").write_text("changed\n", encoding="utf-8")
            code, payload = run_cli(*arguments, "--check")
            self.assertEqual(code, 1)
            self.assertIn(".codex/agents/scout.toml", payload["changed"])
            self.assertEqual(payload["source_hints"][".codex/agents/scout.toml"], str(REPOSITORY_ROOT / "catalog" / "roles" / "scout.md"))

    def test_compile_refuses_unmanaged_instruction_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            (root / "AGENTS.md").write_text("project owned\n", encoding="utf-8")
            code, payload = run_cli(
                "compile", "--root", str(root), "--catalog", str(REPOSITORY_ROOT / "catalog"), "--trust-catalog"
            )
            self.assertEqual(code, 2)
            self.assertIn("project-owned", payload["error"])

    def test_project_owned_runtime_files_do_not_make_managed_projection_dirty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            arguments = ("compile", "--root", str(root), "--catalog", str(REPOSITORY_ROOT / "catalog"), "--trust-catalog")
            self.assertEqual(run_cli(*arguments)[0], 0)
            extra = root / ".codex" / "notes.md"
            extra.write_text("project owned\n", encoding="utf-8")
            code, payload = run_cli(*arguments, "--check")
            self.assertEqual(code, 0)
            self.assertEqual(payload["project_owned"], [".codex/notes.md"])

    def test_stale_generated_output_requires_explicit_hash_verified_prune(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            catalog = Path(directory) / "catalog"
            shutil.copytree(REPOSITORY_ROOT / "catalog", catalog)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            arguments = ("compile", "--root", str(root), "--catalog", str(catalog), "--trust-catalog")
            self.assertEqual(run_cli(*arguments)[0], 0)
            pack = catalog / "packs" / "core" / "pack.toml"
            pack.write_text(
                pack.read_text(encoding="utf-8").replace(', "tasktra-stop"', ""),
                encoding="utf-8",
            )
            stale = root / ".codex" / "skills" / "tasktra-stop" / "SKILL.md"
            code, payload = run_cli(*arguments)
            self.assertEqual(code, 2)
            self.assertIn("--prune-stale", payload["error"])
            self.assertTrue(stale.exists())
            self.assertEqual(run_cli(*arguments, "--prune-stale")[0], 0)
            self.assertFalse(stale.exists())
            self.assertEqual(run_cli(*arguments, "--check")[0], 0)

    def test_prune_refuses_locally_modified_stale_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            catalog = Path(directory) / "catalog"
            shutil.copytree(REPOSITORY_ROOT / "catalog", catalog)
            self.assertEqual(run_cli("init", "--root", str(root), "--apply")[0], 0)
            arguments = ("compile", "--root", str(root), "--catalog", str(catalog), "--trust-catalog")
            self.assertEqual(run_cli(*arguments)[0], 0)
            pack = catalog / "packs" / "core" / "pack.toml"
            pack.write_text(
                pack.read_text(encoding="utf-8").replace(', "tasktra-stop"', ""),
                encoding="utf-8",
            )
            stale = root / ".codex" / "skills" / "tasktra-stop" / "SKILL.md"
            stale.write_text("locally modified\n", encoding="utf-8")
            code, payload = run_cli(*arguments, "--prune-stale")
            self.assertEqual(code, 2)
            self.assertIn("local edits", payload["error"])
            self.assertEqual(stale.read_text(encoding="utf-8"), "locally modified\n")
