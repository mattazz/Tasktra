# ADR 0016: Attempt-bound Codex execution

## Status

Accepted for isolated implementation after design and authority review,
2026-10-08. Primary integration and live migration remain separate gates.

## Context

Tasktra can resolve a role and bounded brief, but the actual host worker and its
result currently live in the coordinator conversation. A restarted coordinator
cannot establish from the ledger whether a launch occurred or which result
belongs to an attempt. Repeating an uncertain launch could duplicate work.

## Decision

Bind each Codex run to a current leased attempt through an immutable preparation.
Snapshot the authority, lease generation, workspace and revision identities,
requested role profile, and brief digest. Only the first successful preparation
response permits one immediate launch. Exact retries return reconciliation
guidance, and a fresh key cannot bypass an unfinished run on the same attempt.

The canonical run skill invokes the host's exposed collaboration tools and
records the actual returned canonical name. Store a separate opaque host ID
only when the host supplies one. Record the observed terminal outcome, exact
result digest, and token measurements when available. Keep full briefs and raw
results outside the ledger. Requested model settings remain distinct from
runtime model attestation.

Use three immutable schema-14 tables for preparation, start and finish receipts,
with transactional uniqueness, audit bindings and authoritative state seals.
Derive run state from these records. A start or finish receipt may arrive after
lease expiry because it records history and grants no execution authority.

Treat a preparation without a start receipt as uncertain: the coordinator may
have stopped before or after the host call. Reconcile against the host tree by
the recorded task name. Do not relaunch. An unresolved run blocks parent success,
automatic retry and requeue. Blocking completion or intervention yield can
release the parent lease while preserving the unresolved run and its capacity
reservation. Expiry recovery requires review when the attempt has host execution
history, including a completed worker whose parent did not finish.

Unfinished preparations retain dispatch capacity even after the parent lease
ends. Count all open preparations against the project's dispatch limit. For
the goal's concurrency limit, count leased attempts plus open runs whose parent
is no longer leased; a live parent and its child occupy one slot. A terminal
receipt releases detached occupancy. Report lease counts separately so these
capacity checks do not turn a historical observation into a fictional lease.

Record the source of attempt token accounting. Existing counters remain legacy
values; omitted measurements are unavailable, not measured zero. Host-derived
accounting requires complete measured terminal runs and an exact total. The
current collaboration host exposes no worker token cap, so reservations describe
admission accounting rather than host enforcement.

## Alternatives considered

Audit events alone lack relational uniqueness and efficient attempt lookup.
Existing provider-effect records carry external-effect authority, which would
misrepresent worker observations. A generic executor service would introduce
another runtime before the Codex workflow has a complete recoverable path.

## Consequences

Operators can inspect worker identity, outcome and measurement availability
after coordinator interruption. Sequential role runs share one attempt;
parallel workers use separate work units. Host observations follow the local
CLI attribution model and do not authenticate the host or its actual model.

The launch and receipt cannot form one transaction with the host. If available
host evidence cannot resolve a launch, its slot stays unresolved. This design
does not claim exactly-once execution or add cancellation and abandonment.
Future operator controls must resolve those states explicitly.

Validation must exercise the uncertain-launch window, late receipts, parent
lifecycle guards, migration and tampering, then launch an actual bounded worker
and recover its receipt from a fresh coordinator process.
