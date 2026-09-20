# Stage 6 acceptance checklist — Lifecycle and optimization

Stage 6 makes Tasktra safe to adopt and upgrade in existing projects, then adds privacy-preserving evidence for improving orchestration efficiency without weakening correctness or authority.

## Scope

- Preview-first adoption that inventories existing runtime instructions and project-owned extensions.
- Semantic-version compatibility checks, deterministic upgrade plans, bounded migrations, rollback evidence, and preserved ownership zones.
- Local-only usage telemetry with explicit sanitization and export.
- Representative benchmarks for evidence reuse, context size, retries, model escalation, latency, and validation outcomes.
- Structured lesson proposals that require review before promotion into canonical workflows, roles, skills, or packs.

## Acceptance criteria

- [x] Adoption inspection is read-only, bounded, deterministic, and reports uncertainty.
- [x] Existing `AGENTS.md`, `.agents`, `.codex`, and `.claude` content is inventoried without assuming semantic equivalence.
- [x] Adoption preview shows every managed write, preserved project-owned path, conflict, capability gap, and validation command before application.
- [x] Upgrade planning compares installed and target semantic versions and fails visibly on unsupported compatibility edges.
- [x] Pack and runtime migrations are previewable, ordered, checksum-bound, authority-scoped, and never execute during preview.
- [x] Applied migrations retain recoverable pre-mutation evidence and verify the resulting lock, generated manifest, and project-owned content.
- [x] Migration failure rolls back or stops with explicit recovery instructions; no partial success is reported as complete.
- [x] The immediately preceding major schema can be read for migration when the declared compatibility contract permits it.
- [x] Local telemetry records only approved bounded metadata and excludes prompts, source content, credentials, and secrets.
- [x] Telemetry is disabled or local by default and leaves the machine only through an explicit sanitized export.
- [x] Representative benchmark fixtures measure context/evidence counts, retries, elapsed time, validation results, and human interventions without inventing token savings.
- [x] Benchmarks detect duplicate retrieval, avoidable context growth, unnecessary model escalation, and retry regressions.
- [x] Lesson candidates cite durable evidence, identify applicability and risks, and remain proposals until independent review approves promotion.
- [x] Promoted lessons update canonical sources and regenerate projections through the normal compiler and drift gates.
- [x] Independent semantic, security, privacy, migration, rollback, and acceptance reviews find no unresolved blocker.
- [x] Requirements coverage and a Stage 6 completion report are current before closure.

## Exit evidence

Record exact adoption, upgrade, rollback, telemetry-privacy, export, benchmark, and lesson-promotion commands and outcomes. Include representative existing-project fixtures, ownership-preservation proofs, compatibility failures, recovery evidence, measured observations, independent findings, and the Stage 7 eligibility decision.
