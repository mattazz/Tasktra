# ADR 0015: Durable operator interventions

## Status

Accepted for isolated implementation after architecture and authority review,
2026-10-08. Integration and runtime migration remain separate gates.

## Context

A worker can explain a blocker in a handoff while its work unit still holds a
lease. Ordinary unsuccessful completion releases the lease, but its arbitrary
outcome evidence does not provide a structured operator inbox or bind a later
answer to the request that stopped work.

## Decision

Persist one bounded intervention request when the current lease holder yields
an attempt. Record the request, terminal outcome, usage accounting, lease
release, and current unit pointer in one transaction. Keep the original request
immutable and bind retries to both its content and the original accounting
inputs, including whether elapsed time was supplied or measured.

Record attributed responses as immutable revisions with one sealed current
head. Each correction must identify the response it replaces. A declined,
cancelled, or mistaken answer can therefore be corrected without rewriting
history or permanently stranding the unit. Local actor attribution follows the
existing CLI trust model; it is not identity authentication.

Keep evidence and authority separate. An answer never changes work eligibility
or creates an approval. Structured requeue still requires the existing
work-requeue authority and evidence, plus the exact request and reviewed
response identity and digest. The transaction rejects a changed response head
and closes the request only when that reviewed head is still current and
answered. Exact historical retries acknowledge the earlier operation without
altering newer work.

Use explicit schema migration for request, response, response-head, and closure
records. Preserve existing outcomes and expose older blocked work as
unstructured; do not infer requests from prose. Include all new authoritative
records and relational invariants in integrity checks. Older clients must
refuse the new schema rather than make partially compatible writes.

Serve a bounded inbox and detail through verified read snapshots. Summarize
current response heads without loading response history. History uses indexed
keyset pagination so corrections remain possible without a revision-count cap.
Portfolio views expose counts and attention labels. Full selected evidence is
available only through explicit detail; inbox rows omit evidence locators.

Lease authorization retains only the existing token hash. Yield rejects the
supplied token in request text. Responses reject exact originating-token values
and substrings matching a standard generated token through hash comparison,
alongside credential-pattern checks. This prevents known token echoes without
retaining plaintext secrets; it cannot classify every arbitrary custom secret
embedded in operator prose.

## Considered alternatives

Replaying arbitrary outcome objects or scanning handoff files would avoid new
tables, but would require inference to establish current requests and answers.
One immutable response per request is simpler, but a mistaken declined answer
would strand work. Mutable answers remove that limitation at the cost of
losing the evidence an operator reviewed. Revisioned responses preserve both
correction and exact requeue binding.

## Consequences

Intentional waits become visible after releasing execution capacity. The
operator can inspect the request, correct a response, and separately authorize
a new attempt. There is no automatic response, requeue, notification, or
execution of evidence commands.

The extra durable relationships require migration, race, and attestation tests.
Response history grows over time, although each record and read page is bounded.
Integrity verification streams the complete ledger; page size bounds projection
payloads and does not make verification time independent of history size.
The design does not add dashboard controls or change existing retry, stop,
dependency, checkpoint, or approval semantics.
