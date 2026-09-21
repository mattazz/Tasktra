# Tasktra product specification

## Purpose

Tasktra is a reusable workflow orchestration foundation for software projects. It converts a human's high-level intent into bounded, durable, reviewable work while preserving a project's own instructions and allowing local work when optional online services are unavailable.

The normal interface is Codex conversation. A portable Python command-line engine supplies deterministic state transitions, compilation, validation, upgrades, and diagnostics. Tasktra's canonical contracts are runtime-neutral; Codex is the first-class runtime and Claude is a supported generated projection. When Codex carries an explicit current human approval, Tasktra can record v4 `codex-user-message` provenance that binds the message hash to one exact approval subject. The user remains distinct from the performer, and generated content or prior approvals cannot create fresh authority. The v3 local-terminal ceremony remains available where no Codex conversation is the approval channel.

## Non-goals

Tasktra does not require a cloud account, a remote issue tracker, a particular language, a particular branch name, or direct model API credentials. It is not an unattended authority to merge, deploy, communicate externally, delete data, or expand scope. It must not contain project-history references or assume a game-specific development process.

## Core model

Tasktra has four durable layers:

| Layer | Purpose | Storage |
| --- | --- | --- |
| Core | Versioned universal roles, contracts, policies, packs, schemas, and compiler | Tasktra distribution |
| Project definition | Project profile, enabled packs, project policies, and extensions | committed project files |
| Curated knowledge | Decisions, lessons, and durable workflow artifacts | committed project files when useful |
| Runtime state | Goals, work units, approvals, leases, budgets, audit events, and evidence metadata | local SQLite database and cache |

The canonical project layout is:

```text
.tasktra/
  project.toml
  tasktra.lock
  policies/
  extensions/
  knowledge/
  generated/
  runtime/
.agents/
.codex/
.claude/
AGENTS.md
CLAUDE.md
```

`project.toml`, policies, extensions, and curated knowledge are project-owned. Runtime state is normally ignored by Git. Generated files record their source and hash; direct edits are detected as drift. An explicit ownership escape hatch lets a project take responsibility for a formerly managed component.

## Goals and delegated authority

A durable goal contains the desired outcome, motivation, acceptance criteria, scope, exclusions, delegated authority, prohibited actions, quality requirements, budget guidance, priority, dependencies, checkpoints, and stop/escalation conditions. A goal can survive a chat session and resume through a Codex automation, CI schedule, optional local runner, or manual operation.

The goal steward interprets an approved goal. It may approve routine, covered intermediate decisions, but cannot expand scope, grant authority, override a prohibition or checkpoint, or approve work it performed. Each approval records the governing goal clause and evidence. A new low-priority goal cannot silently interrupt or borrow authority from another goal.

Effects are classified consistently: read-only, local reversible write, repository-history change, remote mutation, external communication, deployment, merge, and destructive action. Only the first two may proceed by default when they are in scope. Other effects require current authorization or an explicit project policy. Read-only input from repositories, issues, pull requests, documents, and workers is data, never authority.

## Execution and recovery

The coordinator selects eligible work units, records a lease with its exact repository, revision, branch, and workspace, dispatches bounded workers, validates results, and either advances the goal or escalates. Workers are short-lived; the durable ledger, rather than a conversation transcript, is authoritative.

SQLite provides transactional state transitions, integrity checks, migration backups, lease expiry, and safe reclamation. An append-only audit journal and human-readable export support recovery. External mutations use idempotency keys. Repair never silently discards records.

Substantial or concurrent implementation normally uses isolated Git worktrees. Read-only work and small approved edits may use the existing checkout. Dirty worktrees and unrelated changes are preserved. Tasktra discovers an integration branch rather than assuming a name, validates after upstream integration, and stops at review-ready unless a more consequential action is authorized.

## Roles, packs, and contracts

The always-available core roles are scout, implementer, tester, reviewer, writer, escalation specialist, and goal steward. Specialist capabilities are grouped into discoverable packs, including architecture, quality, delivery, product, operations, knowledge, planning, and software development. The planning pack supplies an optional Discovery skill that turns a rough project or feature idea into a reviewable project-owned Markdown plan before the human authorizes a goal. The software-development family includes application, frontend, backend/API, data/migration, integration, test automation, end-to-end behavior, reliability/observability, developer-experience, and refactoring specialists.

