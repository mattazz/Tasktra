# ADR 0017: Observational execution recovery

## Status

Accepted for isolated implementation after design and authority review,
2026-10-08. Primary integration remains a separate gate.

## Context

A coordinator can lose a worker's start or completion receipt while its durable
launch preparation still consumes capacity. Repeating the launch risks duplicate
work. Operators need to discover these reservations and reconcile available host
evidence without changing the parent's execution authority.

## Decision

Expose a bounded unresolved-run list with preparation age, host identity when
known, parent attempt and work-unit state, and retained capacity. Read and verify
the audit and sealed state in one snapshot. The public overview adds execution
attention and capacity counts; the version-1 offline cockpit remains unchanged.

Accept only positively observed running or completed workers from the current
`collaboration.list_agents` adapter. Match the full canonical name against the
supplied parent and stored requested task name, and preserve any recorded host
identity. This establishes consistency of attributed local evidence; it does not
authenticate the historical parent, host, result author or model. The current
adapter exposes no opaque agent ID and requires a null ID. Historical non-null
IDs remain visible and require an adapter that can actually observe that identity.

Reconcile a completed observation in one verified transaction using the existing
schema-14 start and finish receipts, audit events and seals. If the start receipt
is missing, insert both receipts atomically. Store only the existing identity
fields and the digest of exact UTF-8 result bytes. Keep the observation tree and
raw result out of the ledger and output. Usage is unavailable, with null token
measurements. No schema change is required.

An exact factual retry preserves the original observer and recording time even
when another actor supplies it. Conflicting terminal facts fail without writes.
A running observation after any terminal receipt is an explicit stale no-op,
including when the existing terminal receipt contains measured usage.

Count every stored leased attempt against goal capacity, including expired
leases until explicit recovery ends them. Add unresolved runs whose parent is
no longer leased; a leased parent and its worker occupy one goal slot. Wall-clock
liveness is separate. Completion releases a detached worker's reservation but
does not finish or reopen its parent. Requeue still needs its existing separate
approval and evidence.

## Alternatives considered

Sequential start and finish commands can leave a start-only record if the second
write fails. A new recovery table would duplicate the existing immutable facts.
Automatic cancellation or abandonment would require both a control authority
protocol and stronger evidence about worker termination. Missing host data does
not establish that a worker ended.

## Consequences

Operators can resolve lost receipts without another launch. Unknown, absent or
unfamiliar host states keep their reservations and remain visible for review.
The current adapter cannot resolve every historical identity or uncertain worker.
Cancellation and abandonment remain separate future decisions.

Acceptance includes exact-result privacy, cross-actor retries, real concurrent
stores, transaction rollback, capacity parity, historical schema compatibility,
and one actual worker that finishes naturally after its temporary parent blocks.
