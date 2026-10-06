"""Read-only Codex delegation planning; this module never dispatches agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .compiler import Catalog, CatalogError, resolve_projection
from .config import ConfigError, ProjectConfig
from .model_policy import CodexModelPolicy, ModelPolicyError, RoleModelMetadata, profile_reasoning_effort
from .routing import build_brief, route_task


class DelegationError(ValueError):
    """Raised when a bounded delegation plan cannot be resolved safely."""


@dataclass(frozen=True)
class AgentProfile:
    role: str
    model: str | None
    reasoning_effort: str | None
    sandbox_mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_type": self.role, "role": self.role, "model": self.model,
            "reasoning_effort": self.reasoning_effort, "sandbox_mode": self.sandbox_mode,
        }


def effective_model_policy(catalog: Catalog, config: ProjectConfig) -> CodexModelPolicy:
    if catalog.codex_model_policy is None:
        raise DelegationError("catalog has no Codex model policy")
    tiers = dict(catalog.codex_model_policy.tier_models)
    tiers.update(config.codex_tier_models)
    try:
        return CodexModelPolicy(tiers)
    except ModelPolicyError as error:
        raise DelegationError(str(error)) from error


def projection_overrides(catalog: Catalog, config: ProjectConfig) -> tuple[CodexModelPolicy, Mapping[str, Mapping[str, str]]]:
    """Return the one resolved override input shared by every projection path."""
    try:
        resolve_projection(
            catalog,
            config.enabled_packs or ("core",),
            projection_roles=config.projection_roles,
            projection_skills=config.projection_skills,
        )
    except CatalogError as error:
        raise DelegationError(str(error)) from error
    unknown = set(config.codex_role_overrides) - set(catalog.roles)
    if unknown:
        raise DelegationError(f"agents.codex has unknown role(s): {', '.join(sorted(unknown))}")
    effective: dict[str, Mapping[str, str]] = {}
    for role_id, role in catalog.roles.items():
        try:
            metadata = RoleModelMetadata(role.model_tier, role.reasoning_effort, role.sandbox_mode)
            effort = profile_reasoning_effort(config.codex_effort_profile, metadata)
        except ModelPolicyError as error:
            raise DelegationError(f"role {role_id} has invalid metadata: {error}") from error
        if effort != metadata.reasoning_effort:
            effective[role_id] = {"reasoning_effort": effort}
    for role_id, override in config.codex_role_overrides.items():
        merged = dict(effective.get(role_id, {}))
        merged.update(override)
        effective[role_id] = merged
    return effective_model_policy(catalog, config), effective


def agent_profile(catalog: Catalog, config: ProjectConfig, role_id: str) -> AgentProfile:
    role = catalog.roles.get(role_id)
    if role is None:
        raise DelegationError(f"agents.codex has an unknown role: {role_id}")
    try:
        _, projected_roles, _ = resolve_projection(
            catalog,
            config.enabled_packs or ("core",),
            projection_roles=config.projection_roles,
            projection_skills=config.projection_skills,
        )
    except CatalogError as error:
        raise DelegationError(str(error)) from error
    if role_id not in projected_roles:
        raise DelegationError(f"agent role is not projected by the enabled packs: {role_id}")
    try:
        metadata = RoleModelMetadata(role.model_tier, role.reasoning_effort, role.sandbox_mode)
    except ModelPolicyError as error:
        raise DelegationError(f"role {role_id} has invalid metadata: {error}") from error
    policy, overrides = projection_overrides(catalog, config)
    override = overrides.get(role_id, {})
    model = policy.model_for(metadata.model_tier)
    effort = metadata.reasoning_effort
    if "model" in override:
        model = None if override["model"] == "inherit" else override["model"]
    if "reasoning_effort" in override:
        effort = None if override["reasoning_effort"] == "inherit" else override["reasoning_effort"]
    return AgentProfile(role_id, model, effort, metadata.sandbox_mode)


def delegation_plan(
    catalog: Catalog, config: ProjectConfig, request: Mapping[str, Any], *, handoff: Mapping[str, Any] | None = None
) -> dict[str, object]:
    """Resolve one existing routing decision into host-required, no-mutation work."""
    decision = route_task(request)
    brief = build_brief(request, decision, handoff=handoff)
    profile = agent_profile(catalog, config, decision["role"])
    return {
        "kind": "tasktra.delegation-plan", "version": 1, "mutation": "none",
        "availability": "host-unverified", "dispatch": "codex-host-required",
        "decision": decision, "brief": brief, "agent": profile.as_dict(),
    }
