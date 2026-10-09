# ADR 0012: Atomic work-plan loading

## Status

Accepted after independent architecture review, 2026-10-08.

## Context

Large goals need many related work units. Individual creation requires each
prerequisite to exist first, so operators must order commands themselves and
recover partially loaded plans after errors. The existing ledger can represent
the desired graph but lacks a single transaction for loading it.

## Decision

Accept a bounded, closed version-1 manifest describing a single goal's unit
identifiers, titles, scopes, checkpoints, and prerequisites. Allow forward
references within the manifest. Preview canonical definitions, structural
dependency waves, and create/unchanged counts from one verified read snapshot.
Reject conflicting existing definitions; this operation never edits them.

Use a stable manifest digest and a separate preview digest. The preview binds
the resolved database target, current authority contract, existing immutable
definitions and edges, and applicable creation gates. Execution status, lease
renewals, and unrelated audit events do not alter immutable definitions.

Apply re-evaluates the same invariants under one serialized writer transaction
and requires the exact preview digest. Insert units, edges, audit evidence, and
state seals atomically. Single-unit and batch creation share their validation
and insertion helpers. Existing single-unit behavior remains compatible.

After successful application, a fresh preview identifies all matching units as
unchanged. Applying that preview writes no rows or events. A stale preview must
be refreshed; same-digest execution receipts are outside this iteration.

Require an existing valid goal contract for batch loading. Manifests cannot
create or expand contracts, activate goals, grant approvals, claim work, or
dispatch agents. Dependency waves describe structure, not execution permission.

## Alternatives and consequences

Sequential CLI calls can partially create a graph and cannot validate forward
references as one plan. Persisting a separate plan entity would add migrations
and synchronization rules without improving the current creation workflow.
The existing schema-12 tables and per-unit evidence represent the complete
result, so no new runtime schema is needed.

Relevant changes between preview and apply require another preview. Operators
gain a reviewable result and an atomic application while retaining explicit
execution approval and immutable work topology.
