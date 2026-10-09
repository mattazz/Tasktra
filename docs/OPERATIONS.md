# Operations guide

## Install and verify

Install Tasktra into a virtual environment or isolated tool environment, then verify the public CLI and catalog before adopting it in a project:

```powershell
python -m pip install --no-deps tasktra-1.0.0-py3-none-any.whl
python -m tasktra --help
python -m tasktra doctor --root .
```

For a fresh checkout of the Tasktra repository itself, its committed
`.tasktra/project.toml` is intentionally preserved while the local runtime is
ignored. Use this one bootstrap path after the editable install; do not use
`init --apply` there:

```powershell
python -m pip install --no-deps -e .
python -m tasktra bootstrap --root .
python -m tasktra doctor --root .
python -m tasktra compile --root . --check --trust-catalog
```

`bootstrap` creates a project profile only when one is absent. When a profile
already exists, it validates and preserves it byte-for-byte. For a missing
runtime, it initializes the database and updates only the runtime-schema entry
in an existing valid lock; other lock fields and generated files are preserved.
An existing older runtime requires the explicit upgrade workflow below.
`status` and `doctor` are read-only: they report a missing runtime rather than
creating one.

Read-only diagnostics and overviews do not change Tasktra records or perform
state transitions. SQLite may create `-wal` and `-shm` bookkeeping sidecars when
reading a database configured for write-ahead logging. These commands use normal
SQLite locking so committed changes in a live WAL remain visible; a WAL database
with missing sidecars may require a writable directory for SQLite to open it.

Package installation and removal affect only distribution files. They do not remove project-owned configuration, generated projections, knowledge, extensions, or runtime state. See [the release policy](RELEASE_POLICY.md).

### Identify the running build

`doctor` includes a `provenance` object with the Python executable and version,
loaded Tasktra package path and version, supported runtime schema, and the
database schema when available. Inside Tasktra's own source checkout it checks
whether the imported package comes from that checkout. A package loaded from a
different Tasktra source checkout fails the diagnostic check and should be
resolved before considering a database migration. Installed or copied package
use against either a consuming project or a Tasktra checkout remains valid;
the different package location is reported without assuming it is erroneous.

