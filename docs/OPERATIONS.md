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
already exists, it validates and preserves it byte-for-byte, then creates or
migrates only the configured local runtime. `status` and `doctor` are
read-only: they report a missing runtime rather than creating one.

Package installation and removal affect only distribution files. They do not remove project-owned configuration, generated projections, knowledge, extensions, or runtime state. See [the release policy](RELEASE_POLICY.md).

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
