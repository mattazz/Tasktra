# Stage 3 acceptance checklist — Durable autonomy

Stage 3 makes approved high-level goals resumable across sessions. The local SQLite ledger becomes authoritative for lifecycle, leases, budgets, approvals, recovery, and audit history. It does not add GitHub or Jira effects.

## Scope

- Explicit goal authority envelopes and steward approval boundaries.
- Durable work-unit lifecycle with bounded leases, heartbeats, retries, and recovery.
- Token, attempt, concurrency, and elapsed-time budgets with fail-closed enforcement.
- Idempotent effect intents and receipts for local state transitions.
- Append-only audit history and integrity diagnostics.
- Deterministic eligibility, dispatch, stop, pause, and resume decisions.
- Human-facing Codex controls for approving, inspecting, pausing, resuming, and stopping goals.

## Acceptance criteria

- [x] A goal cannot become active without an explicit authority envelope and acceptance criteria.
- [x] Steward approval is scoped, attributable, non-self-approved, and checked at every protected transition.
- [x] Work units use atomic claims and expiring leases so concurrent workers cannot own the same unit.
- [x] Heartbeats renew only valid leases; stale or foreign workers cannot mutate claimed work.
- [x] Interrupted eligible work recovers without duplicate execution, lost evidence, or renewed authority.
- [x] Retry classification distinguishes transient, permanent, blocked, approval-required, and exhausted outcomes.
- [x] Token, attempt, elapsed-time, and concurrency budgets stop work before limits are exceeded.
- [x] Completion requires the Stage 2 workflow gate plus authoritative ledger evidence.
- [x] Local effects use idempotency keys and receipts so replay is observable and safe.
- [x] Audit events are append-only, transactionally coupled to mutations, ordered, and integrity-checkable.
- [x] Pause, resume, stop, and emergency-stop commands are deterministic and preserve recoverable state.
- [x] Codex-facing skills expose high-level goal control without requiring direct database edits.
- [x] Status and diagnostics are concise by default with opt-in bounded detail.
- [x] Crash, contention, stale-lease, budget, approval, and tamper adversarial tests pass.
- [x] Independent semantic and acceptance reviews find no unresolved blocker.
- [x] Requirements coverage and a Stage 3 completion report are current before closure.

## Exit evidence

Record exact commands and outcomes, database migration and recovery fixtures, concurrency results, budget measurements, authority denials, audit-integrity checks, known limitations, efficiency observations, independent findings, and the Stage 4 eligibility decision.

The recorded evidence and decision are in the [Stage 3 completion report](stage-3-completion.md).
