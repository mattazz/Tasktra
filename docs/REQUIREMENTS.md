# Requirements coverage matrix

The identifiers below make the product contract reviewable. “Verified” applies only to the delivered scope named in the status; “Partial” identifies implemented foundations whose later-stage behavior remains open. Evidence is recorded in the [Stage 1](stages/stage-1-completion.md), [Stage 2](stages/stage-2-completion.md), [Stage 3](stages/stage-3-completion.md), [Stage 4](stages/stage-4-completion.md), [Stage 5](stages/stage-5-completion.md), and [Stage 6](stages/stage-6-completion.md) completion reports.

| ID | Requirement | Primary stage | Evidence at completion | Status |
| --- | --- | --- | --- | --- |
| R1 | Codex-first conversational operation with optional deterministic CLI | 1–2 | generated skills and CLI integration tests | Verified — Stage 2 local workflow surface |
| R2 | Runtime-neutral canonical sources generate Codex and Claude projections | 1 | golden projection and drift tests | Verified — Stage 1 |
| R3 | Local-only baseline works without remote accounts | 2 | offline workflow integration test | Verified — Stage 2 |
| R4 | Layered project configuration, packs, extensions, lockfile, and ownership zones | 1, 5–6 | resolution, conflict, and upgrade tests | Verified — Stage 6 exact managed writes/deletions, lock binding, containment, and project-owned preservation |
| R5 | Core and specialist software-development role catalog | 2, 5 | catalog and contract validation | Verified — Stage 5 core and 29-specialist catalog with generic and ecosystem packs |
| R6 | Structured, versioned handoffs plus concise human reports | 2 | envelope schema and workflow tests | Verified — Stage 2 |
| R7 | Durable goals, authority envelopes, steward approvals, and effect gates | 3 | authorization and separation-of-duty tests | Verified — Stage 3 local gates and Stage 4 provider-effect authority |
| R8 | Resumable local state, leases, audit history, and crash recovery | 3 | interruption/recovery and integrity tests | Verified — Stage 3 durable local execution |
| R9 | Evidence reuse, deterministic routing, and token-efficiency controls | 2–3, 6 | benchmark baselines and routing tests | Verified — Stage 6 bounded comparative measurements and regression findings without invented savings |
| R10 | Local-only metrics and explicit sanitized export | 6 | privacy and export tests | Verified — Stage 6 closed-schema local opt-in and explicit sanitized export |
| R11 | Git, Markdown work items, GitHub, and Jira capability adapters | 2, 4 | provider mock and fallback tests | Verified — Stage 4 bounded Git, GitHub, Jira, health, and offline fallback surfaces |
| R12 | Safe initialization and existing-project adoption | 1, 6 | preview and preservation fixtures | Verified — Stage 6 bounded inventory, uncertainty, conflict visibility, and project-owned preservation |
| R13 | Previewable upgrades, migrations, semantic compatibility, and rollback evidence | 6 | migration/rollback tests | Verified — Stage 6 digest-bound plans, exact runtime evidence, bounded snapshots, rollback, and explicit database recovery |
| R14 | Monorepo/worktree isolation and preservation of unrelated work | 2, 7 | workspace and dirty-tree tests | Partial — preservation, bounded affected-scope fan-out, package isolation planning, and read-only guidance verified; worktree execution remains |
| R15 | Windows, macOS, Linux support, CI, examples, and scheduling | 7 | cross-platform CI and fixture tests | Partial — scheduling, fallbacks, examples, Windows validation, and the cross-platform CI matrix are verified locally; authoritative macOS/Linux run evidence remains |
| R16 | Explicit untrusted-input and third-party-pack trust boundaries | 3–6 | adversarial authority and pack tests | Verified — Stages 3–6 authority, provider, executable-pack, migration, telemetry, path-containment, and reparse boundaries fail closed |
| R17 | Self-hosted continuous development and stage governance | 1–7 | stage reports and self-hosting record | Partial — the live sealed runtime upgraded from schema 8 to 10 under Tasktra authority with a verified backup, 335 passing tests, six explicit environment skips, and terminal evidence for four recoverable interruptions; the external Stage 7 release decision remains open |

See [the roadmap](ROADMAP.md), the completed [Stage 6 report](stages/stage-6-completion.md), and the active [Stage 7 checklist](stages/stage-7.md).
