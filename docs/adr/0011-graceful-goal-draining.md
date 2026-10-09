# ADR 0011: Graceful goal draining

## Status

Accepted after independent architecture review, 2026-10-08.

## Context

Operators need to stop taking new work while current workers finish. Immediate
pause terminates leases, so it cannot provide a graceful handover or a quiet
point for maintenance. Draining must remain safe when multiple workers finish,
claim, or recover leases concurrently.

## Decision

Add the durable goal status `draining`. It closes intake while preserving stored
leases, tokens, owners, expiry, and reservations. Existing workers can heartbeat
and finish under the usual authority and lease checks. Live lease-bound provider
operations may continue; unleased effect preparation remains active-only.

The final finish or expired-lease recovery atomically changes a draining goal
to `paused`. An expired but unrecovered lease still holds reservations and counts
toward draining. Applying drain to an empty goal pauses immediately. Reapplying
drain is idempotent. Human-approved resume cancels draining without replacing
leases. Immediate pause, stop, and emergency stop keep their existing accounting
and termination semantics.

Preview reads one verified snapshot and reports bounded attempt identifiers,
expiry and live/expired state, aggregate counts, and a digest of the full lease
set. Apply rechecks the current set under the serialized writer transaction.
The preview is advisory and does not reserve that set or grant new authority.

Advance runtime schema 11 to 12 even though no table or column changes. Older
clients cannot safely interpret the new lifecycle state: recovery or emergency
stop could otherwise leave a draining goal with no leases. The version boundary
rejects those writers before mutation. Migration verifies legacy goal statuses,
preserves records, and retains backup and integrity evidence. Only the existing
exact local-upgrade bridges accept schema 11; ordinary writes require schema 12.

## Alternatives and consequences

A separate intake flag would need the same cross-client compatibility boundary
and introduce combinations of flags and lifecycle states. Reusing immediate
pause would interrupt valid work. A distinct state makes the operator's intent
visible in overview, work-selection explanations, and the goal list.

Draining has no deadline. Workers may extend valid leases, and abandoned leases
need normal recovery. Operators retain immediate pause or stop when graceful
completion is no longer appropriate. Scheduled invocations can be previewed
while draining but cannot claim work until the goal is active again.
