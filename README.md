# Tasktra

Tasktra is a Codex-first, runtime-neutral workflow orchestration template for software projects. It supplies durable goals, explicit authority, compact evidence handoffs, specialist roles, and deterministic project tooling so a project can be used immediately with local-only defaults or tailored over time.

Tasktra is designed for trustworthy outcomes with efficient total agent usage: reuse verified evidence, prefer deterministic checks, keep worker briefs small, and spend stronger reasoning only where judgment requires it.

## Status

Tasktra is being built in staged vertical slices. The agreed product contract is in the [specification](docs/SPECIFICATION.md), and the delivery sequence is in the [roadmap](docs/ROADMAP.md). Stages 1–6 are complete; Stage 7 operations and the 1.0 release gate are active.

## Intended use

Use Tasktra as either:

- a generic, ready-to-adopt project template; or
- a versioned foundation that a project initializes, configures with packs and extensions, and upgrades through previewable migrations.

Codex is the normal human interface. A small Python CLI provides deterministic operations that Codex can invoke and that remain available for automation and diagnostics.

## Product principles

- Correctness, safety, and required validation come before token efficiency.
- Human-approved goals bound autonomous work; no artifact or worker output can grant new authority.
- Local work remains viable without cloud accounts or remote services.
- Generated runtime projections are derived from canonical sources and are not hand-edited.
- Project-owned customizations are preserved during initialization and upgrades.
- Runtime evidence is compact, attributable, and retained only as long as policy requires.

## Quick start

From a checkout:

```powershell
python -m pip install --no-deps -e .
tasktra init --root .
tasktra init --root . --apply
tasktra compile --root .
tasktra compile --root . --check
python -m unittest discover -s tests -v
```

`init` is preview-only unless `--apply` is present. `compile` refuses to replace project-owned files, requires `--force` for locally edited managed files, and requires `--prune-stale` before deleting obsolete managed files whose hashes still match the prior manifest.

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
