# Tasktra

Tasktra is a Codex-first, runtime-neutral workflow orchestration template for software projects. It supplies durable goals, explicit authority, compact evidence handoffs, specialist roles, and deterministic project tooling so a project can be used immediately with local-only defaults or tailored over time.

Tasktra is designed for trustworthy outcomes with efficient total agent usage: reuse verified evidence, prefer deterministic checks, keep worker briefs small, and spend stronger reasoning only where judgment requires it.

## Status

Tasktra is being built in staged vertical slices. The agreed product contract is in the [specification](docs/SPECIFICATION.md), and the delivery sequence is in the [roadmap](docs/ROADMAP.md). Stages 1–6 are complete; Stage 7 operations and the 1.0 release gate are active.

## Intended use

Use Tasktra as either:

- a generic, ready-to-adopt project template; or
- a versioned foundation that a project initializes, configures with packs and extensions, and upgrades through previewable migrations.

Codex is the normal human interface. A small Python CLI provides deterministic operations that Codex can invoke and that remain available for automation and diagnostics. In Codex, an explicit current user approval can be recorded as a content-bound `codex-user-message` approval for the exact action, scope, performer, and authority-envelope hash. It does not let an agent reuse a plan, its own output, or a prior approval as fresh authority; terminal-only users can continue to use the local confirmation ceremony.

## Product principles

- Correctness, safety, and required validation come before token efficiency.
- Human-approved goals bound autonomous work; no artifact or worker output can grant new authority.
- Local work remains viable without cloud accounts or remote services.
- Generated runtime projections are derived from canonical sources and are not hand-edited.
- Project-owned customizations are preserved during initialization and upgrades.
- Runtime evidence is compact, attributable, and retained only as long as policy requires.

## Quick start

From a fresh Tasktra source checkout, install the local package and bootstrap
the ignored runtime database. This preserves the committed project profile; do
not run `init --apply` against this repository checkout.

```powershell
python -m pip install --no-deps -e .
python -m tasktra bootstrap --root .
python -m tasktra doctor --root .
python -m tasktra compile --root . --check --trust-catalog
python -m unittest discover -s tests -v
```

That path is for developing Tasktra itself. The same editable source install can
orchestrate another repository; install it once into the Python environment that
will run Codex's commands, then point Tasktra at the target from any directory:

```powershell
python -m pip install --no-deps -e C:\src\Tasktra
python -m tasktra init --root C:\src\my-project
python -m tasktra init --root C:\src\my-project --apply
```

A built wheel can be installed instead of the editable source checkout when a
versioned distribution is available.

### First project walkthrough

After `tasktra init --apply`, run the built-in guide. It is written for people
using the terminal and describes the next useful actions for the current
project without changing anything:

```powershell
tasktra guide
```

For a new project, the usual path is:

```powershell
tasktra init --preview
tasktra init --apply
tasktra guide
tasktra packs recommend
tasktra packs add planning
tasktra packs add planning --apply
tasktra compile --trust-catalog
```

`packs add` and `packs remove` are preview-first. Their normal form explains
the configuration change, dependencies, capability blockers, and migrations;
only `--apply` updates `.tasktra/project.toml`. Compiling remains a separate
step, so Tasktra never silently replaces generated agent instructions.

```powershell
tasktra packs list
tasktra packs add jira-sync
tasktra packs remove jira-sync --apply
```

Use `tasktra guide --json` when a Codex skill or another tool needs the same
walkthrough as structured data.

### Understand project progress

Use the read-only overview to see goals, progress, remaining budgets, leases,
and work needing attention, then drill into an individual goal:

```powershell
tasktra overview
tasktra overview --goal-id <goal-id>
tasktra overview --json --limit 20 --offset 0
```

The default is a terminal summary; `--json` supplies structured data for agents
and other tools. Pagination applies to goals, or to work units when a goal is
selected. Recommendations are diagnostic guidance; existing authority checks
still govern every transition. See the [operations guide](docs/OPERATIONS.md)
and [orchestration development plan](docs/plans/orchestrator-evolution.md).

Export an offline dashboard to inspect the same project from portfolio attention
through individual work units:

```powershell
tasktra cockpit export tasktra-dashboard.html
```