For a development session, select the current checkout explicitly without
changing another worktree's editable installation:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m tasktra doctor --root .
python -m tasktra validate --root . --run
```

### Inspect the orchestration overview

```powershell
python -m tasktra overview --root .
python -m tasktra overview --root . --goal-id <goal-id>
python -m tasktra overview --root . --json --limit 20 --offset 0
```

The overview combines goal progress, stored work states, live and expired lease
counts, budgets and reservations, dependencies, checkpoints, and attention
reasons. It is read-only and does not recover leases, resume goals, or approve
work. Its recommendations describe the next investigation or control step;
they do not establish claim eligibility.

The JSON response includes whole-project `aggregates`, a bounded `goals` page,
an optional selected `goal`, `runtime.emergency_stop`, and `pagination` with
`total` and `next_offset`. Read-only command recommendations carry direct
`argv` arrays that retain the selected `--root`.
With `--goal-id`, pagination applies to that goal's work units. The page size is
1–100, and the default is 20. Progress reports work completion and recorded
acceptance evidence separately; neither count implies goal acceptance.
Expired leases continue to occupy concurrency and reserve budget until recovery
releases them. The overview reports those held reservations and highlights
provider effects that need investigation or reconciliation.

### Export an offline operator dashboard

```powershell
python -m tasktra cockpit --root . export tasktra-dashboard.html --page-size 20
```

Open the resulting HTML file locally. The dashboard brings together portfolio
attention, goal progress and budgets, individual work, and dependency impact.
Filters and bounded pages work offline; browser back and forward restore the
selected view. Every view identifies the capture time. Export a new file to see
later activity; the saved file does not refresh itself.

Attention order puts provider failures and reconciliation first, followed by
expired leases, exhausted budgets, goal lifecycle conditions, work requiring
attention, dependencies, missing contracts or work, and planned goals. Priority
and identifier break ties. All attention reasons remain visible.

The capture reads one verified ledger transaction. Counts, lease expiry,
budgets, goal facts, and work graphs describe that same instant. Filesystem
configuration and source observations are labeled separately. Capture identity
includes the schema, audit head, and authoritative-state manifest digest.

Capture uses normal SQLite read-only transactions and leaves durable Tasktra
records unchanged. SQLite may create an empty WAL file and create or update its
SHM coordination file when reading a WAL database. These files can remain after
capture; the command never manually removes them. Existing WAL transaction data
remains protected, including committed data not yet in the main database.

Exports include at most 2,000 goal summaries, 10,000 work units overall, 2,000
units per goal, and 50,000 dependency edges. Every goal needing attention is
included; more than 2,000 attention goals stops the export. Omitted normal goals
and work details have explicit counts. A goal missing any work or edge has no
graph-derived readiness, blocker, distance, or gate-clearing results in the
dashboard. Its stored facts and read-only CLI guidance remain available.
Canonical snapshot data is capped at 20 MiB and the final HTML at 24 MiB.

Dependency results describe structural prerequisite gates. Full claim
eligibility requires actor and envelope inputs plus the existing runtime checks.
The dashboard does not evaluate that eligibility. Copyable commands retain the
captured interpreter, project root, and source checkout binding; a foreign
checkout is visibly identified. Commands are displayed for inspection and are
never executed by the page. If clipboard access is unavailable, select the
visible command text.

The output must be a new `.html` file. Export uses an exclusive same-directory
temporary file and atomic hard-link publication, so even concurrent exports
cannot replace an existing destination. Filesystems without that publication
operation fail without producing an output. The HTML contains minimized project
names, local paths, stored states, and relationships; consider those contents
before sharing it. See [ADR 0014](adr/0014-offline-operator-cockpit.md).

## Operator intervention requests

A worker that needs input can yield its current attempt with a bounded request.
The request and unsuccessful completion are recorded together, releasing the
lease and its reservations. The unit stays blocked until a separate authorized
requeue. This requires runtime schema 13 through the normal explicit upgrade
process; do not run a new client against an unmigrated live runtime.

Inspect the inbox and one request:

```powershell
python -m tasktra intervention --root . list --limit 20
python -m tasktra intervention --root . show <request-id>
python -m tasktra intervention --root . responses <request-id> --after-revision 0 --limit 20
```

The inbox includes older unstructured blocked work. Use `--no-legacy` to show
only structured requests, `--goal-id` or `--work-unit-id` to narrow the list,
and `--include-closed` to inspect prior requests. Inbox evidence omits locators;
explicit detail exposes the selected references. Response history uses revision
numbers for pagination, preserving access to long histories.
Page limits bound returned records. Each capture still verifies the full ledger,
so verification time grows with ledger size even for a small page.

Save a request that identifies the current goal, work unit, attempt, and lease
holder. A request file uses this shape; replace the example identifiers with the
identities from the claimed attempt:

```json
{
  "kind": "tasktra.intervention-request",
  "version": 1,
  "request_id": "confirm-import-format",
  "source": {
    "goal_id": "project-goal",
    "work_unit_id": "import-data",
    "attempt_id": "current-attempt"
  },
  "producer": {"actor_id": "worker"},
  "outcome_class": "blocked",
  "prompt": "Confirm which export format the import must accept.",
  "rationale": "The supplied examples use incompatible column names.",
  "impact": "Import implementation and its direct dependents are waiting.",
  "requires_human_approval": false,
  "evidence_refs": []
}
```

```powershell
python -m tasktra work --root . yield <attempt-id> --actor worker --request request.json --lease-token-env TASKTRA_LEASE_TOKEN
```

Keep the current lease token in the existing environment variable. Never place
it in the request, response, evidence, or command arguments. Yield rejects its
supplied token anywhere in request text. Response validation uses the original
attempt's token hash to reject an exact token value or an embedded token of the
standard generated form; it also rejects common credential patterns. These
checks cannot recognize every custom secret embedded in prose.

For a validated
blocked handoff, `intervention request-from-handoff` can produce a draft from
explicit blocker, requested-action, and evidence identifiers. The converter
does not verify a live lease; yielding does.

A response records an answer and the exact request digest. Its
`expected_current_response` is `null` for the first answer, or an object with
the current `response_id` and `response_sha256` when correcting an answer.
Record it using `intervention respond response.json --actor <actor>
--actor-kind human|steward`. Requests classified as `approval-required` require
a human-attributed response, and still need the existing transition approval.
Local actor attribution is an assertion, not identity authentication.

Answers do not approve execution. Declined and cancelled answers leave the
request open and can be corrected by a new response revision. Requeue requires
the latest answer to have disposition `answered`, the usual work-requeue
approval and evidence, and the exact identities reviewed by the operator:

```powershell
python -m tasktra work --root . requeue <unit-id> --actor <performer> --envelope-sha256 <envelope-digest> --evidence-json requeue-evidence.json --request-id <request-id> --response-id <response-id> --response-sha256 <response-digest>
```

If the answer changes before requeue commits, reread the request and review the
new response. Exact historical retries acknowledge the original operation and
never reapply it to a newer attempt. Older unstructured blockers retain their
existing requeue workflow and omit all three intervention identity arguments.
See [ADR 0015](adr/0015-durable-operator-interventions.md).

## Codex-first operation

Codex is the normal human interface. Ask it to inspect or adopt Tasktra, define a durable goal, and state the effect boundary. The CLI remains the deterministic fallback for every state transition. Generated instructions, scheduler prompts, worker output, provider data, and CI artifacts are inputs only; authority comes from the human-approved goal envelope and recorded approvals.

For an existing project, preview before applying:

```powershell
python -m tasktra adopt --root .
python -m tasktra compile --root . --check
python -m tasktra validate --root .
```

Resolve every ownership conflict or uncertainty before a write. Existing instruction systems remain project-owned until deliberately composed or preserved.

## Model routing and delegation plans

Canonical Tasktra roles are portable: each declares a model tier, reasoning
effort, and sandbox mode. The catalog's default Codex mapping is `fast` to
`gpt-5.6-luna`, `balanced` to `gpt-5.6-terra`, `deep` to `gpt-5.6-sol`, and
`exceptional` to `gpt-6-astra`. A project may replace particular tier models
and, when necessary, the model or reasoning effort of a particular role:

```toml
[agents.codex.model_tiers]
fast = "project-fast-model"

[agents.codex.roles.scout]
reasoning_effort = "low"

