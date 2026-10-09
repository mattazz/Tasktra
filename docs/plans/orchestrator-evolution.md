# Orchestration evolution

The approved direction is to make Tasktra a comprehensive orchestrator and
harness for large, complex projects: useful for understanding the whole project
and controlling individual pieces of work. Development follows an inspect,
prioritize, implement, test, review, and repeat loop.

## Current foundation

The current build provides durable goals, authority envelopes, scoped worker
leases, budget accounting, evidence handoffs, audit integrity, recovery,
capability-based providers, optional packs, and reproducible agent projections.
Conversation is the human interface; the CLI is the deterministic control plane.

The initial review found that these controls are distributed across commands.
Aggregate status does not identify which goals need attention. Claiming work can
return no result without explaining why. Goal dependencies and checkpoints exist,
but individual work units cannot yet describe a dependency graph. Goal pause
immediately interrupts active leases; a mode that finishes current work first
would improve operational control.

An observed development problem also informs the first iteration: the global
editable install pointed to another worktree. Running commands from this checkout
therefore reported a misleading schema mismatch. Selecting the current source
with `PYTHONPATH` showed that its code and database were already compatible.
No database migration was necessary.

## Delivery sequence

| Iteration | User outcome | Acceptance |
| --- | --- | --- |
| Overview and provenance | Understand goal progress and identify the running build | Bounded portfolio and work drilldown; distinguish live and expired leases; show budgets, dependencies, checkpoints, attention reasons; identify foreign source imports; read-only behavior verified |
| Explainable work selection | Understand what will run next and why work is waiting | Shared selector for preview and claim; deterministic candidate choice; coded reasons for blocked candidates; unchanged ledger yields the same candidate; no authorization granted by a preview |
| Work dependencies | Coordinate parallel branches and prerequisites within a goal | Audited dependency edges; reject cycles and invalid references; fan-out and fan-in behavior; backwards-compatible migration; claim and explanation use the same dependency gates |
| Graceful operational control | Pause intake while current workers finish | Preview affected work; block new claims while draining; preserve valid active leases; pause after the last attempt; retain existing immediate pause behavior |
| Atomic work-plan loading | Turn a large plan into coordinated work without partial creation | Bounded manifest, forward references, deterministic preview, exact digest, one transaction, shared creation invariants, unchanged-only retry without new events |
| Dependency impact | Trace upstream blockers and downstream effects of one unit | One verified snapshot, iterative minimum-distance traversal, structural gate counter, stable full-graph counts and bounded relation pages |
| Offline operator dashboard | Move from portfolio attention to granular work in one view | Coherent capture, complete attention summaries, bounded honest graph detail, exact read-only guidance, safe deterministic HTML, responsive browser verification |

Later iterations should be selected from observed usage and reviewed evidence.
Multi-project aggregation and live controls can build on these stable read and
control APIs when concrete workflows justify them.

## First iteration evidence

Completed locally on 2026-10-08: `tasktra overview` supplies a terminal summary
and structured JSON with goal and work pagination, progress, held lease budgets,
dependencies, checkpoints, emergency-stop state, and provider attention.
`tasktra doctor` identifies the running implementation and distinguishes foreign
source checkouts from valid installed-package use.

All seven configured validation commands passed: 388 tests ran with six
platform-dependent skips, followed by a clean generated-output check.
Independent review reported no blockers and passed 41 focused checks, including
clean bootstrap. The completed workflow and success receipt are stored against
`orchestration-overview`; audit verification covered 247 events without issues
after completion. Per-iteration token usage was unavailable and is not estimated.

## Second iteration evidence

Implemented locally on 2026-10-08: `tasktra work explain` reports the next work
candidate and coded reasons for unavailable work. It shares selection rules with
claims, verifies one read-only ledger snapshot, and preserves selection across
pagination and inspection of an individual unit. Existing claims retain their
guard order, exhausted-prefix transitions, and exact scheduled-unit behavior.
Malformed approvals without an expiry now fail closed consistently with the
approval contract and other transition checks.

All seven configured validation commands passed: 405 tests ran with six
platform-dependent skips and a clean generated-output check. Independent review
reported no remaining blockers and passed 48 focused and affected legacy checks.
Evidence for `explain-work-selection` is stored under
`.tasktra/preview/orchestrator-evolution/work-selection/`. The live preview also
correctly selected the next approved iteration outside a one-unit output page.

## Third iteration evidence

Implemented and applied locally on 2026-10-08: work units can declare immutable
prerequisites at creation. Generic claims, explanations, and exact scheduled
claims share prerequisite gates. Bounded graph inspection reports readiness,
and validation rejects invalid references, cycles, and later-checkpoint edges.
Fan-out, fan-in, failed prerequisites, and a 1,100-unit chain have regression
coverage.

The schema-11 migration preserved existing work and authority records. The
first live upgrade attempt exposed a fresh-bootstrap lock mismatch and rolled
back before touching the runtime schema. The recovery now reconciles the
runtime pin when initializing an absent database, requires explicit upgrades
for existing older databases, and records failed exact upgrade receipts on the
retained runtime schema. Both the original failure and successful retry remain
recorded.

