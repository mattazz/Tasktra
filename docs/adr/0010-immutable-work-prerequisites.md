# ADR 0010: Immutable work-unit prerequisites

## Status

Accepted after independent architecture review, 2026-10-08.

## Context

Large goals need parallel branches of work and joins that wait for earlier work
to finish. Goal dependencies order whole goals; checkpoints order phases. A
work-unit prerequisite identifies another unit in the same goal that must be
complete before the dependent unit can be claimed.

## Decision

Define a unit's prerequisites atomically with its initial scope and checkpoint.
Prerequisites must already exist, belong to the same goal, and belong to the
same or an earlier checkpoint. The edge set is immutable. Bound each unit to
64 direct prerequisites and provide a paginated structural view.

Store edges as authoritative, audited SQLite state. Existing units migrate with
empty prerequisite sets. Generic selection, selection explanations, and exact
scheduled claims use the same prerequisite readiness rule: every direct
prerequisite has status `complete`. Readiness does not replace approval,
lifecycle, checkpoint, budget, or lease checks.

## Alternatives and consequences

Allowing dependency edits would make replanning easier, but execution approval
does not authorize a change to work topology. Editing also needs rules for
active leases, completed evidence, and stale scheduled invocations. A future
editing operation needs its own explicit authority and exact before/after edge
binding. Creation-only prerequisites provide useful parallel coordination with
the current authority model and preserve schedule invocation version 1.

Units must be defined in prerequisite order. Operators can inspect direct
prerequisites without an unbounded recursive export. Cycles and checkpoint
deadlocks are rejected; completion evidence remains the condition for releasing
dependent work.
