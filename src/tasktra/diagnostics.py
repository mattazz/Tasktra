"""Read-only identification of the Tasktra implementation running a command."""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib
from typing import Any

from . import __version__
from .state import SCHEMA_VERSION


def _source_package(root: Path) -> Path | None:
    expected = (root / "src" / "tasktra").resolve()
    profile = root / "pyproject.toml"
    if profile.is_file() and (expected / "__init__.py").is_file():
        try:
            with profile.open("rb") as handle:
                project = tomllib.load(handle).get("project", {})
            if isinstance(project, dict) and project.get("name") == "tasktra":
                return expected
        except (OSError, ValueError):
            pass
    return None


def runtime_provenance(root: Path) -> dict[str, Any]:
    """Distinguish a Tasktra source checkout from an ordinary consuming project.

    Editable installs can point at a different worktree even when the command
    runs inside Tasktra's source tree. The target project's location alone must
    never be interpreted as the location of the executing implementation.
    """
    package = Path(__file__).resolve().parent
    expected = _source_package(root.resolve())
    source_root = package.parent.parent
    loaded_checkout = _source_package(source_root) == package
    package_kind = (
        "source-checkout" if loaded_checkout else
        "source-layout-unknown" if package.parent.name == "src" else
        "installed-package"
    )
    comparable = expected is not None and package_kind != "installed-package"
    return {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "package_path": str(package),
        "package_version": __version__,
        "supported_runtime_schema": SCHEMA_VERSION,
        "project_source_path": str(expected) if expected is not None else None,
        "source_matches_project": package == expected if comparable else None,
        "package_kind": package_kind,
        "loaded_source_checkout": str(source_root) if loaded_checkout else None,
        "foreign_source_checkout": comparable and package != expected,
    }