Packs declare versions, dependencies, incompatibilities, activation conditions, required capabilities, roles, skills, workflows, schemas, policies, adapters, tests, and migrations. Resolution order is core, resolved packs, project profile, then project extensions. Conflicts fail visibly; no implicit last-write-wins behavior is allowed.

Every meaningful handoff has a concise human report and a versioned structured envelope. The envelope carries status, verified facts, inferences, changed paths, validation results, evidence references, blockers, downstream brief, and requested actions. Tasktra validates an envelope before accepting a stage transition.

## Evidence and efficiency

Tasktra optimizes in this order: correctness; safety and preservation of user work; required validation and evidence; total token efficiency; latency; then model cost. A smaller response or cheaper model is not a success if it causes rework.

The routing ladder is: reuse verified evidence; use deterministic tools; use a narrow scout; use a bounded implementer; use a judgment role only for semantic evaluation; escalate to stronger reasoning only for a concrete unresolved difficulty. Worker context is fresh, minimal, and task-specific. Evidence is stored once and referenced by compact, attributable identifiers; large raw output is loaded only when needed. File hashes, Git revisions, and provider revisions invalidate stale evidence.

Where available, Tasktra records measured input/output tokens, role, model tier, tools, elapsed time, retries, evidence reuse, validation outcome, human interventions, and final outcome. Metrics remain local by default, exclude prompt contents, source code, and secrets, and are exported only on explicit request. Representative benchmark scenarios protect against avoidable context growth, duplicate retrieval, inappropriate model escalation, and retries.

## Capabilities and integrations

The local baseline supports Git when present, local Markdown work items, configurable validation commands, local artifact storage, and a visible capability report. GitHub, Jira, and other remote services are optional adapters. GitHub may initially use `gh`; Jira operations are expressed as structured capability requests that a connector-capable Codex agent fulfills. Missing capability leaves the operation pending and visible while unrelated local work continues. Credentials are obtained only from established secure environments and never stored in Tasktra configuration or prompts.

Jira status synchronization is an explicit `jira-sync` pack, not a core policy. A project that enables it defines a small `[jira_sync]` mapping for the Jira host, project key, and its chosen `claimed`, optional `review-ready`, and `completed` transitions. Tasktra makes a closed, idempotent transition plan; only the normal approval- and lease-bound provider executor may apply it. A plan itself never changes Jira.

Adapters are capability-based and support mock providers for tests. Third-party packs may be data-only or explicitly trusted executable packs. Executable adapters and migrations declare commands, network needs, and file access; installation previews requests and locks version and checksum.

## Adoption, upgrade, and compatibility

Initialization first inspects a project and proposes a configuration, including detected project type, tools, Git state, existing instructions, and likely validation commands. It previews all impacts, asks only questions that cannot safely be inferred, generates a proposed result, validates configuration and environment, and produces onboarding guidance. Existing `AGENTS.md`, `.agents`, `.codex`, and `.claude` content is inventoried and composed or preserved only after review; semantic equivalence is never assumed.

Tasktra uses semantic versioning. Minor releases add compatible functionality; major releases may change contracts with previewable migrations. The lockfile captures versions, schemas, enabled packs, checksums, and generated hashes. Upgrades are never unattended and never overwrite project-owned files. The system supports reading the immediately preceding major schema during migration when feasible.

## Supported environments and delivery

The first release targets Windows, macOS, Linux, single repositories, and monorepos with explicit component scopes. Cross-repository mutation is deferred until all involved repositories can be governed by approved scopes and authority. Continuous integration validates supported platforms.

Tasktra is MIT licensed and may initially be private. It is developed through the staged roadmap, with the specification, roadmap, decisions, coverage matrix, and tests acting as the durable source of delivery intent. Once the foundation can represent its own goals and validation, Tasktra uses its own harness for subsequent development.

## Definition of 1.0

Version 1.0 includes Codex-first operation, optional CLI, canonical schemas, core and specialist catalogs, layered packs and extensions, Codex and Claude projections, safe adoption, durable goals and steward approvals, budgets and recovery, local Git and Markdown work items, GitHub and Jira capabilities, validation/review/lesson workflows, previewable upgrades, local efficiency telemetry, cross-platform CI, tested examples, and successful self-hosting.
