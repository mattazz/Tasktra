# ADR 0018: Goal execution readiness

## Status

Accepted for isolated implementation after design and authority review,
2026-10-08. Primary integration remains a separate gate.

## Context

An overview shows goal progress, selection explains a particular performer's
queue, and dependency impact examines one chosen unit. For a large goal, an
operator still has to guess which units to inspect to understand the current
frontier and the dependencies holding up progress.

## Decision

Add one read-only goal readiness report. Read the graph and operational facts
from one SQLite snapshot after verifying the audit chain and sealed state. Use
the existing graph loader and connection-scoped intervention and execution
counts. Keep the report independent of actor-specific claim decisions.

Only complete units satisfy dependencies. Every other state remains in the
residual graph, including leased, failed and blocked work. Iterative topological
passes calculate dependency waves, downstream depth and membership in a longest
remaining branch. Structural depth counts units; it is not a duration or a
schedule estimate.

The ready frontier contains incomplete units with no incomplete prerequisite.
The blocking frontier contains incomplete units with incomplete direct
dependents, plus terminal-attention units. Order the latter by terminal
attention, direct prerequisite gates cleared if completed, incomplete direct
dependents, longest-branch membership and binary identifier. This is a declared
structural order, not a business-priority decision.

Direct gate counts preserve anchored impact semantics: a sole incomplete
prerequisite clears its dependent's prerequisite gate even when that dependent
already has a complete or other terminal status. A separate count identifies
incomplete dependents. Reuse the existing impact command for transitive detail.

Return bounded, independently paginated ready, blocking and wave sections.
Checkpoint summaries are bounded by the authority contract. Report lifecycle,
contract presence, goal dependencies, checkpoints, budgets, capacity, emergency
stop, interventions and unresolved workers as separate observations. In
particular, zero remaining tokens does not reject a prospective zero-token
reservation. Explain commands retain the actor-specific authority checks.

Emit argument arrays for existing diagnostic commands, with explicit required
placeholders where needed. The CLI binds every array to the inspected project
root. Do not include unbounded titles, request bodies or result text.

## Alternatives considered

Computing transitive impact for every unit would repeat graph walks and increase
large-goal cost. Choosing work automatically would require the existing claim
authority and product priorities. Persisting another graph or ranking would
duplicate current state and require invalidation rules.

## Consequences

Operators can move from a whole-goal report to a particular blocker without
guessing an initial anchor. Graph synthesis is linear in nodes and edges, with
additional frontier sorting. Full-ledger verification has its own cost; the
whole command is not claimed to have goal-local linear complexity.

No schema, control or lifecycle behavior changes. Reads preserve the database
and pre-existing WAL contents. SQLite shared-memory coordination or a new empty
WAL may occur under the existing read-only connection policy. There are no
clock-derived fields, so unchanged snapshots produce repeatable output.
