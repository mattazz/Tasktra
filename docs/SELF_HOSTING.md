# Self-hosting evidence

Tasktra develops Tasktra through the same durable goal, authority, work-unit, handoff, validation, and audit contracts shipped to other projects. Generated projections and worker output are evidence only; neither can authorize a transition.

## Governing goal

- Goal: `tasktra-1-0`
- Authority envelope: `.tasktra/authority/tasktra-1-0.json`
- Scope: the Tasktra repository only
- Allowed effects: read-only and local reversible writes
- Prohibited effects: repository-history changes, remote mutation, external communication, merge, deployment, and destructive deletion
- Checkpoints: `stage-three` through `stage-seven`
- Token budget: no hard cap; measured usage is recorded when the runtime supplies it
- Attempt budget: 100
- Concurrency limit: 3

The human-approved envelope hash is bound to every work claim and completion approval. The goal steward is distinct from the coordinator and cannot expand scope or approve work it performed.

## Completed self-hosted stages

| Checkpoint | Durable workflow | Completion evidence |
| --- | --- | --- |
| Stage 3 | durable autonomy workflow | `docs/stages/stage-3-completion.md` |
| Stage 4 | `.tasktra/workflows/stage-4-remote-capabilities.json` | `.tasktra/evidence/stage-4-completion.json` |
| Stage 5 | `.tasktra/workflows/stage-5-specialist-ecosystem-packs.json` | `.tasktra/evidence/stage-5-completion.json` |
| Stage 6 | `.tasktra/workflows/stage-6-lifecycle-optimization.json` | `.tasktra/evidence/stage-6-completion.json` |

Stage 6 also exercised recovery: an expired attempt was recovered and requeued from `.tasktra/evidence/stage-6-requeue.json` without duplicating an external effect. Its replacement attempt completed with matching workflow and evidence hashes, after which the checkpoint advanced atomically.

## Stage 7 record

`stage-7-operations-release` is bound to the `stage-seven` checkpoint and `docs/stages/stage-7.md`. Its completion requires platform, scheduling, packaging, example, operations, efficiency, security, and independent acceptance evidence. A passing test or generated release artifact alone cannot close it.

The live self-host upgrade applied digest `2da1f95e532f68dc54e8a11ca206f8785847541d195ae048016b55e433de423c`, migrated the sealed runtime from schema 8 to 10, and retained `.tasktra/runtime/tasktra.sqlite.v8.bak` with SHA-256 `a07cfd635a1a7bd3d500c353af7eef4e46fbf5a824b57640a48bb0f86877a98e`. Six bounded test groups plus the tooling-enabled wheel rerun accounted for 341 test cases: 335 passed and six explicit environment-dependent skips remained. The compile check and live audit were clean. Four interrupted local upgrade intents remain durable evidence, produced no external effect, and have terminal `failed-before-mutation` receipts rather than remaining pending.

Stage 7 remains open. `.tasktra/workflows/stage-7-operations-release.json` is blocked—not completed—until exact Windows, macOS, and Linux CI runs, a final independent GO, and a hash-bound release decision exist. The current envelope does not authorize creating repository history, remote CI state, a merge, or a deployment.

## Verification

Run these read-only checks from the repository root:

```powershell
tasktra goal --root . show tasktra-1-0
tasktra status --root .
tasktra audit --root . verify
tasktra compile --root . --check --trust-catalog
```

The goal view must show each completed checkpoint as reached with evidence hashes, the active Stage 7 attempt within its lease, no emergency stop, and a clean audit chain.
