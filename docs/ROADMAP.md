# Roadmap

This roadmap is the durable delivery plan. A stage may advance only when its acceptance evidence is recorded, its requirement coverage is current, and unresolved limitations are explicit. Stages [1](stages/stage-1-completion.md), [2](stages/stage-2-completion.md), [3](stages/stage-3-completion.md), [4](stages/stage-4-completion.md), [5](stages/stage-5-completion.md), and [6](stages/stage-6-completion.md) are complete; the active Stage 7 gate is [here](stages/stage-7.md).

| Stage | Outcome | Depends on | Status |
| --- | --- | --- | --- |
| 1. Foundation | Canonical configuration, schemas, compiler skeleton, projections, drift checks, and project docs | — | Complete |
| 2. Local workflows | Core roles, local work-item workflows, validation and review loops | 1 | Complete |
| 3. Durable autonomy | Goal ledger, steward approvals, budgets, recovery, audit history, resumable execution | 1–2 | Complete |
| 4. Remote capabilities | Git/GitHub/Jira adapters and visible capability degradation | 1–3 | Complete |
| 5. Specialist and ecosystem packs | Specialist catalog plus generic, Python, TypeScript, web, and monorepo packs | 1–4 | Complete |
| 6. Lifecycle and optimization | Upgrades, migrations, telemetry, benchmarks, and lesson promotion | 1–5 | Complete |
| 7. Operations and release | Scheduling, CI integration, cross-platform examples, self-hosting, and 1.0 release gate | 1–6 | In progress |

## Stage 1 — Foundation

Create the versioned canonical source model and deterministic tooling needed to compile runtime projections without direct edits. Establish project documentation, requirements traceability, initial schemas, configuration validation, lockfile model, generated-file manifest, and drift checking. This stage deliberately does not claim durable autonomous execution.

## Stage 2 — Local workflows

Make the local baseline useful without online services. Implement universal role contracts, structured handoff validation, local Markdown work items, configurable validation commands, and review-oriented workflows. Exercise safe worktree selection and preservation of unrelated changes.

## Stage 3 — Durable autonomy

Implement the SQLite goal engine, authority envelope, steward separation of duties, work-unit leasing, budgets, audit journal, recovery, exports, and failure classification. Prove that an interrupted session can resume a valid eligible unit without reauthorizing or duplicating work.

## Stage 4 — Remote capabilities

Add capability-driven Git, GitHub, and Jira providers, with mocks and local fallbacks. Ensure remote requests have receipts and idempotency keys, credentials remain external, unavailable capability is explicit, and local work remains unblocked.

## Stage 5 — Specialist and ecosystem packs

Ship the core specialist catalog and generic software-development family. Add detection-assisted but non-mutating configuration packs for generic repositories, Python applications, TypeScript/web applications, and monorepos. Add composition, conflict, capability, and pack-migration validation.

## Stage 6 — Lifecycle and optimization

Deliver safe existing-project adoption, previewable upgrades, semantic-version compatibility checks, migrations, project ownership escape hatches, local telemetry, representative efficiency benchmarks, and structured lesson proposals. No automatic remote telemetry or unattended upgrades.

## Stage 7 — Operations and release

Add scheduler adapters for Codex automation, CI, local runner, and manual fallback. Validate Windows, macOS, and Linux. Ship tested example projects, complete operational guidance, self-hosted Tasktra development evidence, and the 1.0 release review.

## Continuous delivery protocol

For every stage: map criteria to checks; run unit, integration, generated-output, and applicable migration/rollback tests; review authority boundaries and documentation; record measured representative efficiency data; obtain independent review; update [requirements coverage](REQUIREMENTS.md); and retain a completion report. Continue to the next eligible stage without pausing unless a new product decision, authority expansion, destructive action, or unresolved tradeoff requires human direction.
