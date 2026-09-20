import tempfile
import unittest
from pathlib import Path, PurePosixPath
import os
import shutil
import subprocess

from tasktra.compiler import Catalog, CatalogError, Pack, check_drift, compile_catalog, load_catalog, resolve_packs, write_projection
from tasktra.ecosystem import plan_monorepo_scopes, preflight_packs, preview_pack_migrations, recommend_packs
from tasktra.routing import route_specialists


ROOT = Path(__file__).resolve().parents[1]


class StageFiveEcosystemTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(ROOT / "catalog")

    def test_composition_is_canonical_for_equivalent_requests(self):
        first = resolve_packs(self.catalog, ("python", "monorepo", "typescript-web"))
        second = resolve_packs(self.catalog, ("typescript-web", "python", "monorepo", "python"))
        self.assertEqual(first, second)
        self.assertEqual(first[0], "core")
        self.assertLess(first.index("generic"), first.index("python"))
        self.assertLess(first.index("generic"), first.index("monorepo"))

    def test_duplicate_ownership_fails_visibly(self):
        packs = dict(self.catalog.packs)
        packs["duplicate-owner"] = Pack(
            "duplicate-owner", "1.0.0", ("core",), (), (), (), owns=("policy:core-authority",)
        )
        catalog = Catalog(self.catalog.version, self.catalog.roles, self.catalog.skills, packs)
        with self.assertRaisesRegex(CatalogError, "duplicate pack ownership"):
            resolve_packs(catalog, ("core", "duplicate-owner"))

    def test_declared_policy_contribution_conflicts_with_owned_policy(self):
        packs = dict(self.catalog.packs)
        packs["duplicate-policy"] = Pack(
            "duplicate-policy",
            "1.0.0",
            ("core",),
            (),
            (),
            (),
            policies=("core-authority",),
        )
        catalog = Catalog(self.catalog.version, self.catalog.roles, self.catalog.skills, packs)
        with self.assertRaisesRegex(CatalogError, "duplicate pack ownership for 'policy:core-authority'"):
            resolve_packs(catalog, ("duplicate-policy",))

    def test_detection_is_preview_only_deterministic_and_evidence_backed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packages" / "web").mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            (root / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
            (root / "packages" / "web" / "package.json").write_text("{}\n", encoding="utf-8")
            before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            first = recommend_packs(root, self.catalog)
            second = recommend_packs(root, self.catalog)
            after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertIn("python", first.recommended)
            self.assertIn("typescript-web", first.recommended)
            self.assertIn("monorepo", first.recommended)
            self.assertGreaterEqual(len(first.evidence), 3)
            self.assertEqual(first.to_dict()["mutation"], "none")

    def test_detection_limits_make_uncertainty_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                (root / f"file-{index}.txt").write_text("x", encoding="utf-8")
            report = recommend_packs(root, self.catalog, max_files=2)
            self.assertTrue(report.truncated)
            self.assertTrue(report.uncertain)
            self.assertEqual(report.files_observed, 2)

    def test_capability_preflight_degrades_optional_and_blocks_untrusted_executable(self):
        report = preflight_packs(self.catalog, ("python",), available_capabilities=())
        self.assertTrue(report["ok"])
        self.assertIn({"pack": "python", "capability": "python"}, report["optional_missing"])
        self.assertFalse(report["unrelated_local_work_blocked"])

        packs = dict(self.catalog.packs)
        packs["vendor-tool"] = Pack(
            "vendor-tool", "1.0.0", ("core",), (), (), (), trust="third-party-executable", source_sha256="d" * 64
        )
        vendor = Catalog(self.catalog.version, self.catalog.roles, self.catalog.skills, packs)
        blocked = preflight_packs(vendor, ("vendor-tool",))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["blockers"][0]["capability"], "checksum-bound-executable-pack-trust")
        self.assertEqual(blocked["blockers"][0]["required_trust"], f"vendor-tool@1.0.0:{'d' * 64}")

    def test_pack_migration_is_exact_and_preview_only(self):
        plan = preview_pack_migrations(self.catalog, {"core": "0.6.0"}, ("core",))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["changes"][0]["to"], "1.0.0")
        self.assertEqual(plan["mutation"], "none")
        blocked = preview_pack_migrations(self.catalog, {"core": "0.3.0"}, ("core",))
        self.assertFalse(blocked["ok"])

    def test_executable_migration_requires_checksum_bound_trust_and_exposes_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_root = Path(directory) / "catalog"
            shutil.copytree(ROOT / "catalog", catalog_root)
            vendor = catalog_root / "packs" / "vendor-tool"
            vendor.mkdir()
            script = vendor / "migrate.py"
            script.write_text("raise SystemExit('preview must not execute')\n", encoding="utf-8")
            manifest = vendor / "pack.toml"
            manifest.write_text(
                """[pack]
id = "vendor-tool"
version = "1.0.0"
contract_version = 1
trust = "third-party-executable"
dependencies = ["core"]
conflicts = []
roles = []
skills = []
owns = ["migration:vendor-tool"]
required_capabilities = []
optional_capabilities = []
validation_commands = []
detection_markers = []
detection_globs = []

[[migration]]
from = "0.9.0"
to = "1.0.0"
kind = "executable"
description = "Preview a bounded vendor migration."
argv = ["python", "migrate.py"]
network = false
read_paths = ["migrate.py"]
write_paths = [".tasktra"]
""",
                encoding="utf-8",
            )
            if os.name == "nt":
                for shadow_name in ("python.exe", "python.cmd"):
                    shadow = vendor / shadow_name
                    shadow.write_bytes(b"pack-controlled tool shadow")
                    with self.assertRaisesRegex(CatalogError, "bare argv\[0\] is shadowed"):
                        load_catalog(catalog_root)
                    shadow.unlink()
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python", "migrate.py"]',
                    'argv = ["python.exe", "migrate.py"]',
                ),
                encoding="utf-8",
            )
            exact_target = catalog_root / "host-python.exe"
            exact_target.write_bytes(b"external linked executable")
            exact_link = vendor / "python.exe"
            try:
                exact_link.symlink_to(exact_target)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(CatalogError, "link or reparse point"):
                    load_catalog(catalog_root)
                exact_link.unlink()
            if os.name == "nt":
                junction_target = catalog_root / "host-python-directory"
                junction_target.mkdir()
                command = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(exact_link), str(junction_target)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(command.returncode, 0, command.stderr or command.stdout)
                with self.assertRaisesRegex(CatalogError, "link or reparse point"):
                    load_catalog(catalog_root)
                exact_link.rmdir()
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python.exe", "migrate.py"]',
                    'argv = ["python", "migrate.py"]',
                ),
                encoding="utf-8",
            )
            catalog = load_catalog(catalog_root)
            blocked = preflight_packs(catalog, ("vendor-tool",))
            token = blocked["blockers"][0]["required_trust"]
            self.assertFalse(blocked["ok"])
            self.assertTrue(preflight_packs(catalog, ("vendor-tool",), trusted_executable_packs=(token,))["ok"])
            preview = preview_pack_migrations(catalog, {"vendor-tool": "0.9.0"}, ("vendor-tool",))
            self.assertFalse(preview["ok"])
            trusted = preview_pack_migrations(
                catalog,
                {"vendor-tool": "0.9.0"},
                ("vendor-tool",),
                trusted_executable_packs=(token,),
            )
            self.assertTrue(trusted["ok"])
            self.assertEqual(trusted["changes"][0]["effects"]["argv"], ["python", "migrate.py"])
            self.assertFalse(trusted["changes"][0]["effects"]["network"])
            script.write_text("print('changed payload')\n", encoding="utf-8")
            changed = load_catalog(catalog_root)
            changed_block = preflight_packs(changed, ("vendor-tool",), trusted_executable_packs=(token,))
            self.assertFalse(changed_block["ok"])
            decoy = vendor / "decoy.txt"
            decoy.write_text("unrelated\n", encoding="utf-8")
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'read_paths = ["migrate.py"]', 'read_paths = ["decoy.txt"]'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "argv must name a hashed read_path"):
                load_catalog(catalog_root)

            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python", "migrate.py"]',
                    'argv = ["python", "migrate.py", "decoy.txt"]',
                ).replace(
                    'read_paths = ["decoy.txt"]',
                    'read_paths = ["decoy.txt"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "unhashed local payloads: migrate.py"):
                load_catalog(catalog_root)

            outside = catalog_root / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python", "migrate.py", "decoy.txt"]',
                    f'argv = ["python", "{outside.as_posix()}", "decoy.txt"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "external unhashable path"):
                load_catalog(catalog_root)

            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    outside.as_posix(), "../../outside.py"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "external unhashable path"):
                load_catalog(catalog_root)

            payload_directory = vendor / "payload-dir"
            payload_directory.mkdir()
            (payload_directory / "__main__.py").write_text("print('directory payload')\n", encoding="utf-8")
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python", "../../outside.py", "decoy.txt"]',
                    'argv = ["python", "payload-dir", "decoy.txt"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "directory payloads are unsupported"):
                load_catalog(catalog_root)

            external_directory = catalog_root / "external-payload-dir"
            external_directory.mkdir()
            (external_directory / "__main__.py").write_text("print('external directory')\n", encoding="utf-8")
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'argv = ["python", "payload-dir", "decoy.txt"]',
                    f'argv = ["python", "{external_directory.as_posix()}", "decoy.txt"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "external unhashable path"):
                load_catalog(catalog_root)

            if os.name == "nt":
                outside_executable = catalog_root / "outside.exe"
                outside_executable.write_bytes(b"external executable")
                manifest.write_text(
                    manifest.read_text(encoding="utf-8").replace(
                        f'argv = ["python", "{external_directory.as_posix()}", "decoy.txt"]',
                        "argv = ['..\\..\\outside.exe', 'decoy.txt']",
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(CatalogError, "external unhashable path"):
                    load_catalog(catalog_root)

                manifest.write_text(
                    manifest.read_text(encoding="utf-8").replace(
                        "argv = ['..\\..\\outside.exe', 'decoy.txt']",
                        "argv = ['C:outside.exe', 'decoy.txt']",
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(CatalogError, "argv\[0\] path is missing or unhashable"):
                    load_catalog(catalog_root)

    def test_pack_contract_version_and_duplicate_arrays_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_root = Path(directory) / "catalog"
            shutil.copytree(ROOT / "catalog", catalog_root)
            core = catalog_root / "packs" / "core" / "pack.toml"
            text = core.read_text(encoding="utf-8")
            core.write_text(text.replace("contract_version = 1", "contract_version = 99"), encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "contract_version"):
                load_catalog(catalog_root)
            core.write_text(text.replace('dependencies = []', 'dependencies = ["core", "core"]'), encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "unique items|duplicate"):
                load_catalog(catalog_root)

    def test_specialist_routing_is_minimal_and_preserves_independent_gates(self):
        decision = route_specialists(
            self.catalog,
            ("generic",),
            ["frontend", "frontend"],
            require_implementation_validation=True,
            require_independent_review=True,
        )
        self.assertEqual(decision["roles"], ["frontend-specialist", "tester", "reviewer"])
        self.assertTrue(decision["separation"]["tester_independent"])
        self.assertTrue(decision["separation"]["reviewer_independent"])
        self.assertEqual(decision["observation"]["retry_count"], 0)
        reordered = route_specialists(self.catalog, ("generic",), ["backend-api", "frontend"])
        same = route_specialists(self.catalog, ("generic",), ["frontend", "backend-api"])
        self.assertEqual(reordered, same)
        with self.assertRaisesRegex(CatalogError, "not enabled"):
            route_specialists(self.catalog, ("core",), ["frontend"])

    def test_representative_pack_fixtures_compile_and_preserve_project_owned_extensions(self):
        selections = {
            "generic": ("generic",),
            "python": ("python",),
            "typescript": ("typescript-web",),
            "monorepo": ("python", "typescript-web", "monorepo"),
        }
        for name, packs in selections.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                owned = root / ".tasktra" / "extensions" / "roles" / "project-specialist.md"
                owned.parent.mkdir(parents=True)
                owned.write_text("project-owned\n", encoding="utf-8")
                project_files = {
                    root / ".tasktra" / "policies" / "local.md": "policy\n",
                    root / ".tasktra" / "workflows" / "local.json": "{}\n",
                    root / ".agents" / "roles" / "project-role.md": "role\n",
                    root / ".codex" / "skills" / "project-skill" / "SKILL.md": "skill\n",
                }
                for path, content in project_files.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                projection = compile_catalog(self.catalog, packs)
                self.assertEqual(projection, compile_catalog(self.catalog, reversed(packs)))
                self.assertIn(PurePosixPath(".tasktra/generated/pack-plan.json"), projection.files)
                write_projection(root, projection)
                drift = check_drift(root, projection, managed_paths=projection.files)
                self.assertTrue(drift.clean)
                self.assertTrue(drift.project_owned)
                self.assertEqual(owned.read_text(encoding="utf-8"), "project-owned\n")
                for path, content in project_files.items():
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_monorepo_plan_bounds_packages_and_fans_out_only_affected_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, manifest in (("packages/api", "pyproject.toml"), ("apps/web", "package.json"), ("packages/unused", "package.json")):
                package = root / relative
                package.mkdir(parents=True)
                (package / manifest).write_text("{}\n", encoding="utf-8")
            report = plan_monorepo_scopes(root, ["apps/web/src/view.tsx"])
            self.assertEqual(report["affected"], ["apps/web"])
            self.assertEqual(report["validation_fanout"][0]["workspace"], "apps/web")
            self.assertEqual(report["isolation"], "per-package-workspace")
            self.assertEqual(report["mutation"], "none")
            all_packages = plan_monorepo_scopes(root, ["shared/config.json"])
            self.assertEqual(set(all_packages["affected"]), {"apps/web", "packages/api", "packages/unused"})


if __name__ == "__main__":
    unittest.main()