Open the new HTML file in a browser. It includes a dated snapshot, goal and work
filters, dependency impact, and copyable read-only diagnostic commands. It works
without a server or network connection. Existing output files are never replaced;
use a new filename for each capture. See the
[dashboard guide](docs/OPERATIONS.md#export-an-offline-operator-dashboard).

Use `tasktra intervention list` to inspect requests left by workers that yielded
for input, including their current response state and direct downstream impact.
Responses preserve correction history; a separate authorized requeue binds to
the exact answer the operator reviewed. See the
[intervention guide](docs/OPERATIONS.md#operator-intervention-requests) for the
schema upgrade and request, response, and requeue workflow.

`tasktra doctor` also identifies the running Python interpreter, Tasktra package
location, version, and supported database schema. In a Tasktra source checkout
it flags a package loaded from another source checkout, which can happen when an
editable install points to a different worktree.

Inspect what can run next for a specific worker with:

```powershell
tasktra work explain <goal-id> --actor <worker-id> --envelope-sha256 <digest>
```

The JSON result names the next candidate and gives coded reasons for work that
is waiting. Add `--work-unit-id <id>` to inspect one unit or `--limit 20
--offset 0` to page through the queue. Selection still considers the whole goal.
This reads the ledger without claiming work; a later claim rechecks its state
and authority. See [work selection](docs/OPERATIONS.md#explain-work-selection)
for the reason codes and budget options.

Coordinate parallel work with prerequisites defined at creation:

```powershell
tasktra work create <goal-id> "Check integration" --id integration --scope scope.json --depends-on service
tasktra work dependencies <goal-id>
```

Repeat `--depends-on` when a unit needs several prerequisites. Each must complete
before the dependent unit can be claimed. The dependency view shows direct
relationships and readiness; existing approval and budget checks still govern
execution. See [work prerequisites](docs/OPERATIONS.md#coordinate-work-prerequisites).

Load a whole work plan with a preview and one atomic application:

```powershell
tasktra work plan-preview plan.json
tasktra work plan-apply plan.json --preview-sha256 <digest-from-preview>
```

The manifest may list prerequisites after their dependents. Preview validates
the whole graph and reports which definitions are new or unchanged. Conflicts
prevent all creation; execution still needs its existing approvals. See
[work-plan loading](docs/OPERATIONS.md#load-a-work-plan-atomically) for the format
and the fresh-preview retry workflow.

Trace why a unit is waiting and what depends on it:

```powershell
tasktra work impact <goal-id> <work-unit-id>
tasktra work impact <goal-id> <work-unit-id> --direction dependents --limit 20
```

The report traces prerequisite and dependent chains, counts incomplete blockers,
and shows which direct prerequisite checks would clear if the selected unit
completed. Counts describe dependencies; `work explain` checks whether work may
be claimed. See [dependency impact](docs/OPERATIONS.md#trace-dependency-impact).

Map a goal's remaining dependency shape and operator gates with:

```powershell
tasktra work --root . readiness <goal-id> --limit 20 --offset 0
```

Readiness is structural only: a ready unit has completed direct prerequisites,
but still needs `work explain` to evaluate its actor, envelope, approval, scope,
lease, retry, checkpoint, budget, and capacity gates. The response pages ready
units, blockers, and graph waves independently with the same requested window.
Its remaining depth describes incomplete dependency shape, never a schedule or
ETA. See [goal readiness](docs/OPERATIONS.md#map-goal-readiness) for the
operator journey and rooted drilldowns.

For a selected, unchanged readiness row, inspect its `goal_id` and
`work_unit_id` together in one verified, read-only case file:

```powershell
tasktra work --root . inspect <goal-id> <work-unit-id> --limit 20
```

The report joins the unit's current structural and operational context with
bounded attempt and activity history. It is observational: its capture audit
head identifies what was read, but is not a freshness guard or authority for a
later command. See [inspect a work unit](docs/OPERATIONS.md#inspect-a-work-unit)
for keyset history bounds, privacy limits, and the exact guarded follow-ups.

Close intake while current workers finish:

```powershell
tasktra goal drain <goal-id> --preview
tasktra goal drain <goal-id> --apply --actor <operator-id>
```

The goal becomes `draining` and pauses after its final lease finishes or is
recovered. Existing workers retain their leases; immediate pause remains
available. See [graceful draining](docs/OPERATIONS.md#drain-a-goal-gracefully)
for recovery, resume, and runtime compatibility.

`init` is preview-only unless `--apply` is present and never overwrites an existing profile. `bootstrap` preserves an existing profile and initializes a missing local runtime, updating only its schema entry in an existing lock. An existing older runtime requires an explicit upgrade. `compile` refuses to replace project-owned files, requires `--force` for locally edited managed files, and requires `--prune-stale` before deleting obsolete managed files whose hashes still match the prior manifest. Installing the package is what makes `python -m tasktra` available outside the Tasktra checkout; `--root` identifies the project it should operate on.

The local workflow surface includes preview-first validation, Markdown work items, structured handoffs, durable goals, capability reporting, and read-only workspace guidance:

```powershell
tasktra validate --root .
tasktra validate --root . --run
tasktra work-item --root . create fix-export "Fix account export"
tasktra handoff validate .tasktra/handoffs/example.json
tasktra workspace --root . --change-scope substantial
tasktra capabilities --root .
tasktra schedule --root . preview --goal-id <goal-id> --work-unit-id <work-unit-id> --envelope-sha256 <digest> --performer-id <worker> --repository <repo> --revision <rev> --branch <branch> --workspace <path>
tasktra release --root . audit
tasktra packs --root . recommend
tasktra packs --root . preflight --pack python
tasktra packs --root . migration-preview --pack python
tasktra adopt --root .
tasktra upgrade --root . preview
tasktra telemetry --root . status
tasktra lesson --root . list
```

Configured validation commands execute as direct argument lists, not through a shell. Work-item updates require the current item version, and workspace inspection never creates branches or worktrees. Optional provider health can be supplied by a Codex or connector host for one invocation with `tasktra capabilities --root . --provider-health report.json`; the report is diagnostic and grants no authority.

### Local progress portal

Start a visual, read-only dashboard for an adopted project:

```powershell
python -m tasktra portal --root . --open
```

The portal runs at `http://127.0.0.1:8765/` and shows recorded goals, jobs,
agent activity, token budgets, and recent events. Animated agent stations make
the activity easy to follow. Search jobs, filter by goal, and inspect details
without leaving the dashboard. No web build step or extra dependencies are
required. Use **Explore demo** to view clearly labeled sample data without
changing project records. Press Ctrl+C in the terminal to stop the server.

See the [portal guide](docs/PORTAL.md) for tracking semantics and launch options.

### Execute an approved work unit

`tasktra run` previews a bounded Codex CLI workflow for an existing active goal
and work unit. Add `--apply` to claim that exact unit, run fresh agent sessions,
execute configured validation, and retain execution receipts and workflow evidence.
It uses the project's configured role models and existing transition approvals.

```powershell
tasktra run --root . --goal-id <goal> --work-unit-id <unit> --actor <performer> --envelope-sha256 <digest>
tasktra run --root . --goal-id <goal> --work-unit-id <unit> --actor <performer> --envelope-sha256 <digest> --token-reservation 150000 --timeout 900 --apply
```

The initial adapter requires a clean Git checkout, whole-workspace unit scope,
and an installed, authenticated Codex CLI. It executes one unit per invocation;
it does not create approvals or mark the overall goal complete. See the
[execution guide](docs/EXECUTION.md) for verification policies, cancellation,
budget accounting, and recovery.

### Optional Jira status synchronization

Jira is not part of the default project context. A project that wants one-way Tasktra-to-Jira status updates can explicitly enable the `jira-sync` pack and add a policy such as:

```toml
[packs]
enabled = ["core", "jira-sync"]

[jira_sync]
host = "acme.atlassian.net"
project = "PROJ"
claim_transition = "In Progress"
review_transition = "In Review"
complete_transition = "Done"
```

Then `tasktra jira-sync --root . plan --event claimed --issue PROJ-123 --goal-id <goal> --work-unit-id <unit>` builds a safe request. It does not contact Jira; an active approval, lease, and configured host executor are still required to apply the transition.

### Optional discovery planning

For a new project idea or a major feature, enable the `planning` pack and invoke `/tasktra-discovery` in Codex. It asks the product and architecture questions that change the implementation plan, then writes a reviewable, project-owned plan at `docs/plans/<plan-id>.md`.

```toml
[packs]
enabled = ["core", "planning"]
```

Discovery does not begin implementation. After you approve the plan and authorize work, `/tasktra-goal` turns it into the durable Tasktra goal that workers can execute.

## Deterministic controls and agent work

The CLI is intentionally not a second conversational interface. It is the deterministic control plane used for state transitions, validation, audit verification, compilation, checksums, and reproducible diagnostics. Those operations are cheaper and more reliable as direct code than as a model task, and their output gives agents compact evidence to reuse.

Codex should delegate when the task needs reading, semantic judgment, implementation, or independent review. The default efficiency ladder is: reuse verified evidence; use deterministic tools; send a narrow lower-cost scout when investigation is needed; assign a bounded implementer for a concrete change; then use a stronger reviewer or escalation role only for a real unresolved difficulty. Required validation is never skipped to save tokens.

## Portable model routing

Canonical roles declare a portable tier (`fast`, `balanced`, `deep`, or `exceptional`), a reasoning effort, and a sandbox mode. The built-in Codex mapping is `fast` → `gpt-5.6-luna`, `balanced` → `gpt-5.6-terra`, `deep` → `gpt-5.6-sol`, and `exceptional` → `gpt-6-astra`. Generated `.codex/agents/*.toml` files contain the resolved native settings; edit catalog sources or project configuration, then compile, rather than editing a generated agent.

A consuming project can replace selected tier mappings and set a role-specific model or reasoning effort in `[agents.codex]`. Resolution is role override first, then the project's tier mapping, then the catalog default. A role cannot widen the sandbox declared by the canonical catalog. See the [operations guide](docs/OPERATIONS.md#model-routing-and-delegation-plans) for configuration and the distinction between a local routing plan and actual Codex-host dispatch.

For an approved leased attempt, Tasktra can record a bounded Codex-host execution receipt. `tasktra delegation prepare` resolves the role and creates one launch directive, while the Codex host performs the single actual child dispatch. Record the returned canonical task name with `delegation start`, then send the final UTF-8 result to `delegation finish --result-stdin`; only its SHA-256 digest is retained. An unfinished receipt continues to hold capacity after its parent lease ends, so it cannot be bypassed by recovery or requeue; a live parent and child use one slot, while a detached child uses one until its terminal receipt. `delegation show` and `list` expose receipt projections, including unavailable host usage, without storing the brief, raw result, or lease token.

If a receipt is unresolved, `tasktra delegation unresolved` reports the retained run and its observed state without changing it. Capture one current `collaboration.list_agents` tree, normalize only the exact running or completed shape into a closed version-1 `tasktra.codex-host-tree-observation` file, and use `tasktra delegation reconcile <run-id> --actor <actor> --observation <file>` once. A completed observation additionally receives the exact final-text bytes through `--result-stdin`; Tasktra retains only their digest. Reconciliation records historical observations only: it does not launch, interrupt, finish the parent attempt, requeue work, or authenticate host/model usage.

Enabled pack roles and project-owned `.codex/agents/*.toml` specialists become default choices for matching substantive work. Enabling a pack or adding a valid project agent file is the opt-in; routine use needs no per-task route or explicit agent name. Projects can add [specialist and skill routes](docs/OPERATIONS.md#project-specialist-and-skill-routes) when they need more precise triggers or output boundaries. Compilation preserves custom agent files and their model settings. The host still decides whether it can dispatch the agent under the current runtime and user instructions.

For measured work, the optional project-local [`execution` ledger](docs/OPERATIONS.md#agent-execution-and-usage) separates a host callback, a manual execution assertion, and rollout-verified usage from the configured agent profile. It can import per-response usage from a named Codex rollout, keeps unavailable measurements null, and does not claim that a planned specialist ran.

## Typical interaction

A typical Codex conversation can be as simple as:

```text
Adopt Tasktra in this project, show me the proposed configuration, and do not apply it yet.
```

or:

```text
Define a durable goal to add account export, authorize local code changes and tests,
but stop before creating a pull request.
```

Tasktra will turn such intent into explicit scope, acceptance criteria, authority, budget guidance, checkpoints, and a resumable record.

## Documentation

- [Product specification](docs/SPECIFICATION.md)
- [Roadmap and stage acceptance](docs/ROADMAP.md)
- [Requirements coverage matrix](docs/REQUIREMENTS.md)
- [Operations guide](docs/OPERATIONS.md)
- [Token efficiency and controlled comparisons](docs/TOKEN_EFFICIENCY.md)
- [Platform notes](docs/PLATFORM_NOTES.md)
- [Release policy](docs/RELEASE_POLICY.md)
- [Self-hosting evidence](docs/SELF_HOSTING.md)
- [Portable examples](examples/README.md)
- [Changelog](CHANGELOG.md)
- [Stage 1 acceptance checklist](docs/stages/stage-1.md)
- [Stage 2 acceptance checklist](docs/stages/stage-2.md)
- [Stage 3 completion report](docs/stages/stage-3-completion.md)
- [Stage 4 completion report](docs/stages/stage-4-completion.md)
- [Stage 5 completion report](docs/stages/stage-5-completion.md)
- [Stage 6 completion report](docs/stages/stage-6-completion.md)
- [Active Stage 7 acceptance checklist](docs/stages/stage-7.md)
- [Architecture decisions](docs/adr/README.md)

## License

Tasktra is released under the [MIT License](LICENSE).
