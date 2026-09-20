"""Stage 7 package, install, compatibility, and removal-boundary checks."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
import zipfile

from tasktra.compiler import compile_catalog, load_catalog, write_projection
from tasktra.config import ProjectConfig
from tasktra.lifecycle import preview_upgrade
from tasktra.manifest import build_generated_manifest, build_lockfile, write_lockfile, write_manifest
from tasktra.state import SCHEMA_VERSION, StateStore


ROOT = Path(__file__).resolve().parents[1]


def packaging_source(destination: Path) -> Path:
    """Copy only distribution inputs so build tools cannot dirty the checkout."""
    source = destination / "source"
    source.mkdir()
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "CHANGELOG.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    for name in ("catalog", "docs", "examples", "src"):
        shutil.copytree(
            ROOT / name, source / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
    return source


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, shell=False, check=False, timeout=180)


class PackagingTests(unittest.TestCase):
    def test_wheel_is_reproducible_complete_and_works_from_isolated_target(self) -> None:
        if importlib.util.find_spec("wheel") is None:
            self.skipTest("wheel build tooling is not installed; CI installs it explicitly")
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = packaging_source(temporary)
            environment = dict(os.environ)
            environment["SOURCE_DATE_EPOCH"] = "1700000000"
            wheels: list[Path] = []
            for index in range(2):
                destination = temporary / f"wheel-{index}"
                destination.mkdir()
                completed = run([
                    sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                    "--wheel-dir", str(destination), str(source),
                ], cwd=source, env=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                wheels.append(next(destination.glob("tasktra-1.0.0-*.whl")))
            self.assertEqual(sha256(wheels[0].read_bytes()).hexdigest(), sha256(wheels[1].read_bytes()).hexdigest())

            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            for required in (
                "tasktra/catalog/catalog.toml",
                "tasktra/catalog/core-skills.toml",
                "tasktra/catalog/roles/scout.md",
                "tasktra/catalog/packs/core/pack.toml",
                "tasktra/schemas/project.json",
            ):
                self.assertIn(required, names)

            target = temporary / "target"
            installed = run([
                sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
                "--target", str(target), str(wheels[0]),
            ], cwd=temporary)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            isolated = dict(os.environ)
            isolated["PYTHONPATH"] = str(target)
            project = temporary / "project"
            project.mkdir()
            version = run([sys.executable, "-m", "tasktra", "--version"], cwd=project, env=isolated)
            self.assertEqual((version.returncode, version.stdout.strip()), (0, "tasktra 1.0.0"))
            initialized = run([sys.executable, "-m", "tasktra", "init", "--root", str(project), "--apply"], cwd=project, env=isolated)
            self.assertEqual(initialized.returncode, 0, initialized.stderr or initialized.stdout)
            compiled = run([sys.executable, "-m", "tasktra", "compile", "--root", str(project), "--trust-catalog"], cwd=project, env=isolated)
            self.assertEqual(compiled.returncode, 0, compiled.stderr or compiled.stdout)
            before = {path.relative_to(project).as_posix(): sha256(path.read_bytes()).hexdigest() for path in project.rglob("*") if path.is_file()}
            # Distribution removal is represented by deleting only this isolated
            # target. Project state is deliberately outside that boundary.
            for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            target.rmdir()
            after = {path.relative_to(project).as_posix(): sha256(path.read_bytes()).hexdigest() for path in project.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_sdist_contents_are_stable_and_include_canonical_sources(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = packaging_source(temporary)
            environment = dict(os.environ)
            environment["SOURCE_DATE_EPOCH"] = "1700000000"
            content_hashes: list[str] = []
            for index in range(2):
                destination = temporary / f"sdist-{index}"
                destination.mkdir()
                completed = run([
                    sys.executable, "setup.py", "--quiet", "sdist", "--dist-dir", str(destination),
                ], cwd=source, env=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                archive_path = next(destination.glob("tasktra-1.0.0.tar.gz"))
                with tarfile.open(archive_path, "r:gz") as archive:
                    entries = []
                    for member in archive.getmembers():
                        if member.isfile():
                            handle = archive.extractfile(member)
                            assert handle is not None
                            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
                            entries.append((relative, sha256(handle.read()).hexdigest()))
                content_hashes.append(sha256(repr(sorted(entries)).encode("utf-8")).hexdigest())
                names = {name for name, _ in entries}
                self.assertIn("catalog/catalog.toml", names)
                self.assertIn("docs/RELEASE_POLICY.md", names)
                self.assertIn("src/tasktra/schemas/project.json", names)
            self.assertEqual(content_hashes[0], content_hashes[1])

    def test_declared_prior_release_edge_previews_0_6_to_1_0_without_writing(self) -> None:
        catalog = load_catalog(ROOT / "catalog")
        with TemporaryDirectory() as directory:
            project = Path(directory)
            projection = compile_catalog(catalog, ("core",))
            write_projection(project, projection)
            manifest = build_generated_manifest(
                projection.files, tasktra_version="0.6.0", catalog_version="0.6.0", packs=("core",),
            )
            core = catalog.packs["core"]
            lock = build_lockfile(
                manifest,
                catalog_source_sha256="a" * 64,
                pack_versions={"core": "0.6.0"},
                pack_contracts={"core": {
                    "version": "0.6.0", "contract_version": core.contract_version,
                    "trust": core.trust, "sha256": core.source_sha256,
                }},
                schema_versions={"runtime": SCHEMA_VERSION},
            )
            write_manifest(project, manifest)
            write_lockfile(project, lock)
            StateStore(ProjectConfig(name="prior").database_path(project)).migrate()
            before = {path.relative_to(project).as_posix(): sha256(path.read_bytes()).hexdigest() for path in project.rglob("*") if path.is_file()}

            preview = preview_upgrade(project, catalog, current_lock=lock).as_dict()

            self.assertTrue(preview["ok"], preview["conflicts"])
            self.assertEqual(preview["target"]["tasktra_version"], "1.0.0")
            self.assertTrue(any(step.get("pack") == "core" and step.get("from") == "0.6.0" and step.get("to") == "1.0.0" for step in preview["migration_steps"]))
            after = {path.relative_to(project).as_posix(): sha256(path.read_bytes()).hexdigest() for path in project.rglob("*") if path.is_file()}
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