[agents.codex.roles.reviewer]
model = "project-review-model"
```

Resolution is deterministic: a role-specific setting wins, then the project's
tier mapping, then the catalog mapping. A per-role `model = "inherit"` or
`reasoning_effort = "inherit"` omits that native Codex field so the host can
use its own default. Sandbox mode is canonical role policy and cannot be
widened through project configuration. After changing model configuration,
recompile the managed projections and review the resulting drift before
accepting it.

Use a delegation plan when Codex needs a bounded brief and resolved role
profile without making the Python CLI pretend that it can launch a Codex
subagent:

```powershell
python -m tasktra delegation --root . plan routing-request.json
python -m tasktra delegation --root . plan routing-request.json --handoff .tasktra/handoffs/verified.json
```

The result is read-only (`mutation: none`) and intentionally reports
`availability: host-unverified` and `dispatch: codex-host-required`. It is a
recommendation and handoff-ready brief, not confirmation that a model or agent
is available. Codex (or another capable host) performs any actual dispatch
after applying its own availability checks and the goal's authority limits.

For an already claimed, live attempt, record one actual Codex child through the
receipt commands. The Python CLI never launches the child:

```powershell
python -m tasktra delegation --root . prepare <attempt-id> --actor <performer> `
  --request routing-request.json --idempotency-key <key> --lease-token-env TASKTRA_LEASE_TOKEN
python -m tasktra delegation --root . start <run-id> --actor <performer> `
  --host-canonical-name <exact-returned-task-name>
<exact-final-result-command> | python -m tasktra delegation --root . finish <run-id> `
  --actor <performer> --outcome completed --usage-status unavailable --result-stdin
python -m tasktra delegation --root . show <run-id>
```

### Recover an unresolved Codex receipt

Use this only to record a current, factual host-tree observation. First inspect
the bounded queue, then make one `collaboration.list_agents` call and construct
the closed version-1 `tasktra.codex-host-tree-observation` from exact canonical names. The adapter recognizes only
the actual `running` string and a completed object with final text. Unknown,
missing, or unfamiliar statuses remain unresolved.

```powershell
python -m tasktra delegation --root . unresolved --goal-id <goal-id>
python -m tasktra delegation --root . reconcile <run-id> --actor <performer> --observation <observation.json>
python -m tasktra delegation --root . show <run-id>
```

The observation has exactly this shape. Replace the example names and capture
time with values from the same host-tree observation. The target name must use
the stored run's `requested_task_name` under the observed parent.

```json
{
  "kind": "tasktra.codex-host-tree-observation",
  "version": 1,
  "source": "collaboration.list_agents",
  "captured_at": "2026-10-08T15:00:00Z",
  "parent_canonical_name": "/root",
  "observed_agent_names": ["/root/codex_example_1_scout"],
  "target": {
    "canonical_name": "/root/codex_example_1_scout",
    "agent_id": null,
    "status": {"kind": "running", "source_shape": "running-string"}
  }
}
```

For an actual completed object, use `{"kind":"completed","source_shape":"completed-object"}`
and add `--result-stdin`. Supply the exact final text encoded as UTF-8 through a
binary stdin interface, such as `subprocess.run(..., input=final_text.encode("utf-8"))`.
Do not add a newline, normalize whitespace, or use a text pipeline that changes
the bytes. Empty final text is valid. The result limit is 64 KiB; the observation
file limit is 32 KiB. Raw results are discarded after hashing.

This adapter requires `agent_id: null`. Supplied parent context establishes
internal consistency, not authenticated historical ownership. Recorded host
identity and first attribution remain immutable. Historical non-null host IDs
stay visible but require an adapter that can actually observe them.

Do not issue another spawn for an unresolved receipt. Reconciliation never
interrupts or controls a worker and does not finish, resume, or requeue its
parent. A completed receipt releases detached capacity; an already blocked
parent remains blocked until a separately approved ordinary requeue. A leased
parent keeps its slot even after wall-clock expiry until explicit lease recovery.

Before prepare, the host verifies that its collaboration spawn, wait, and list
capabilities and the configured role profile are available. It first obtains a
read-only `delegation plan` and keeps that brief only in host memory, comparing
its plan and brief digests with prepare. A returned `invoke-once-now` is a
single-use launch directive within the already approved attempt. Recheck that
the attempt is live and the runtime is not stopped before launch. Record the
exact returned canonical task name immediately; the opaque host ID is optional.
An exact prepare retry is reconciliation-only. Inspect the host tree by the
stored deterministic task name after an uncertain call and do not relaunch.
An unfinished receipt holds capacity even after its parent attempt is no longer
leased. A live parent and child count once; a detached unfinished child counts
once until its terminal receipt. Claim, explain, scheduled-claim, recovery, and
requeue use that same occupancy, so an unresolved host run cannot be bypassed.
The finish command reads at most 64 KiB of valid UTF-8 from standard input,
stores its exact-byte SHA-256 digest, and discards the result. Host usage is
`unavailable` unless the host provides measured input and output tokens.
Receipts are local observations, never host authentication or work authority.

### Project specialist and skill routes

Enabled packs opt their catalog roles in by default. A project-owned Codex
agent in `.codex/agents/<role>.toml` is also opted in automatically: Tasktra
uses its `description` as a concise selection hint in generated `AGENTS.md`
and `CLAUDE.md`. The file needs a matching `name` and a useful description.
That is the normal setup: enable the pack or add the project agent file once,
then the coordinator selects a matching agent for suitable work without a
per-task route or a request that names the agent.
Tasktra does not take ownership of the file or change its model and effort.
Set custom-agent model and effort in that file; `[agents.codex.roles]` applies
to catalog roles only. The coordinator selects a matching specialist for
substantive bounded work when delegation is permitted; a simple lookup stays
direct. Other hosts must check for an equivalent callable agent.

Add explicit routes to `.tasktra/project.toml` only when the agent's
description is insufficient, such as distinct boundaries for a concept draft,
an art critique, and approved art integration, or a skill that should accompany
a specialist:

