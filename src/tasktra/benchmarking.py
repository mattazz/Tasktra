"""Pure, deterministic comparisons for representative execution observations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

MAX_BENCHMARK_OBSERVATIONS = 64
MAX_RETRIEVALS_PER_OBSERVATION = 64
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")

class BenchmarkError(ValueError):
    """A benchmark input is not a bounded representative observation."""

def _label(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise BenchmarkError(f"{field} must be a bounded identifier")
    return value

def _count(value: Any, *, field: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000_000_000:
        raise BenchmarkError(f"{field} must be a non-negative bounded integer")
    return value

@dataclass(frozen=True)
class BenchmarkObservation:
    """One measured representative scenario; this class never runs work itself."""
    scenario: str
    retrievals: tuple[str, ...] = ()
    context_tokens: int | None = None
    escalation_count: int = 0
    retry_count: int = 0
    evidence_count: int = 0
    elapsed_ms: int = 0
    validation_outcome: str = "not-run"
    human_interventions: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario", _label(self.scenario, field="scenario"))
        if not isinstance(self.retrievals, tuple) or len(self.retrievals) > MAX_RETRIEVALS_PER_OBSERVATION:
            raise BenchmarkError(f"retrievals must contain at most {MAX_RETRIEVALS_PER_OBSERVATION} identifiers")
        object.__setattr__(self, "retrievals", tuple(_label(item, field="retrieval") for item in self.retrievals))
        object.__setattr__(self, "evidence_count", _count(self.evidence_count, field="evidence_count"))
        object.__setattr__(self, "context_tokens", _count(self.context_tokens, field="context_tokens", optional=True))
        object.__setattr__(self, "escalation_count", _count(self.escalation_count, field="escalation_count"))
        object.__setattr__(self, "retry_count", _count(self.retry_count, field="retry_count"))
        object.__setattr__(self, "elapsed_ms", _count(self.elapsed_ms, field="elapsed_ms"))
        if self.validation_outcome not in {"not-run", "passed", "failed"}:
            raise BenchmarkError("validation_outcome is not supported")
        object.__setattr__(self, "human_interventions", _count(self.human_interventions, field="human_interventions"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BenchmarkObservation":
        if not isinstance(value, Mapping):
            raise BenchmarkError("benchmark observation must be an object")
        allowed = {
            "scenario", "retrievals", "evidence_count", "context_tokens", "escalation_count",
            "retry_count", "elapsed_ms", "validation_outcome", "human_interventions",
        }
        unknown = set(value) - allowed
        if unknown:
            raise BenchmarkError(f"benchmark observation has unsupported field(s): {', '.join(sorted(map(str, unknown)))}")
        if "scenario" not in value:
            raise BenchmarkError("benchmark observation is missing scenario")
        retrievals = value.get("retrievals", ())
        if not isinstance(retrievals, (list, tuple)):
            raise BenchmarkError("retrievals must be a list of identifiers")
        return cls(
            scenario=value["scenario"], retrievals=tuple(retrievals),
            evidence_count=value.get("evidence_count", 0), context_tokens=value.get("context_tokens"),
            escalation_count=value.get("escalation_count", 0), retry_count=value.get("retry_count", 0),
            elapsed_ms=value.get("elapsed_ms", 0), validation_outcome=value.get("validation_outcome", "not-run"),
            human_interventions=value.get("human_interventions", 0),
        )

@dataclass(frozen=True)
class BenchmarkFinding:
    kind: str
    scenario: str
    detail: str

@dataclass(frozen=True)
class BenchmarkReport:
    """Stable findings only; it intentionally contains no estimated token savings."""
    findings: tuple[BenchmarkFinding, ...]
    compared_scenarios: tuple[str, ...]
    measurements: tuple[Mapping[str, Any], ...]
    @property
    def has_regressions(self) -> bool:
        return bool(self.findings)

@dataclass(frozen=True)
class BenchmarkPlan:
    """A bounded declared scenario set that keeps comparisons representative."""
    scenarios: tuple[str, ...]
    def __post_init__(self) -> None:
        if not 1 <= len(self.scenarios) <= MAX_BENCHMARK_OBSERVATIONS:
            raise BenchmarkError(f"a benchmark plan must contain 1 to {MAX_BENCHMARK_OBSERVATIONS} scenarios")
        scenarios = tuple(sorted((_label(value, field="scenario") for value in self.scenarios), key=str.casefold))
        if len(set(scenarios)) != len(scenarios):
            raise BenchmarkError("benchmark plan scenarios must be unique")
        object.__setattr__(self, "scenarios", scenarios)

def _normalise(observations: Iterable[BenchmarkObservation | Mapping[str, Any]]) -> dict[str, BenchmarkObservation]:
    materialized = tuple(item if isinstance(item, BenchmarkObservation) else BenchmarkObservation.from_mapping(item) for item in observations)
    if not 1 <= len(materialized) <= MAX_BENCHMARK_OBSERVATIONS:
        raise BenchmarkError(f"benchmark comparisons require 1 to {MAX_BENCHMARK_OBSERVATIONS} observations")
    result = {item.scenario: item for item in materialized}
    if len(result) != len(materialized):
        raise BenchmarkError("benchmark scenarios must be unique within each observation set")
    return result

def compare_observations(baseline: Iterable[BenchmarkObservation | Mapping[str, Any]], candidate: Iterable[BenchmarkObservation | Mapping[str, Any]], *, plan: BenchmarkPlan | None = None) -> BenchmarkReport:
    """Compare supplied measurements without executing work or estimating savings."""
    previous, current = _normalise(baseline), _normalise(candidate)
    if set(previous) != set(current):
        raise BenchmarkError("baseline and candidate must cover the same representative scenarios")
    scenarios = tuple(sorted(previous, key=str.casefold))
    if plan is not None and scenarios != plan.scenarios:
        raise BenchmarkError("benchmark observations do not match the declared plan")
    findings: list[BenchmarkFinding] = []
    measurements: list[Mapping[str, Any]] = []
    for scenario in scenarios:
        before, after = previous[scenario], current[scenario]
        measurements.append({
            "scenario": scenario,
            "baseline": _measurement(before),
            "candidate": _measurement(after),
        })
        seen: set[str] = set()
        duplicates: list[str] = []
        for retrieval in after.retrievals:
            if retrieval in seen and retrieval not in duplicates:
                duplicates.append(retrieval)
            seen.add(retrieval)
        if duplicates:
            findings.append(BenchmarkFinding("duplicate-retrieval", scenario, f"candidate repeated retrieval(s): {', '.join(sorted(duplicates, key=str.casefold))}"))
        if before.context_tokens is not None and after.context_tokens is not None and after.context_tokens > before.context_tokens and set(after.retrievals).issubset(set(before.retrievals)):
            findings.append(BenchmarkFinding("avoidable-context-growth", scenario, "candidate context grew without any additional distinct retrieval"))
        if after.escalation_count > before.escalation_count:
            findings.append(BenchmarkFinding("unnecessary-escalation", scenario, "candidate escalated more often than the representative baseline"))
        if after.retry_count > before.retry_count:
            findings.append(BenchmarkFinding("retry-regression", scenario, "candidate retried more often than the representative baseline"))
    return BenchmarkReport(tuple(findings), scenarios, tuple(measurements))


def _measurement(observation: BenchmarkObservation) -> dict[str, Any]:
    return {
        "retrieval_count": len(observation.retrievals),
        "distinct_retrieval_count": len(set(observation.retrievals)),
        "evidence_count": observation.evidence_count,
        "context_tokens": observation.context_tokens,
        "escalation_count": observation.escalation_count,
        "retry_count": observation.retry_count,
        "elapsed_ms": observation.elapsed_ms,
        "validation_outcome": observation.validation_outcome,
        "human_interventions": observation.human_interventions,
    }

class BenchmarkHarness:
    """Small explicit facade around the pure comparison function."""
    def __init__(self, plan: BenchmarkPlan | None = None) -> None:
        self.plan = plan
    def compare(self, baseline: Iterable[BenchmarkObservation | Mapping[str, Any]], candidate: Iterable[BenchmarkObservation | Mapping[str, Any]]) -> BenchmarkReport:
        return compare_observations(baseline, candidate, plan=self.plan)
