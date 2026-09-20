# Stage 3 completion report — Durable autonomy

Status: complete on 2026-09-19. Stage 4 is eligible and active.

## Delivered outcome

Tasktra now has a schema-v5 authoritative SQLite goal ledger with immutable authority envelopes, scoped and expiring transition approvals, separation of duties, ordered evidence checkpoints, atomic work claims, caller-held lease credentials, bounded heartbeats, recovery, retry classification, global execution budgets, idempotent local effect receipts, and tamper-evident audit history. Codex-facing goal, run, status, and stop skills expose these controls without requiring direct database edits.

The runtime fails closed: activation and resume require human authority; completion requires an implement-test-review workflow; final goal acceptance remains human-only; paused, stopped, expired, foreign, or malformed authority cannot mutate work; and blocked work requires separately authorized evidence before requeue.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -q
```

Result: 169 tests passed in 12.736 seconds. Three POSIX-style symbolic-link tests skipped because this Windows account lacks symbolic-link privilege; equivalent containment, traversal, and Windows-junction checks passed.

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_authority tests.test_stage3_acceptance tests.test_stage3_autonomy tests.test_stage3_checkpoints tests.test_stage3_cli tests.test_stage3_integrity tests.test_stage3_ledger tests.test_stage3_operations -q
```

Result: 65 focused authority and Stage 3 tests passed in 4.724 seconds.

```powershell
$env:PYTHONPATH='src'
python -m tasktra validate --root . --run --timeout 60
```

Result: exit 0. The configured complete suite passed in 12.811 seconds and the generated-output check passed in 202 milliseconds.

```powershell
$env:PYTHONPATH='src'
python -m tasktra compile --root . --check
python -m tasktra audit --root . verify --limit 64
```

Result: generated projections were clean. Before self-hosted completion, all 12 live audit events verified; after the claim, workflow-bound completion, and checkpoint transition, all 15 events verified with no issue.

## Migration, recovery, and integrity evidence

The Tasktra repository's own runtime was explicitly migrated from schema 3 to schema 5. A pre-mutation `.v3.bak` snapshot was retained beside the database. Legacy approval scopes were normalized under explicit human control, every authoritative current row was sealed, and a global human ledger attestation verified authority, lifecycle, budgets, attempts, workflow and checkpoint evidence, effects, and the audit chain before the runtime resumed.

Tests prove transaction rollback under an injected mid-write crash; one-owner atomic claim behavior under contention; expiry recovery without duplicate ownership or renewed authority; lease-deadline caps during pause and emergency stop; global token, attempt, elapsed-time, and concurrency enforcement; idempotent effect replay; and tamper detection across authority rows, state seals, workflow evidence, checkpoints, and audit events.

Stage 3 then completed itself through the live runtime. `stage-3-durable-autonomy` was claimed with a caller-generated credential that was never printed or persisted in plaintext, finished through a three-role evidence workflow, and atomically advanced checkpoint `stage-three`. The goal remains active for the later 1.0 checkpoints; no final acceptance evidence was claimed early.

## Authority and efficiency observations

- Authority and evidence inputs are rejected above 64 KiB; workflow input is rejected above 256 KiB.
- Status defaults to aggregates, limits detailed units to 32, and limits audit export batches to 200.
- Routing and eligibility are deterministic; downstream briefs cite at most eight selected evidence records instead of replaying transcripts.
- Lease credentials can remain caller-held and are compared only by hash in the ledger.
- Budget accounting includes live reservations and caps elapsed use at lease expiry.
- Approved transitions must have a finite expiry. Both execution and status reject malformed null-expiry authority.
- The measured complete validation run took 12.811 seconds; no token-savings figure is invented because the runtime does not yet collect local usage telemetry.

## Independent review closure

Independent semantic review found no remaining correctness or security blocker after regressions were added for checkpoint assignment and evidence recomputation, global ledger attestation, stale approvals, workflow hashes, effect receipts, write-locked audit preflight, budget reservations, lifecycle races, lease timing, bounded status, caller-held secrets, explicit requeue, and approval truthfulness.

Independent acceptance review passed the focused 65-test Stage 3 surface, the full 169-test suite, clean projections, the live schema-v5 runtime, and audit integrity. Its final null-expiry observation was resolved by confirming the canonical time-bounded approval invariant and adding defensive execution/status regression coverage.

## Known limitations

- Three symbolic-link tests require a Windows privilege unavailable to this account; the suite retains them for privileged Windows and POSIX CI.
- GitHub and Jira effects are intentionally absent. Stage 4 adds optional adapters without making them a local-work prerequisite.
- The runtime can be driven manually and through Codex, but recurring scheduler adapters remain a Stage 7 deliverable.
- Local usage telemetry and comparative optimization benchmarks remain Stage 6 work.

## Decision

All Stage 3 criteria are satisfied. Durable execution is evidence-bound, resumable, budgeted, checkpointed, and independently reviewed. The live self-hosting ledger reached `stage-three`; Stage 4 may add optional remote capabilities without weakening the offline baseline.