```toml
[[routing.routes]]
id = "art-concept"
trigger = "Create or revise character artwork, including a draft for review"
role = "graphic-designer"
skills = ["imagegen"]
boundary = "Return a draft for review; do not register or publish it."

[[routing.routes]]
id = "art-critique"
trigger = "Critique existing character artwork"
role = "graphic-designer"
boundary = "Return findings without generation or edits."
```

Each route needs a distinct `id` and `trigger`, plus a `role`, `roles`, one or
more `skills`, or a role/skill combination. Use `roles = ["tester",
"implementer"]` when distinct bounded subtasks may need different specialists;
this is selection guidance, not an instruction to spawn all of them.
`boundary` states the requested output and effect limit.
Routes may refer to roles in enabled packs or to an existing project-owned
Codex agent. A catalog role or skill from a disabled pack is an error. Names
of skills outside the catalog, such as personal or plugin skills, are kept as
host-unverified references: the coordinator checks availability and the
skill's actual trigger, including explicit-only triggers, in the active
session. A skill-only route does not request agent delegation.

Compile and inspect the generated entrypoints after changing routes. The
compiler rejects invalid custom agents and duplicate route IDs or exact
triggers. Overlapping natural-language triggers still need project review.
The selection guidance is not a deterministic dispatch engine;
user instructions, runtime delegation limits, and the existing goal authority
remain controlling. If a matching role, model, skill, or tool is unavailable,
the coordinator must state the limitation and accurately report the fallback
it used. Keep direct lookups and trivial mechanical changes direct.
The `delegation plan` command intentionally resolves an explicit primary signal
to a core role for a reproducible brief. The generated host instructions handle
matching project agents by description during normal conversational work.

## Durable goals and manual fallback

The core recovery loop is:

```powershell
python -m tasktra goal --root . show <goal-id>
python -m tasktra status --root .
python -m tasktra work --root . recover --goal-id <goal-id>
python -m tasktra audit --root . verify
```

Recover only expired leases. Re-read the exact envelope, next checkpoint, scope, budget, and approval before claiming existing work. Generate the lease token in a process environment variable; never put it in a prompt, file, log, or command argument.

## Explain work selection

Before dispatch, inspect what the queue would select for the intended worker:

```powershell
python -m tasktra work --root . explain <goal-id> `
  --actor <worker-id> --envelope-sha256 <digest> `
  --lease-seconds 300 --token-reservation 0 --limit 20 --offset 0
```

The JSON `explanation.selected_work_unit_id` is the next candidate across the
whole goal in lexical ID order. Pagination changes only the displayed
`candidates`. Add `--work-unit-id <id>` to inspect a particular unit while
retaining the true next candidate. An unknown unit or one belonging to another
goal is an error. Limits are 1–100 units per page and offsets 0–1,000,000.

Use the same actor, envelope digest, requested lease duration, and token
reservation as the eventual claim. The explanation and claim share selection
rules; intervening changes or time boundaries can change the result. Every
claim rechecks the ledger under its write transaction. A successful explanation
does not grant approval or reserve work.

Both `goal` and each displayed candidate have `eligible` and `reason_codes`.
Common codes explain the following conditions:

| Reason code | Meaning |
| --- | --- |
| `goal.lifecycle_not_active` | The goal is not active. |
| `goal.contract_missing`, `goal.budgets_missing` | Execution setup is incomplete. |
| `queue.no_claimable_work` | No unit can currently be selected; inspect candidate reasons or add scoped work. |
| `runtime.emergency_stopped` | The runtime is stopped. |
| `goal.intake_draining` | Intake is closed while existing leases finish or recover. |
| `goal.dependencies_incomplete` | A prerequisite goal has not completed. |
| `goal.checkpoints_complete` | No contract checkpoint remains to select work for. |
| `budget.concurrency_exhausted` | Held leases occupy all available slots. |
| `budget.attempts_exhausted`, `budget.tokens_exhausted`, `budget.elapsed_exhausted` | The attempt limit is reached or the requested reservation cannot fit. |
| `candidate.lease_held` | The unit has an unreleased attempt. |
| `candidate.status_not_claimable` | The unit's lifecycle state does not allow a claim. |
| `candidate.retry_wait` | The retry time has not arrived. |
| `candidate.checkpoint_not_current` | The unit does not belong to the next checkpoint. |
| `candidate.prerequisites_incomplete` | At least one prerequisite work unit has not completed. |
| `candidate.attempts_exhausted` | The unit has used its attempt allowance. |
| `approval.unavailable` | No usable approval covers this worker, action, effect, envelope, and unit scope. |
| `authorization.envelope_mismatch`, `authorization.action_not_allowed`, `authorization.effect_not_allowed`, `authorization.scope_outside_envelope` | The claim does not fit the current envelope. |

Expired leases still hold concurrency, tokens, and elapsed-time reservations
until explicit recovery. The explanation reports these gates without recovering
leases, marking exhausted units, or writing audit events. It verifies the audit
chain and sealed current state in one read snapshot and rejects damaged or
incompatible state. It omits lease secrets, approval payloads, titles, and scopes.
Approved records require an expiry; malformed legacy records without one are
unavailable for claims, consistent with the approval validator and other
transition checks.

## Coordinate work prerequisites

Define work in prerequisite order. Each unit can declare up to 64 existing
prerequisites in the same goal:

```powershell
python -m tasktra work --root . create <goal-id> "Build service" `
  --id service --scope service-scope.json
