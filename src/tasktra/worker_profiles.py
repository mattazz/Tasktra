"""Closed, observable launch profiles for bounded Codex worker sessions.

The objects in this module deliberately model only the small set of Codex
configuration switches Tasktra can safely own.  They are not a general
``-c`` escape hatch: server identities must have been observed by the caller,
and all emitted values are fixed booleans.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PLUGIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}@[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_SERVERS = 32


class WorkerProfileError(ValueError):
    """A requested worker context cannot safely be represented by this CLI."""


def _names(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) > _MAX_SERVERS:
        raise WorkerProfileError(f"{label} exceeds the {_MAX_SERVERS} item bound")
    for value in result:
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise WorkerProfileError(f"{label} contains an unsafe identity")
    if len(set(result)) != len(result):
        raise WorkerProfileError(f"{label} contains duplicate identities")
    return result


def _plugin_servers(values: Iterable[tuple[str, str]], *, label: str) -> tuple[tuple[str, str], ...]:
    result = tuple(values)
    if len(result) > _MAX_SERVERS:
        raise WorkerProfileError(f"{label} exceeds the {_MAX_SERVERS} item bound")
    for value in result:
        if not isinstance(value, tuple) or len(value) != 2:
            raise WorkerProfileError(f"{label} contains an invalid plugin server")
        plugin, server = value
        if not isinstance(plugin, str) or not _PLUGIN_NAME.fullmatch(plugin):
            raise WorkerProfileError(f"{label} contains an unsafe plugin identity")
        _names((server,), label=label)
    if len(set(result)) != len(result):
        raise WorkerProfileError(f"{label} contains duplicate identities")
    return result


@dataclass(frozen=True)
class CliCapabilities:
    """A bounded observation of the installed CLI's documented flags."""

    checked: bool = False
    supports_profile: bool = False
    supports_config_overrides: bool = False

    @classmethod
    def from_help(cls, cli_help: str, exec_help: str) -> "CliCapabilities":
        """Derive support only from both bounded help responses."""
        if not isinstance(cli_help, str) or not isinstance(exec_help, str):
            raise WorkerProfileError("CLI capability help must be text")
        # Profiles are an exec launch flag.  Checking both surfaces avoids
        # treating a stale top-level parser as support for this invocation.
        return cls(
            checked=True,
            supports_profile="--profile" in cli_help and "--profile" in exec_help,
            supports_config_overrides="--config" in cli_help and "--config" in exec_help,
        )


