"""Project configuration with deliberately small, explicit defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import tomllib
from types import MappingProxyType
from typing import Mapping

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, require_identifier
from .jira_sync import JiraSyncError, JiraSyncPolicy
from .model_policy import MODEL_TIERS, REASONING_EFFORTS

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
    codex_tier_models: Mapping[str, str] = field(default_factory=dict)
    codex_role_overrides: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    jira_sync: JiraSyncPolicy | None = None
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


def _codex_agents(value: object) -> tuple[Mapping[str, str], Mapping[str, Mapping[str, str]]]:
    agents = _table(value, "agents") if value else {}
    if set(agents) - {"codex"}:
        raise ConfigError("agents permits only the codex table")
    codex = _table(agents.get("codex", {}), "agents.codex")
    if set(codex) - {"model_tiers", "roles"}:
        raise ConfigError("agents.codex permits only model_tiers and roles")
    tiers = _table(codex.get("model_tiers", {}), "agents.codex.model_tiers")
    parsed_tiers: dict[str, str] = {}
    for tier, model in tiers.items():
        if tier not in MODEL_TIERS:
            raise ConfigError(f"agents.codex.model_tiers has unknown tier: {tier}")
        if not isinstance(model, str) or not model.strip() or model == "inherit":
            raise ConfigError(f"agents.codex.model_tiers.{tier} must be a non-empty model id")
        parsed_tiers[tier] = model
    roles = _table(codex.get("roles", {}), "agents.codex.roles")
    parsed_roles: dict[str, Mapping[str, str]] = {}
    for role, override in roles.items():
        try:
            require_identifier(role, label="agents.codex role")
        except IdentifierError as error:
            raise ConfigError(str(error)) from error
        override = _table(override, f"agents.codex.roles.{role}")
        if not override or set(override) - {"model", "reasoning_effort"}:
            raise ConfigError(f"agents.codex.roles.{role} permits only non-empty model/reasoning_effort overrides")
        parsed: dict[str, str] = {}
        if "model" in override:
            model = override["model"]
            if not isinstance(model, str) or not model.strip():
                raise ConfigError(f"agents.codex.roles.{role}.model must be a non-empty model id or inherit")
            parsed["model"] = model
        if "reasoning_effort" in override:
            effort = override["reasoning_effort"]
            if not isinstance(effort, str) or effort not in REASONING_EFFORTS | {"inherit"}:
                raise ConfigError(f"agents.codex.roles.{role}.reasoning_effort is invalid")
            parsed["reasoning_effort"] = effort
        parsed_roles[role] = MappingProxyType(parsed)
    return MappingProxyType(parsed_tiers), MappingProxyType(parsed_roles)


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
    jira_sync_raw = data.get("jira_sync")
    tier_models, role_overrides = _codex_agents(data.get("agents", {}))
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
    jira_sync = None
    if jira_sync_raw is not None:
        if "jira-sync" not in _string_list(packs.get("enabled", ["core"]), "packs.enabled"):
            raise ConfigError("[jira_sync] requires the optional jira-sync pack")
        try:
            jira_sync = JiraSyncPolicy.from_mapping(_table(jira_sync_raw, "jira_sync"))
        except JiraSyncError as error:
            raise ConfigError(str(error)) from error
    return ProjectConfig(
        name=name, version=version, database=database,
        enabled_packs=_string_list(packs.get("enabled", ["core"]), "packs.enabled"),
        validation_commands=_argv_list(validation.get("commands", []), "validation.commands"),
        include_pack_validation_defaults=include_pack_defaults,
        concurrency_limit=concurrency, raw=data,
        catalog_trusted=catalog_trusted,
        codex_tier_models=tier_models, codex_role_overrides=role_overrides,
        jira_sync=jira_sync,
    )