python -m tasktra work --root . create <goal-id> "Check integration" `
  --id integration --scope integration-scope.json --depends-on service
python -m tasktra work --root . dependencies <goal-id> --limit 20 --offset 0
python -m tasktra work --root . dependencies <goal-id> --work-unit-id integration
```

Repeat `--depends-on` for joins that need several units to finish. Multiple units
can share a prerequisite and become ready for parallel claims after it completes.
Failed, blocked, stopped, or leased work does not satisfy a prerequisite. All
other claim gates, including approvals and available concurrency, still apply.

Prerequisites are fixed when the unit is created. Missing, duplicate, self,
cross-goal, cyclic, or later-checkpoint references are rejected atomically.
Units in checkpointed goals still need `--checkpoint`; their prerequisites may
be in the same or an earlier checkpoint.

`work dependencies` reads one verified ledger snapshot. Its `ready` field means
only that all prerequisites are complete. It does not indicate claim authority
or reserve work. The result contains direct prerequisite IDs and statuses,
paginates units at 1–100 per page, and avoids recursive expansion.

Scheduled previews reject a blocked target, and exact scheduled claims recheck
its prerequisites. Existing work migrates with no prerequisites. Runtime schema
11 adds the authoritative edge table through the existing backup and integrity
mechanisms. For a self-hosted upgrade, validate a copied runtime first, finish
schema-10 attempts before cutover, then generate and authorize a fresh exact
upgrade preview. Keep active leases at zero during that migration.

The authority and scheduling rationale is recorded in
[ADR 0010](adr/0010-immutable-work-prerequisites.md).

## Load a work plan atomically

Define the goal and its authority contract first, then save a manifest such as
`plan.json`. The goal must be planned or active when new units are added.

```json
{
  "kind": "tasktra.work-plan",
  "version": 1,
  "goal_id": "delivery",
  "units": [
    {
      "id": "integration",
      "title": "Verify the integrated result",
      "scope": {"paths": ["tests"], "exclusions": []},
      "prerequisite_ids": ["service", "interface"]
    },
    {
      "id": "service",
      "title": "Build the service",
      "scope": {"paths": ["src/service"], "exclusions": []}
    },
    {
      "id": "interface",
      "title": "Build the interface",
      "scope": {"paths": ["src/interface"], "exclusions": []}
    }
  ]
}
```

```powershell
python -m tasktra work --root . plan-preview plan.json
python -m tasktra work --root . plan-apply plan.json --preview-sha256 <preview-sha256>
```

Preview is read-only. Its `plan` result contains canonical definitions, create
and unchanged counts, structural dependency waves, a stable `manifest_sha256`,
and the state-bound `preview_sha256` required by apply. Unit order does not
affect the manifest identity. Dependencies may refer forward within the manifest
or to existing units in the same goal. Cycles, missing or cross-goal references,
later-checkpoint dependencies, out-of-scope definitions, and conflicting
existing units are rejected before any unit is created.

For a goal with checkpoints, every unit needs `checkpoint_id` from the contract.
New units cannot be added to a reached checkpoint. Existing exact definitions
remain unchanged, including their current execution state. Scope and checkpoint
constraints still apply to those definitions under the current contract.

Apply checks the exact preview against the current database target, contract,
immutable definitions, edges, and creation gates inside one transaction. A
relevant change makes the preview stale; review a fresh preview before retrying.
Successful application creates all new units, edges, and audit evidence together.
After success, previewing and applying the same manifest again produces an
unchanged-only operation with no new rows or events. The original preview digest
does not serve as a reusable execution receipt.

Input is limited to 64 KiB, 256 units, 64 prerequisites per unit, and 4,096 total
edges. Each unit has a nonempty title of at most 240 characters and an explicit
closed scope. Unknown fields and duplicate JSON keys are rejected. Structured
errors include `error_code` and bounded `details` for correction.

Loading definitions never activates a goal, grants approvals, claims work, or
dispatches agents. Dependency waves describe graph structure; they do not imply
that a worker may execute a unit. Use `work dependencies` and `work explain` to
inspect the resulting graph and the existing execution gates. See
[ADR 0012](adr/0012-atomic-work-plan-loading.md).

## Trace dependency impact

Inspect the work around one unit from a verified, read-only ledger snapshot:

```powershell
python -m tasktra work --root . impact <goal-id> <work-unit-id>
python -m tasktra work --root . impact <goal-id> <work-unit-id> --direction prerequisites --limit 20 --offset 0
python -m tasktra work --root . impact <goal-id> <work-unit-id> --direction dependents --limit 20 --offset 0
```

The `anchor` describes the selected unit's status and direct prerequisite check.
The `summary` counts its direct and transitive prerequisites, incomplete
prerequisite ancestors, and direct and transitive dependents. Each relation
shows the minimum number of dependency edges between it and the selected unit.
Repeated paths to the same unit count once.

`direct_prerequisite_gates_cleared_if_completed` counts direct dependents whose
prerequisite check would change from false to true if the selected unit became
complete. Other prerequisites must already be complete. Transitive dependents
do not count, and an already complete anchor always produces zero. This is a
structural count even when a dependent's own status prevents execution; inspect
its displayed status and use `work explain` for the full claim checks.

`claimability_evaluated` is always false. The report grants no approvals,
reserves no work, and changes no lifecycle state. Titles, scopes, credentials,
and approval records are excluded from the output.

