# ADR 0019: Read-only work-unit inspection

## Status

Accepted for isolated implementation after design and authority review,
2026-10-08. Primary integration remains a separate gate.

## Context and decision

Goal readiness identifies work worth investigating, but understanding one unit
requires several commands for attempts, interventions, workers and effects.
Add a work inspection that joins their metadata in one verified read transaction.
Reuse readiness structure and operational facts through connection-scoped
helpers, preserving all existing public response shapes.

Keep capture provenance separate from command preconditions. The captured audit
head identifies the inspected snapshot; existing commands do not accept it as a
freshness guard. Offer mutation templates only for a current structured
intervention's exact response and for a worker run the existing reconciliation
adapter can observe. Those commands retain their existing authority, conflict
and idempotence checks. Goal-wide recovery, next-work claiming, legacy requeue
and latest-dispatch provider reconciliation cannot bind the same selected
episode, so inspection provides no mutation template for them.

## Privacy boundary

Project validated statuses, identities, timestamps, counts and content digests.
Omit titles, actor names, request and result bodies, evidence prose and raw
repository context. Existing detail commands remain separate navigation and may
show more content. Screen emitted identities against their originating attempt's
lease material; omit unsafe identities and the commands that require them.
This targeted screening does not establish that every persisted string is free
of every possible secret.

Use a closed registry for interpreted activity events. Unknown event names are
themselves arbitrary text, so return their digest and a fixed label with an
empty metadata object. Recognized payload values require native validation and
an exact authoritative relation. Audit consistency establishes provenance, not
the safety or meaning of arbitrary recorded text.

## Consequences

Attempts and activity use exclusive descending keyset cursors. Each evidence
family returns a bounded recent window, with explicit truncation and redaction
counts and no unbounded backfill. Attempt rows remain mutable snapshots; an
activity page is a curated observation history, not a reconstruction of every
past state.

Full audit and state verification still visit the ledger, and one selected unit
requires its goal's dependency graph. Sparse activity, selected worker histories
and provider lookups can scan or sort more rows than the returned page. Keeping
schema 14 avoids a migration or a second persisted timeline, at the cost of those
documented scans. Pagination bounds history output, not verification latency.

Reads preserve ledger state, the database and any pre-existing WAL contents.
SQLite may change shared-memory coordination or create an empty WAL. A future
integrated control surface must preserve these identity and authority boundaries
rather than treating a captured inspection as permission to execute.