All seven configured checks passed on the final source and during the approved
live upgrade: 431 tests ran with six platform-dependent skips. Independent
reviews covered 107 dependency/legacy tests and 28 recovery tests, plus explicit
schema-8 and schema-9 receipt probes. Post-upgrade health, lock/projection checks,
record-preservation comparisons, and verification of 267 audit events passed.
The exact schema-10 database backup and its checksum are retained in
`.tasktra/preview/orchestrator-evolution/upgrade-recovery/live-verification.json`.
Dependency and recovery workflows are stored in the adjacent `work-dependencies`
and `upgrade-recovery` evidence directories.

## Fourth iteration evidence

Completed locally on 2026-10-08: `goal drain` previews or closes intake while
preserving current leases. The final finish or recovery atomically pauses the
goal. Existing workers can heartbeat and finish under normal checks; immediate
pause and stop remain available. Repeated draining and paused retries preserve
database bytes. Tests cover both serialized claim/drain and finish/drain orders,
accounting, provider continuation, scheduling rejection, and attestation.

Runtime schema 12 prevents older writers from misinterpreting draining. A genuine
schema-11 client refused seven write APIs against a temporary schema-12 ledger.
The implementation lease finished before the live upgrade. That upgrade exposed
a collision between successive runtime-only snapshot identities; the bounded
recovery now binds each snapshot to the full lifecycle plan while preserving
historical generic digests and recovery formats.

Final configured and live upgrade validation passed 461 tests with six platform
skips and all seven commands. Independent reviews covered 94 draining tests and
46 recovery tests. Comparison with the schema-11 backup preserved all ten checked
business tables, including dependencies and the unrelated expired lease. The
post-upgrade audit verified 282 events; the superseded approval was then revoked.
The running goal remains active and was only inspected with drain preview.

Evidence is under `graceful-control` and `snapshot-recovery`; the latter contains
the completed recovery workflow, exact upgrade receipt, retained backup checksum,
record-preservation comparison, doctor result, and compilation check.

## Fifth iteration evidence

Completed locally on 2026-10-08: bounded work plans can be previewed and applied
atomically, including forward dependencies. Exact existing definitions are
unchanged; a fresh preview supports retries without writing rows or events.
Single-unit and batch creation share scope, checkpoint, graph, and insertion
checks. Runtime schema 12 remains sufficient.

Review corrected legacy-definition limits, contract-version attestation, coded
integrity errors, malformed-input handling, and the established missing-scope
error contract. All seven configured commands passed on 449 unchanged source
files: 484 tests ran with six platform skips. Independent review passed 64
focused tests plus migrated/unsealed contract and byte-stability probes.

The completed workflow is under `work-plan`. Audit verification covered 289
events after completion. The new commands then loaded the next real work unit
and its prerequisite using the exact preview taken before the fifth unit
finished; immutable definitions remained unchanged. The post-load audit passed
through event 292, and the preview's database hashes were unchanged.

## Sixth iteration evidence

Completed locally on 2026-10-08: `work impact` traces prerequisite ancestors,
incomplete blockers, and downstream dependents from one selected unit. Iterative
traversal reports minimum distances and structural direct-gate changes, with
stable full-graph summaries and bounded pages. Concurrent WAL tests verify that
all fields describe one snapshot; malformed or unverified graphs fail closed.

All seven configured checks passed on 454 unchanged source files: 497 tests ran
with six platform skips. Independent review passed 54 focused tests with one
platform skip, plus a separate database/WAL/SHM byte-stability probe. The live
read-only report preserved database bytes, and audit verification after completion
covered 296 events. Evidence is stored under `dependency-impact`.

## Current iteration

`operator-cockpit-preview` integrates portfolio attention, goal facts, work
drilldown, and anchored impact into a static offline HTML dashboard. One verified
capture supports every view, with explicit omitted-data counts and no graph
claims for incomplete captures. Read-only command guidance retains the exact
interpreter and project source context. Browser verification, cross-language
impact parity, exclusive publication, and independent review gate completion.

## Quality and authority

`operator-interventions` is complete as a reviewed isolated deliverable: all seven
configured commands passed, with 573 tests run and seven skipped. It adds atomic
lease-bound yield, durable requests, attributed response revisions, a bounded
inbox, and separately authorized requeue. Primary integration and live schema-13
migration have not occurred; cockpit browser acceptance remains pending.

The next bounded implementation, `host-execution`, uses a new isolated copy of
that reviewed source. It binds actual Codex worker launches and results to leased
attempts, makes uncertain launches recoverable without automatic duplication,
and distinguishes available measurements from legacy or unavailable accounting.
Schema 14 is exercised only against temporary databases. Its acceptance includes
a real bounded host worker and receipt inspection from a fresh process.

Primary integration and any live upgrade require their own reviewed transitions;
this work does not waive the dashboard's browser acceptance gate.

Every iteration preserves existing user changes, stays within the approved local
scope, exercises focused behavioral regressions, passes configured validation,
and receives independent review. New read surfaces do not create approvals or
infer dispatch authority from status counts. Database changes require the
existing migration, backup, and integrity mechanisms.

The durable local goal is `orchestrator-evolution`. The first work unit is
`orchestration-overview`. Local runtime evidence remains under ignored
`.tasktra/preview/orchestrator-evolution/`; it is not a source of new authority.
The initial current-source baseline ran 364 tests with six platform-dependent
skips and passed the generated-output check on Windows. This evidence does not
resolve the separate cross-platform release gates.