The default `both` direction lists prerequisites first, then dependents. Within
each direction, rows sort by minimum distance and then identifier. `--limit`
accepts 1–100 and `--offset` accepts 0–1,000,000. Summary counts cover both complete
relation sets regardless of direction or page; `total` and `next_offset` describe
only the selected detail set. See [ADR 0013](adr/0013-anchored-dependency-impact.md).

## Map goal readiness

Use the readiness map to inspect a goal's remaining dependency structure and
its separately reported operator gates without selecting, claiming, or changing
work:

```powershell
python -m tasktra work --root . readiness <goal-id> --limit 20 --offset 0
```

The report is a read-only, verified snapshot. `structural_ready` means that an
incomplete unit has no incomplete direct prerequisite. It is not claimability,
priority, authorization, or a schedule. In particular, a ready unit can still
be held by a lease, be at a later checkpoint, be paused or terminal-attention,
or fail lifecycle, capacity, budget, approval, envelope, scope, retry, or
prospective reservation checks. Run the rooted `work explain` template returned
by the report after substituting the literal placeholders for the intended
performer, envelope digest, lease duration, and token reservation.

The response independently applies `--limit` and `--offset` to its three page
objects: `remaining_structure.waves`, `frontiers.ready_frontier`, and
`frontiers.blocking_frontier`. Their `total` and `next_offset` values therefore
advance independently even though they share the requested window. Waves start
at zero for the residual structural frontier. `maximum_structural_depth` is the
longest path of incomplete units and is graph shape only; it is not elapsed
time, an ETA, probability, priority, or a scheduling critical path.

The blocking page highlights upstream work with incomplete dependents and every
terminal-attention unit, including a terminal leaf. Its
`direct_prerequisite_gates_cleared_if_completed` value is the same anchored
dependency-impact metric exposed by `work impact`: it credits every direct
dependent for which this is the sole incomplete prerequisite, even when that
dependent is already complete, leased, failed, or blocked. Use
`remaining_direct_prerequisite_gates_cleared_if_completed` for the subset that
would make incomplete dependent work structurally ready.

`operational_gates` intentionally keeps a goal's lifecycle, goal dependencies,
checkpoints, budgets, capacity, emergency stop, interventions, unresolved runs,
and authority-contract presence separate from graph readiness. A later
checkpoint may be structurally ready while `candidate.checkpoint_not_current`
appears on its row. Capacity includes stored leased attempts and detached
unresolved runs; budget observations do not make a claim decision.

For a concrete operator journey, first inspect the map, then use a returned
unit's rooted `dependencies_argv` or `impact_argv` to inspect its local graph.
If it has an intervention or unresolved-run signal, execute the corresponding
rooted inspection argv. Finally, replace the placeholders in that unit's
`explain_argv_template` and run it with the actual prospective claim inputs:

```powershell
python -m tasktra work --root . explain <goal-id> `
  --actor <performer-id> --envelope-sha256 <current-envelope-digest> `
  --lease-seconds 300 --token-reservation 0 --work-unit-id <work-unit-id> `
  --limit 1 --offset 0
```

The readiness report's argv arrays are token arrays, not shell strings. The CLI
binds each displayed `work`, `intervention`, and `delegation` drilldown to the
exact resolved `--root` supplied to the readiness command and preserves literal
placeholder tokens for the caller to substitute. The service-level report
remains root-neutral. All drilldowns are inspections until an explicit claim or
other transition command is issued.

## Inspect a work unit

Select a `goal_id` and `work_unit_id` from an unchanged `work readiness`
frontier row, then inspect that pair with one rooted command:

```powershell
python -m tasktra work --root . inspect <goal-id> <work-unit-id> --limit 20
```

The result is one verified, read-only snapshot of the selected unit's current
structural and operational context, attempts, curated activity, and allowlisted
evidence identities. It neither evaluates authority or claimability nor changes
the ledger. `capture.audit_head` is provenance for the observed transaction. It
does not reserve state, authorize a transition, or make a later command reject a
stale capture; existing commands open their own transaction and apply their own
guards.

`--limit` applies independently to the attempts and activity pages and accepts
integers from 1 through 50. `--before-attempt-no` and `--before-sequence` are
optional exclusive positive keyset cursors. Each returns strictly older rows in
newest-first order; a cursor beyond the captured head is valid and gives the
first page. Use each page's `next_before_attempt_no` or
`next_before_sequence` only when its `has_more` value is true. These bounds
limit the response; they do not turn mutable current attempt state into an
immutable historical record.

The inspection intentionally omits work titles and scopes, lease tokens and
their hashes, raw repository context, workflow documents, intervention prose,
Codex result and host content, provider requests and evidence, and arbitrary
audit payloads. SHA-256 values are persisted-content identities for comparison
and existing detail commands. They do not establish content safety, authority,
or freshness.

Each evidence family shows a bounded recent window. `redacted_items` counts
omitted candidates within that window; it does not count every historical item.
Provider effects that have crossed work-attempt bindings are omitted because
the current binding alone cannot establish their privacy lineage. Their keys
and dependent command templates are omitted too. The explicit effect detail
command remains a separate inspection surface with broader content.

`action_candidates` contain only rooted read commands and narrowly guarded
templates. A structured `work requeue` template appears only when this unit has
a safe current structured intervention with a current answered response head and
no closure. It carries that exact request ID, response ID, and response digest;
the operator must still supply an actor, the applicable envelope and approval
scope, and nonempty evidence. The existing requeue transaction rechecks the
unit, goal, intervention, closure, authorization, and exact response head; a
historical retry may be an idempotent no-op. A legacy unit-only requeue is never
proposed.

A `delegation reconcile` template appears only for a safe unresolved run of the
selected unit whose state permits an observation. It binds the immutable run ID;
the existing reconciliation command applies its established result-transport,
idempotent no-op, and conflict rules when it reads the new observation. Provider
effects may offer their rooted read-only inspection and a deferred observation,
but never a provider-reconcile template because that command selects its latest
dispatch internally and has no exact effect-attempt input. Claim, goal recovery,
intervention response, legacy requeue, and provider reconciliation are likewise
omitted as broad mutation templates.

## Scheduling

Tasktra previews scheduler-neutral resume instructions but never creates or manages a schedule:

```powershell
python -m tasktra schedule --root . preview `
  --goal-id <goal-id> `
  --work-unit-id <work-unit-id> `
  --envelope-sha256 <digest> `
  --checkpoint <checkpoint> `
  --performer-id <worker-id> `
  --repository <repository> `
  --revision <revision> `
  --branch <branch> `
  --workspace <workspace> `
  --cadence "weekdays 09:00" `
  --notification-intent on-failure
```

