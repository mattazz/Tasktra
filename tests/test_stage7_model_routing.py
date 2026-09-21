"""End-to-end acceptance coverage for portable Codex model routing."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
import unittest

from tasktra.cli import main
from tasktra.compiler import compile_catalog, load_catalog
from tasktra.config import initialize_project, load_project_config
from tasktra.delegation import projection_overrides


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def planned_sha256(plan: dict[str, object], path: str) -> str:
    writes = plan["managed_writes"]
    assert isinstance(writes, list)
    item = next(item for item in writes if item["path"] == path)
    assert isinstance(item, dict)
    value = item.get("sha256")
    assert isinstance(value, str)
    return value


class PortableModelRoutingAcceptanceTests(unittest.TestCase):
    def test_compile_adopt_and_upgrade_share_project_override_policy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(invoke("init", "--root", str(root), "--apply")[0], 0)
            profile = root / ".tasktra" / "project.toml"
            profile.write_text(
                profile.read_text(encoding="utf-8")
                + """
[agents.codex.model_tiers]
balanced = "project-balanced"

[agents.codex.roles.scout]
model = "inherit"
reasoning_effort = "inherit"
""",
                encoding="utf-8",
            )

            catalog = load_catalog(CATALOG)
            config = load_project_config(root)
            policy, overrides = projection_overrides(catalog, config)
            expected = compile_catalog(
                catalog, config.enabled_packs, codex_model_policy=policy,
                codex_role_overrides=overrides,
            ).files
            scout_path = ".codex/agents/scout.toml"
            implementer_path = ".codex/agents/implementer.toml"
            expected_scout = expected[PurePosixPath(scout_path)]
            expected_implementer = expected[PurePosixPath(implementer_path)]
            self.assertNotIn("model =", expected_scout)
            self.assertNotIn("model_reasoning_effort", expected_scout)
            self.assertIn('model = "project-balanced"', expected_implementer)

            preview_code, adoption = invoke(
                "adopt", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog",
            )
            self.assertEqual(preview_code, 0)
            self.assertEqual(planned_sha256(adoption, scout_path), sha256(expected_scout.encode()).hexdigest())
            self.assertEqual(
                planned_sha256(adoption, implementer_path), sha256(expected_implementer.encode()).hexdigest(),
            )

            compile_args = (
                "compile", "--root", str(root), "--catalog", str(CATALOG), "--trust-catalog",
            )
            compiled_code, compiled = invoke(*compile_args)
            self.assertEqual((compiled_code, compiled["ok"]), (0, True))
            self.assertEqual((root / scout_path).read_text(encoding="utf-8"), expected_scout)
            self.assertEqual((root / implementer_path).read_text(encoding="utf-8"), expected_implementer)
            self.assertEqual(invoke(*compile_args, "--check")[0], 0)

            upgrade_code, upgrade = invoke(
                "upgrade", "--root", str(root), "preview", "--catalog", str(CATALOG), "--trust-catalog",
            )
            self.assertEqual(upgrade_code, 0)
            self.assertEqual(planned_sha256(upgrade, scout_path), sha256(expected_scout.encode()).hexdigest())
            self.assertEqual(
                planned_sha256(upgrade, implementer_path), sha256(expected_implementer.encode()).hexdigest(),
            )

    def test_oversized_delegation_request_fails_without_project_mutation(self) -> None:
        before = snapshot(ROOT / ".tasktra")
        with NamedTemporaryFile("wb", suffix=".json", delete=False) as temporary:
            request = Path(temporary.name)
            temporary.write(b"x" * 65_537)
        try:
            code, result = invoke("delegation", "--root", str(ROOT), "plan", str(request))
        finally:
            request.unlink(missing_ok=True)
        self.assertEqual(code, 2)
        self.assertIn("runtime JSON exceeds 64KiB", str(result["error"]))
        self.assertEqual(snapshot(ROOT / ".tasktra"), before)

    def test_delegation_rejects_ambiguous_or_non_finite_json_without_mutation(self) -> None:
        before = snapshot(ROOT / ".tasktra")
        invalid_documents = (
            (b'{"kind":"tasktra.routing-request","kind":"duplicate"}', "duplicate JSON key"),
            (b'{"value":NaN}', "non-finite JSON value"),
        )
        for raw, expected in invalid_documents:
            with self.subTest(expected=expected), NamedTemporaryFile(
                "wb", suffix=".json", delete=False
            ) as temporary:
                request = Path(temporary.name)
                temporary.write(raw)
            try:
                code, result = invoke("delegation", "--root", str(ROOT), "plan", str(request))
            finally:
                request.unlink(missing_ok=True)
            self.assertEqual(code, 2)
            self.assertIn(expected, str(result["error"]))
            self.assertEqual(snapshot(ROOT / ".tasktra"), before)

    def test_bootstrap_preserves_profile_and_read_only_commands_do_not_create_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = initialize_project(root, name="bootstrap preservation")
            profile_before = sha256(profile.read_bytes()).hexdigest()
            before = snapshot(root)

            status_code, _ = invoke("status", "--root", str(root))
            doctor_code, doctor = invoke("doctor", "--root", str(root))
            self.assertEqual(status_code, 2)
            self.assertEqual(doctor_code, 1)
            self.assertFalse(doctor["ok"])
            self.assertEqual(snapshot(root), before)

            bootstrap_code, bootstrap = invoke("bootstrap", "--root", str(root))
            self.assertEqual((bootstrap_code, bootstrap["config_action"]), (0, "preserve"))
            self.assertEqual(sha256(profile.read_bytes()).hexdigest(), profile_before)
            self.assertTrue((root / ".tasktra" / "runtime" / "tasktra.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
