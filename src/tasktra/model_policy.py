"""Portable role-model policy for the Codex projection."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MODEL_TIERS = frozenset({"fast", "balanced", "deep", "exceptional"})
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


class ModelPolicyError(ValueError):
    """Raised when portable role-model metadata is incomplete or invalid."""


@dataclass(frozen=True)
class RoleModelMetadata:
    """Provider-neutral execution metadata required for each role."""

    model_tier: str
    reasoning_effort: str
    sandbox_mode: str

    def __post_init__(self) -> None:
        _registered(self.model_tier, "model_tier", MODEL_TIERS)
        _registered(self.reasoning_effort, "reasoning_effort", REASONING_EFFORTS)
        _registered(self.sandbox_mode, "sandbox_mode", SANDBOX_MODES)


@dataclass(frozen=True)
class CodexModelPolicy:
    """Resolve portable model tiers to project-specific Codex model IDs."""

    tier_models: Mapping[str, str]

    def __post_init__(self) -> None:
        if set(self.tier_models) != MODEL_TIERS:
            missing = sorted(MODEL_TIERS - set(self.tier_models))
            unknown = sorted(set(self.tier_models) - MODEL_TIERS)
            detail = ([f"missing: {', '.join(missing)}"] if missing else []) + ([f"unknown: {', '.join(unknown)}"] if unknown else [])
            raise ModelPolicyError(f"Codex tier mapping must cover exactly the supported tiers ({'; '.join(detail)})")
        for tier, model in self.tier_models.items():
            _registered(tier, "model tier", MODEL_TIERS)
            if not isinstance(model, str) or not model.strip():
                raise ModelPolicyError(f"Codex model for {tier} must be a non-empty string")
        object.__setattr__(self, "tier_models", MappingProxyType(dict(self.tier_models)))

    def model_for(self, model_tier: str) -> str:
        _registered(model_tier, "model_tier", MODEL_TIERS)
        return self.tier_models[model_tier]


def _registered(value: object, label: str, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ModelPolicyError(f"{label} must be one of: {', '.join(sorted(allowed))}")