@dataclass(frozen=True)
class WorkerContext:
    """An explicit opt-in request to focus one local worker session.

    The caller provides the effective server identities it observed.  A
    selection outside that observation is rejected instead of guessing a
    configuration key.  ``enable_*`` supports an explicit later expansion of
    an inherited profile; omission leaves the user's setting untouched.
    """

    focused: bool = False
    user_profile: str | None = None
    observed_mcp_servers: tuple[str, ...] = ()
    observed_plugin_mcp_servers: tuple[tuple[str, str], ...] = ()
    disable_mcp_servers: tuple[str, ...] = ()
    disable_plugin_mcp_servers: tuple[tuple[str, str], ...] = ()
    enable_mcp_servers: tuple[str, ...] = ()
    enable_plugin_mcp_servers: tuple[tuple[str, str], ...] = ()
    requested_capabilities: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Any) -> "WorkerContext":
        """Read a closed JSON-compatible context without accepting config keys.

        ``observed_*`` are the bounded, credential-free identity records from
        the host's own discovery.  Their ``enabled`` flags are intentionally
        read but never used to infer a permission or silently alter a choice.
        """
        if not isinstance(value, dict):
            raise WorkerProfileError("worker context must be an object")
        allowed = {
            "focused", "user_profile", "observed_mcp_servers", "observed_plugin_mcp_servers",
            "disable_mcp_servers", "disable_plugin_mcp_servers", "enable_mcp_servers",
            "enable_plugin_mcp_servers", "requested_capabilities",
        }
        if set(value) - allowed:
            raise WorkerProfileError("worker context contains unsupported fields")

        def strings(key: str) -> tuple[str, ...]:
            raw = value.get(key, [])
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise WorkerProfileError(f"{key} must be a string array")
            return tuple(raw)

        def plugin_servers(key: str) -> tuple[tuple[str, str], ...]:
            raw = value.get(key, [])
            if not isinstance(raw, list):
                raise WorkerProfileError(f"{key} must be an array")
            result: list[tuple[str, str]] = []
            for item in raw:
                allowed_item = {"plugin", "server", "enabled"} if key == "observed_plugin_mcp_servers" else {"plugin", "server"}
                if not isinstance(item, dict) or set(item) != allowed_item:
                    raise WorkerProfileError(f"{key} contains an invalid plugin server")
                if "enabled" in item and not isinstance(item["enabled"], bool):
                    raise WorkerProfileError(f"{key} contains an invalid enabled flag")
                if not isinstance(item["plugin"], str) or not isinstance(item["server"], str):
                    raise WorkerProfileError(f"{key} contains an invalid plugin server")
                result.append((item["plugin"], item["server"]))
            return tuple(result)

        focused = value.get("focused", False)
        profile = value.get("user_profile")
        if not isinstance(focused, bool) or (profile is not None and not isinstance(profile, str)):
            raise WorkerProfileError("worker context has invalid scalar values")
        def observed_servers() -> tuple[str, ...]:
            raw = value.get("observed_mcp_servers", [])
            if not isinstance(raw, list):
                raise WorkerProfileError("observed_mcp_servers must be an array")
            result: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and set(item) == {"id", "enabled"} and isinstance(item["id"], str) and isinstance(item["enabled"], bool):
                    # The flag is evidence only.  It allows a caller to retain
                    # the observed effective state without trusting it as
                    # permission to change some other identity.
                    result.append(item["id"])
                else:
                    raise WorkerProfileError("observed_mcp_servers contains an invalid identity")
            return tuple(result)

        return cls(
            focused=focused, user_profile=profile,
            observed_mcp_servers=observed_servers(),
            observed_plugin_mcp_servers=plugin_servers("observed_plugin_mcp_servers"),
            disable_mcp_servers=strings("disable_mcp_servers"),
            disable_plugin_mcp_servers=plugin_servers("disable_plugin_mcp_servers"),
            enable_mcp_servers=strings("enable_mcp_servers"),
            enable_plugin_mcp_servers=plugin_servers("enable_plugin_mcp_servers"),
            requested_capabilities=strings("requested_capabilities"),
        )

    def __post_init__(self) -> None:
        if self.user_profile is not None:
            _names((self.user_profile,), label="user profile")
        observed = _names(self.observed_mcp_servers, label="observed MCP servers")
        observed_plugin = _plugin_servers(self.observed_plugin_mcp_servers, label="observed plugin MCP servers")
        disabled = _names(self.disable_mcp_servers, label="disabled MCP servers")
        disabled_plugin = _plugin_servers(self.disable_plugin_mcp_servers, label="disabled plugin MCP servers")
        enabled = _names(self.enable_mcp_servers, label="enabled MCP servers")
        enabled_plugin = _plugin_servers(self.enable_plugin_mcp_servers, label="enabled plugin MCP servers")
        requested = _names(self.requested_capabilities, label="requested capabilities")
        if (disabled or disabled_plugin or enabled or enabled_plugin) and not self.focused:
            raise WorkerProfileError("MCP selection requires an explicit focused context")
        if not set(disabled).issubset(observed) or not set(enabled).issubset(observed):
            raise WorkerProfileError("MCP selection is not in the observed effective identities")
        if not set(disabled_plugin).issubset(observed_plugin) or not set(enabled_plugin).issubset(observed_plugin):
            raise WorkerProfileError("plugin MCP selection is not in the observed effective identities")
        if set(disabled) & set(enabled) or set(disabled_plugin) & set(enabled_plugin):
            raise WorkerProfileError("an MCP server cannot be both enabled and disabled")
        # A same-named requested capability is an explicit request to retain
        # that server.  The context must make any conflict visible to its
        # caller rather than silently narrowing it.
        if (set(disabled) | {server for _, server in disabled_plugin}) & set(requested):
            raise WorkerProfileError("focused context disables a requested capability")

    @property
    def needs_capability_check(self) -> bool:
        return self.user_profile is not None or any((
            self.disable_mcp_servers, self.disable_plugin_mcp_servers,
            self.enable_mcp_servers, self.enable_plugin_mcp_servers,
        ))


@dataclass(frozen=True)
class HostLaunchProfile:
    """The exact bounded launch deltas accepted by :class:`CodexHostAdapter`."""

    user_profile: str | None = None
    config_overrides: tuple[str, ...] = ()
    unavailable_capabilities: tuple[str, ...] = ()

    @classmethod
    def from_context(cls, context: WorkerContext | None, capabilities: CliCapabilities | None) -> "HostLaunchProfile":
        if context is None:
            return cls()
        needs_profile = context.user_profile is not None
        needs_overrides = bool(
            context.disable_mcp_servers or context.disable_plugin_mcp_servers
            or context.enable_mcp_servers or context.enable_plugin_mcp_servers
        )
        unavailable: list[str] = []
        if needs_profile and (capabilities is None or not capabilities.checked or not capabilities.supports_profile):
            unavailable.append("named-user-profile")
        if needs_overrides and (capabilities is None or not capabilities.checked or not capabilities.supports_config_overrides):
            unavailable.append("bounded-mcp-overrides")
        if unavailable:
            return cls(unavailable_capabilities=tuple(unavailable))
        overrides: list[str] = []
        for name in context.disable_mcp_servers:
            overrides.append(f"mcp_servers.{name}.enabled=false")
        for plugin, server in context.disable_plugin_mcp_servers:
            overrides.append(f"plugins.{json.dumps(plugin)}.mcp_servers.{server}.enabled=false")
        for name in context.enable_mcp_servers:
            overrides.append(f"mcp_servers.{name}.enabled=true")
        for plugin, server in context.enable_plugin_mcp_servers:
            overrides.append(f"plugins.{json.dumps(plugin)}.mcp_servers.{server}.enabled=true")
        return cls(user_profile=context.user_profile, config_overrides=tuple(overrides))

    @property
    def available(self) -> bool:
        return not self.unavailable_capabilities

    def preview(self) -> dict[str, Any]:
        """Return bounded diagnostics suitable for a receipt, never config data."""
        return {
            "user_profile": self.user_profile,
            "requested_config_overrides": list(self.config_overrides),
            "unavailable_capabilities": list(self.unavailable_capabilities),
            "focused_requested": bool(self.config_overrides),
            # Help text confirms only launch-flag support.  It cannot prove
            # an installed CLI accepted these particular keys or that the
            # resulting session used fewer tools.
            "effective_tool_reduction": "unverified" if self.config_overrides else "not-requested",
        }
