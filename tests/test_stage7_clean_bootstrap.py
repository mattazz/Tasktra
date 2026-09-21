"""Clean-checkout bootstrap and newline-portability acceptance coverage."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tasktra.compiler import catalog_digest, load_catalog


ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True,
        shell=False, check=False, timeout=180,
    )


def output(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(completed.stdout or completed.stderr)


def files_below(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class CleanBootstrapAcceptanceTests(unittest.TestCase):
    def test_autocrlf_checkout_bootstraps_with_the_public_cli(self) -> None:
        if importlib.util.find_spec("wheel") is None:
            self.skipTest("wheel build tooling is not installed; CI installs it explicitly")
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "source"
            shutil.copytree(
                ROOT, source,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", "*.egg-info", ".venv", "venv", "build", "dist",
                ),
            )
            environment = dict(os.environ)
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            for command in (
                ["git", "init", "--quiet", str(source)],
                ["git", "-C", str(source), "add", "--all"],
                ["git", "-C", str(source), "-c", "user.name=Tasktra test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "source snapshot"],
            ):
                completed = run(command, cwd=temporary, env=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

            checkout = temporary / "checkout"
            cloned = run(
                ["git", "clone", "--quiet", "--no-local", "-c", "core.autocrlf=true", str(source), str(checkout)],
                cwd=temporary, env=environment,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr or cloned.stdout)

            # The repository policy must win over autocrlf for catalog bytes.
            self.assertEqual(catalog_digest(source / "catalog"), catalog_digest(checkout / "catalog"))
            source_packs = load_catalog(source / "catalog").packs
            checkout_packs = load_catalog(checkout / "catalog").packs
            self.assertEqual(
                {name: pack.source_sha256 for name, pack in source_packs.items()},
                {name: pack.source_sha256 for name, pack in checkout_packs.items()},
            )
            self.assertFalse(any(b"\r\n" in path.read_bytes() for path in (checkout / "catalog").rglob("*") if path.is_file()))

            runtime = temporary / "runtime"
            installed = run(
                [
                    sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-deps", "--no-build-isolation", "--target", str(runtime), str(checkout),
                ],
                cwd=checkout, env=environment,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            public_environment = dict(environment)
            public_environment["PYTHONPATH"] = str(runtime)

            profile = checkout / ".tasktra" / "project.toml"
            profile_before = sha256(profile.read_bytes()).hexdigest()
            state_before = files_below(checkout / ".tasktra")
            doctor_before = run([sys.executable, "-m", "tasktra", "doctor", "--root", str(checkout)], cwd=checkout, env=public_environment)
            self.assertEqual(doctor_before.returncode, 1, doctor_before.stderr or doctor_before.stdout)
            self.assertFalse(output(doctor_before)["ok"])
            status_before = run([sys.executable, "-m", "tasktra", "status", "--root", str(checkout)], cwd=checkout, env=public_environment)
            self.assertEqual(status_before.returncode, 2, status_before.stderr or status_before.stdout)
            self.assertEqual(files_below(checkout / ".tasktra"), state_before)

            bootstrapped = run([sys.executable, "-m", "tasktra", "bootstrap", "--root", str(checkout)], cwd=checkout, env=public_environment)
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr or bootstrapped.stdout)
            self.assertEqual(output(bootstrapped)["config_action"], "preserve")
            self.assertEqual(sha256(profile.read_bytes()).hexdigest(), profile_before)
            self.assertTrue((checkout / ".tasktra" / "runtime" / "tasktra.sqlite").is_file())

            doctor_after = run([sys.executable, "-m", "tasktra", "doctor", "--root", str(checkout)], cwd=checkout, env=public_environment)
            self.assertEqual(doctor_after.returncode, 0, doctor_after.stderr or doctor_after.stdout)
            checked = run([sys.executable, "-m", "tasktra", "compile", "--root", str(checkout), "--check", "--trust-catalog"], cwd=checkout, env=public_environment)
            self.assertEqual(checked.returncode, 0, checked.stderr or checked.stdout)
            goal = run(
                [
                    sys.executable, "-m", "tasktra", "goal", "--root", str(checkout), "create",
                    "Verify local bootstrap", "Create one local runtime record.", "--id", "clean-bootstrap-goal",
                    "--acceptance", "The local bootstrap goal exists.",
                ],
                cwd=checkout, env=public_environment,
            )
            self.assertEqual(goal.returncode, 0, goal.stderr or goal.stdout)
            self.assertEqual(output(goal)["goal"]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
