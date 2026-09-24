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

`init` is preview-only unless `--apply` is present and never overwrites an existing profile. `bootstrap` preserves an existing profile and creates or migrates only its local runtime. `compile` refuses to replace project-owned files, requires `--force` for locally edited managed files, and requires `--prune-stale` before deleting obsolete managed files whose hashes still match the prior manifest. Installing the package is what makes `python -m tasktra` available outside the Tasktra checkout; `--root` identifies the project it should operate on.

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
