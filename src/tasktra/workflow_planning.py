"""Pure, explicit selection of a closed verification workflow policy.

This module does not inspect prose, infer risk, read authority envelopes, or
create a work unit.  Callers supply only declared requirements and the already
authorized policy names; persistence and authority validation remain separate.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .authority import VERIFICATION_POLICIES
from .workflow import policy_roles


class WorkflowPlanningError(ValueError):
    """Raised when explicit requirements cannot select a safe closed policy."""


_WORK_TYPES = frozenset({"implementation", "research", "documentation", "deterministic"})

_MANDATORY_CHECKS: dict[str, tuple[str, ...]] = {
    "implementation-review": ("exploratory-tests", "independent-review"),
    "implementation-deterministic-review": (
        "configured-deterministic-acceptance", "independent-review",
    ),
    "research-review": ("independent-review",),
    "documentation-review": ("independent-review",),
    "deterministic-direct": ("configured-deterministic-acceptance",),
}


def _require_boolean(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowPlanningError(f"{name} must be a boolean")
    return value


def _closed_allowed_policies(allowed_policies: Iterable[str]) -> frozenset[str]:
    try:
        policies = frozenset(allowed_policies)
    except TypeError as error:
        raise WorkflowPlanningError("allowed_policies must be an iterable of policy names") from error
    if not all(isinstance(policy, str) for policy in policies):
        raise WorkflowPlanningError("allowed_policies must contain only policy names")
    unsupported = policies - VERIFICATION_POLICIES
    if unsupported:
        raise WorkflowPlanningError("allowed_policies contains an unsupported verification policy")
    return policies


def _implementation_candidates(*, deterministic_acceptance_available: bool,
                               exploratory_tests_required: bool) -> tuple[str, ...]:
    # The shorter route is eligible only with a configured deterministic
    # acceptance check.  An explicit exploratory-test requirement needs the
    # full implementer/tester/reviewer route and may never be silently erased.
    if deterministic_acceptance_available and not exploratory_tests_required:
        return ("implementation-deterministic-review", "implementation-review")
    return ("implementation-review",)


def plan_verification_policy(
    *,
    work_type: str,
    deterministic_acceptance_available: bool,
    independent_review_required: bool,
    exploratory_tests_required: bool,
    allowed_policies: Iterable[str],
) -> dict[str, Any]:
    """Return an immutable-policy recommendation from explicit structured facts.

    ``allowed_policies`` is a caller-provided capability boundary, normally
    derived from a separately validated authority envelope.  This planner does
    not treat it as an authority grant and does not mutate any state.
    """
    if not isinstance(work_type, str) or work_type not in _WORK_TYPES:
        raise WorkflowPlanningError("work_type is not supported")
    deterministic = _require_boolean(
        deterministic_acceptance_available, name="deterministic_acceptance_available",
    )
    independent_review = _require_boolean(
        independent_review_required, name="independent_review_required",
    )
    exploratory_tests = _require_boolean(
        exploratory_tests_required, name="exploratory_tests_required",
    )
    allowed = _closed_allowed_policies(allowed_policies)

    if work_type == "implementation":
        candidates = _implementation_candidates(
            deterministic_acceptance_available=deterministic,
            exploratory_tests_required=exploratory_tests,
        )
    elif work_type == "research":
        if exploratory_tests:
            raise WorkflowPlanningError("research-review cannot satisfy exploratory_tests_required")
        candidates = ("research-review",)
    elif work_type == "documentation":
        if exploratory_tests:
            raise WorkflowPlanningError("documentation-review cannot satisfy exploratory_tests_required")
        candidates = ("documentation-review",)
    else:
        if not deterministic:
            raise WorkflowPlanningError("deterministic work requires deterministic_acceptance_available")
        if independent_review:
            raise WorkflowPlanningError("deterministic-direct is weaker than independent_review_required")
        if exploratory_tests:
            raise WorkflowPlanningError("deterministic-direct is weaker than exploratory_tests_required")
        candidates = ("deterministic-direct",)

    policy = next((candidate for candidate in candidates if candidate in allowed), None)
    if policy is None:
        required = candidates[0]
        raise WorkflowPlanningError(
            f"allowed_policies does not permit a policy that satisfies {work_type} requirements; "
            f"{required} is required"
        )

    checks = _MANDATORY_CHECKS[policy]
    if independent_review and "independent-review" not in checks:
        raise WorkflowPlanningError("selected policy is weaker than independent_review_required")
    if exploratory_tests and "exploratory-tests" not in checks:
        raise WorkflowPlanningError("selected policy is weaker than exploratory_tests_required")
    if "configured-deterministic-acceptance" in checks and not deterministic:
        raise WorkflowPlanningError("selected policy requires deterministic_acceptance_available")

    return {
        "work_type": work_type,
        "verification_policy": policy,
        "stages": list(policy_roles(policy)),
        "mandatory_checks": list(checks),
        "rationale": [
            f"work-type:{work_type}",
            f"deterministic-acceptance:{str(deterministic).lower()}",
            f"independent-review:{str(independent_review).lower()}",
            f"exploratory-tests:{str(exploratory_tests).lower()}",
            f"selected-from-allowed-policies:{policy}",
        ],
    }
