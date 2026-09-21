# ADR 0009: Deterministic control plane and portable agent routing

## Status

Accepted.

## Context

Tasktra is operated primarily through Codex, but goals, audit records,
checksums, validation, and generated projections need repeatable behavior that
does not depend on an agent's interpretation. At the same time, treating every
small lookup as agent work wastes context and obscures the reason a model was
selected.

Projects also need to customize the Codex models used by roles without baking
provider-specific model IDs into the canonical role definitions.

## Decision

Keep state transitions, compilation, validation, audit verification, hashing,
capability reporting, and routing-plan construction in a deterministic Python
control plane. The control plane does not launch model agents. A Codex host
selects from its available agents and performs an actual dispatch.

Canonical roles carry portable `model_tier`, `reasoning_effort`, and
`sandbox_mode` metadata. Catalog tier mappings resolve those tiers to Codex
model IDs. A project's `[agents.codex]` configuration may replace individual
tier mappings or set model and reasoning overrides for individual roles. The
precedence order is role override, project tier mapping, then catalog mapping.
`inherit` deliberately omits an overridden native model or effort field; it is
not a request to guess a new value. Sandbox mode stays canonical and cannot be
widened by a project override.

`tasktra delegation plan` produces a bounded, read-only dispatch recommendation
and labels it `host-unverified` and `codex-host-required`. It does not assert
that a host has a requested model, agent, account, or external capability.

Optimize total agent use with this ladder: reuse verified evidence; perform
deterministic operations; use a narrow lower-cost scout for necessary
investigation; assign a bounded implementer for a concrete change; then use a
stronger reviewer or escalation role only for material semantic judgment or a
specific unresolved difficulty. Correctness, safety, and required validation
remain ahead of token efficiency.

## Consequences

Commands such as status, audit verification, compilation, and validation are
expected to run without model delegation. Codex should dispatch specialists for
work that requires reading, interpretation, implementation, or independent
review, with compact evidence rather than copied transcripts.

The generated Codex projection is reproducible from catalog and project
configuration, while the runtime host retains responsibility for availability
and execution. Missing online integrations remain visible capability gaps; they
can make only the dependent operation pending and cannot block eligible local
work.