The preview reports Codex scheduled tasks, CI, a local runner, and manual operation as explicit capabilities. Unavailable scheduling never blocks eligible local work. Every future run must recover expired leases, re-read the ledger, match the goal/work/envelope/checkpoint/budget reference, and use a fresh unprinted token. Notification intent does not authorize external communication.

Codex scheduled tasks are configured in the ChatGPT desktop or web experience, not through the CLI. For local projects, keep the machine and desktop app available, prefer an isolated worktree for mutating work, keep the prompt durable, and use the narrowest sandbox and network permissions. Scheduled runs are unattended and cannot self-approve. See the [official OpenAI scheduled-tasks documentation](https://learn.chatgpt.com/docs/automations).

## Validation and CI

Preview configured commands, then execute them directly without a shell:

```powershell
python -m tasktra validate --root .
python -m tasktra validate --root . --run
python -m tasktra compile --root . --check --trust-catalog
python -m tasktra audit --root . verify
```

The committed CI definition covers Windows, macOS, and Linux with Python 3.11–3.13. A workflow definition is not execution evidence: release approval requires authoritative successful runs for every supported platform.

## Optional providers and offline work

GitHub, Jira, research, and scheduling integrations are optional. `tasktra capabilities` reports each one as available, degraded, or unavailable without granting authority or blocking unrelated local work. Do not store credentials in Tasktra configuration, prompts, telemetry, or evidence. Reconcile an indeterminate provider effect before retrying it. An unavailable online capability can leave only its own operation pending; it never prevents eligible local planning, coding, validation, evidence capture, or recovery.

## Upgrades, backup, and recovery

Use one exact preview and its digest:

```powershell
python -m tasktra upgrade --root . preview
```

Apply or roll back only through the authority-gated commands printed by the preview. Retain snapshot and receipt files. If the runtime schema changes, Tasktra disables automatic file rollback and reports the exact database backup and SHA-256. Stop active work and obtain a human recovery decision before restoring that backup.

Upgrade snapshot identity includes the complete lifecycle plan digest. Separate
runtime-only upgrades therefore retain separate snapshots even when their pack
changes and managed file paths are identical. Existing snapshots and plain pack
migration digests remain readable. Reusing the same bound plan against a
different pre-mutation state still fails; never delete an earlier snapshot to
make a retry succeed.

If configured checks fail before migration, the snapshot is restored and the
failed upgrade receipt can be recorded against the retained runtime version.
This compatibility path accepts only a failure for the existing exact local
upgrade intent and its bound performer. Retry with a fresh reviewed preview,
approval, and idempotency key; preserve the earlier failure evidence.

Runtime upgrades also retain a prepared migration journal before committing the
database change. Keep this journal even if an interruption prevents the final
receipt from being written. Prepared evidence identifies the exact backup; it
does not assert that the database committed. Recovery checks the on-disk schema
before permitting file rollback and fails closed if that schema is unavailable.
When recovery is required, CLI error output includes a structured `recovery`
object with the available backup path and SHA-256.

Runtime schema 15 combines two earlier migration histories: verification policy
and completion evidence from schemas 11–12, and dependencies, draining,
interventions, and Codex receipts from schemas 11–14. The upgrade verifies the
original schema shape and sealed records, preserves the backup, and adds the
missing fields and tables. Use the normal upgrade preview; changing SQLite's
version number manually cannot convert one history into the other.

Explicitly trusted executable packs are full-host code execution. Checksums, environment scrubbing, and declared effects improve reviewability but do not create an operating-system sandbox.

For a general runtime backup, first prevent new work and verify the ledger:

```powershell
python -m tasktra runtime --root . emergency-stop --actor <human-operator> --reason "consistent backup"
python -m tasktra audit --root . verify
```

Then use an SQLite-aware backup tool against the database path in `.tasktra/project.toml`; do not copy only a live `.sqlite` file because committed data can still be in its WAL. With the standard Python library, this direct-argument snippet creates a consistent project-local backup:

```powershell
python -c "import sqlite3; s=sqlite3.connect(r'.tasktra/runtime/tasktra.sqlite'); d=sqlite3.connect(r'.tasktra/backups/tasktra.sqlite'); s.backup(d); d.close(); s.close()"
python -c "import sqlite3; c=sqlite3.connect(r'.tasktra/backups/tasktra.sqlite'); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; c.close()"
```

Record a SHA-256 with the backup and store it outside the working copy under the project's normal backup policy. To restore, keep the emergency stop active, preserve the failed database, verify both the recorded digest and `PRAGMA integrity_check`, restore to the configured contained path while no Tasktra process is running, then run `doctor` and `audit verify` before a human clears the stop. Never infer a valid restore from file existence alone.

## Telemetry and privacy

Telemetry is off by default. Status is read-only:

```powershell
python -m tasktra telemetry --root . status
python -m tasktra telemetry --root . export .tasktra/evidence/telemetry-export.json
```

Collection accepts only the closed local metadata schema. It excludes prompts, source content, credentials, arbitrary labels, and secrets. Export is a separate explicit action and remains project-local until a separately authorized external transfer occurs.

## Drain a goal gracefully

```powershell
python -m tasktra goal --root . drain <goal-id> --preview --limit 20 --offset 0
python -m tasktra goal --root . drain <goal-id> --apply --actor <operator-id>
python -m tasktra goal --root . list --status draining
python -m tasktra overview --root . --goal-id <goal-id>
```

Preview is read-only. It reports the resulting status, stored/live/expired lease
counts, a bounded page of attempt and work-unit identifiers, expiry and state,
and a digest of the full lease set. Apply observes current leases inside its
write transaction; a previous preview does not reserve them. Reapplying drain
is idempotent.

Draining blocks new generic and scheduled claims while preserving leases,
owners, tokens, expiry, and reservations. Existing workers can heartbeat and
finish under normal checks. Live lease-bound provider operations may continue;
unleased effect preparation still requires an active goal. An empty goal pauses
immediately. Otherwise the last finish or expired-lease recovery atomically
changes `draining` to `paused`.

Expired leases count until normal recovery releases their reservations. Draining
has no deadline and valid workers may extend leases. Use immediate pause or stop
if waiting is no longer appropriate. Existing human-approved `goal resume`
cancels draining without replacing leases, or reactivates a fully paused goal.
Schedule preview remains available, but execution requires an active goal.

Overview reports `goal-draining` attention and `intake.accepting_claims` /
`intake.draining`. These describe the lifecycle gate; they do not establish work
readiness or approval. `work explain` reports `goal.intake_draining`.

This feature requires runtime schema 12. Upgrade schema 11 through the normal
exact preview and approved application so the database backup, integrity check,
and lock update are retained. Older clients refuse writes to schema 12 even
though its tables are unchanged; they cannot safely finalize the new lifecycle
state. See [ADR 0011](adr/0011-graceful-goal-draining.md).

### Agent execution and usage

`tasktra execution` is a separate, project-local ledger for individual agent work.
Creating a work record opts that record in; it does not backfill historical
sessions or turn on the older aggregate telemetry store. The configured role,
model, and effort come from an enabled catalog role or an opted-in project
agent file. A requested override is recorded separately. The report labels
manual start and finish entries as assertions; a host callback or a matching
Codex rollout supplies stronger execution evidence.

```powershell
python -m tasktra execution --root . plan work-1 --role scout
python -m tasktra execution --root . start work-1 --host local --thread-id <host-thread-id> --agent-id /root/scout
python -m tasktra execution --root . finish work-1 --outcome succeeded --rollout <local-rollout.jsonl>
python -m tasktra execution --root . report
```

When a host provides lifecycle callbacks, its adapter can record the start and
finish receipts and import the named rollout at completion. The CLI is the
explicit fallback for hosts without such a callback; its entries remain
manual assertions unless a rollout verifies the matching thread. Tasktra's
Python control plane cannot observe an arbitrary native Codex subagent spawn
by itself. If usage is unavailable, finish with
`--unknown-reason host-no-usage` (or another short reason code) and leave token
counters null. A final rollout import can refresh a completed record without
double counting responses. Scope reused
sessions by turn; one whole-thread record cannot overlap records for its
individual turns. Coordinator work needs its own record and attribution reason.

The project-local database is `.tasktra/runtime/agent-execution.sqlite`, separate from
the canonical workflow database and aggregate telemetry. It stores counters,
identifiers, profile provenance, and a source digest, not raw rollout content
or the rollout path. Do not add the project-local database, its WAL, or raw host logs
to version control or an export. The project-local database uses the project's
filesystem permissions and is not encrypted; limit access to its runtime directory when
host, thread, or agent identifiers are sensitive. Usage totals describe measured work only;
cached and reasoning counters are subsets, and no token savings or quality
effect is inferred from them. The record outcome does not replace configured
validation evidence.

## Stop and incident response

Use these exact controls; each preserves durable evidence:

```powershell
python -m tasktra goal --root . pause <goal-id> --actor <human-operator>
python -m tasktra goal --root . stop <goal-id> --actor <human-operator>
python -m tasktra runtime --root . emergency-stop --actor <human-operator> --reason "incident description"
python -m tasktra audit --root . verify
python -m tasktra runtime --root . clear-emergency-stop --actor <human-operator> --actor-kind human
```

Pause is a planned interruption, stop is a recoverable terminal goal decision, and emergency stop prevents new work across the runtime. Clear an emergency stop only after a human has reviewed the cause, audit, leases, and recovery evidence. Do not delete the runtime database or hand-edit audit records. Preserve the database, versioned migration backups, snapshots, receipts, audit export, and the exact failing command.

Escalate when scope or effect authority is missing, a checkpoint needs a human decision, a provider result is indeterminate, recovery evidence is incomplete, audit integrity fails, or platform/release proof is unavailable.

See [platform behavior and fail-closed fallbacks](PLATFORM_NOTES.md) for link, reparse-point, process-tree, locking, path, shell, and CI evidence expectations.
