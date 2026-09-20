"""Project configuration with deliberately small, explicit defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import tomllib

from .contracts import ContractError, validate_named

CONFIG_DIRECTORY = ".tasktra"
CONFIG_FILENAME = "project.toml"
DEFAULT_DATABASE = ".tasktra/runtime/tasktra.sqlite"


class ConfigError(ValueError):
    """Raised when a Tasktra project configuration is invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    version: int = 1
    database: str = DEFAULT_DATABASE
    enabled_packs: tuple[str, ...] = ("core",)
    validation_commands: tuple[tuple[str, ...], ...] = ()
    include_pack_validation_defaults: bool = True
    concurrency_limit: int = 3
    catalog_trusted: bool = False
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def database_path(self, root: Path) -> Path:
        project = root.resolve()
        candidate = Path(self.database)
        lexical = candidate if candidate.is_absolute() else project / candidate
        try:
            lexical_absolute = Path(os.path.abspath(lexical))
            lexical_relative = lexical_absolute.relative_to(project)
        except (OSError, ValueError) as error:
            raise ConfigError("runtime.database must resolve inside the project root") from error
        current = project
        for part in lexical_relative.parts:
            current = current / part
            if _is_linklike(current):
                raise ConfigError(f"runtime.database crosses a symbolic link or reparse point: {current}")
        try:
            resolved = lexical_absolute.resolve(strict=False)
            resolved.relative_to(project)
        except (OSError, ValueError) as error:
            raise ConfigError("runtime.database must resolve inside the project root") from error
        return resolved


def config_path(root: Path) -> Path:
    return root / CONFIG_DIRECTORY / CONFIG_FILENAME


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or bool(is_junction and is_junction()):
        return True
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def default_config_text(name: str) -> str:
    safe_name = name.replace('"', "'") or "tasktra-project"
    return f'''# Tasktra project profile. Project-owned settings belong here.
[project]
name = "{safe_name}"
config_version = 1

[runtime]
database = ".tasktra/runtime/tasktra.sqlite"
concurrency_limit = 3

[packs]
enabled = ["core"]

[catalog]
trust_builtin = false

[validation]
include_pack_defaults = true
commands = []
'''


def initialize_project(root: Path, *, name: str | None = None) -> Path:
    """Create only the project definition; never overwrite an existing one."""
    destination = config_path(root)
    if destination.exists():
        raise FileExistsError(f"Tasktra configuration already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(default_config_text(name or root.name), encoding="utf-8")
    return destination


def _table(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(value)


def _argv_list(value: object, name: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of argv arrays, not shell command strings")
    argv: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if isinstance(command, str):
            raise ConfigError(
                f"{name}[{index}] must be an argv array, not a string; "
                'use ["python", "-m", "unittest"]'
            )
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ConfigError(f"{name}[{index}] must be a non-empty argv array of strings")
        if any("\x00" in item for item in command):
            raise ConfigError(f"{name}[{index}] argv items must not contain NUL bytes")
        argv.append(tuple(command))
    return tuple(argv)


def load_project_config(root: Path) -> ProjectConfig:
    path = config_path(root)
    if not path.is_file():
        raise FileNotFoundError(f"No Tasktra configuration at {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {path}: {error}") from error
    validation_raw = data.get("validation", {})
    if isinstance(validation_raw, dict) and "commands" in validation_raw:
        # Keep this diagnostic clear even though the JSON contract also rejects
        # legacy strings.
        _argv_list(validation_raw["commands"], "validation.commands")
    try:
        validate_named(data, "project")
    except ContractError as error:
        raise ConfigError(str(error)) from error
    project = _table(data.get("project"), "project")
    runtime = _table(data.get("runtime", {}), "runtime")
    packs = _table(data.get("packs", {}), "packs")
    catalog = _table(data.get("catalog", {}), "catalog")
    validation = _table(data.get("validation", {}), "validation")
    name = project.get("name")
    version = project.get("config_version", 1)
    database = runtime.get("database", DEFAULT_DATABASE)
    concurrency = runtime.get("concurrency_limit", 3)
    catalog_trusted = catalog.get("trust_builtin", False)
    include_pack_defaults = validation.get("include_pack_defaults", True)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("project.name must be a non-empty string")
    if not isinstance(version, int) or version != 1:
        raise ConfigError("project.config_version must be 1")
    if not isinstance(database, str) or not database.strip():
        raise ConfigError("runtime.database must be a non-empty string")
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ConfigError("runtime.concurrency_limit must be a positive integer")
    if not isinstance(catalog_trusted, bool):
        raise ConfigError("catalog.trust_builtin must be boolean")
    if not isinstance(include_pack_defaults, bool):
        raise ConfigError("validation.include_pack_defaults must be boolean")
    return ProjectConfig(
        name=name, version=version, database=database,
        enabled_packs=_string_list(packs.get("enabled", ["core"]), "packs.enabled"),
        validation_commands=_argv_list(validation.get("commands", []), "validation.commands"),
        include_pack_validation_defaults=include_pack_defaults,
        concurrency_limit=concurrency, raw=data,
        catalog_trusted=catalog_trusted,
    )
