# ADR 0003: Durable goals and delegated authority

## Status

Accepted.

## Context

Long-running autonomous work must survive sessions without treating a broad objective as unlimited permission.

## Decision

Persist goals with acceptance criteria, scope, prohibitions, authority, budget, checkpoints, and escalation conditions. A goal steward can approve only routine intermediate decisions explicitly covered by an approved goal. Classify effects and gate consequential actions. Treat all external and worker content as untrusted data.

## Consequences

Tasktra gains safe autonomy and auditable decisions but requires structured state, separation-of-duty checks, and deliberate goal authoring.
