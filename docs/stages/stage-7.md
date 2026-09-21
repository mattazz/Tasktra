# Stage 7 acceptance checklist — Operations and 1.0 release

Stage 7 turns the reviewed local orchestration foundation into a portable, operable 1.0 release without weakening local-first behavior or authority boundaries.

## Scope

- Scheduler adapters for Codex automation, CI, a local runner, and a manual fallback.
- Cross-platform verification on Windows, macOS, and Linux.
- Generic example projects and end-to-end adoption, goal, upgrade, recovery, and provider-degradation walkthroughs.
- Operational, security, privacy, backup, recovery, and release guidance.
- Self-hosted evidence showing Tasktra can continuously develop Tasktra under its own goal and authority contracts.
- Packaging, compatibility, and final 1.0 release gates.

## Acceptance criteria

- [x] Scheduling remains capability-based: Codex automation is primary, with CI, local-runner, and manual fallbacks that do not block local work.
- [x] Scheduled work preserves the human goal, authority envelope, budget, checkpoint, lease, idempotency, and notification intent across runs.
- [x] No scheduler, CI job, generated artifact, worker, or provider response can broaden authority or approve its own consequential effect.
- [ ] Windows, macOS, and Linux CI exercise installation, compilation, local workflows, durable goals, migrations, privacy controls, and representative examples.
- [x] Platform-specific link, reparse, process-tree, locking, path, and shell behavior is tested or documented with a fail-closed fallback.
- [x] Generic application, service/API, web, Python, TypeScript, and monorepo examples demonstrate ready-to-use and customized profiles without source-project history.
- [x] At least one offline end-to-end example completes with no remote account; unavailable GitHub, Jira, research, or scheduler capabilities degrade visibly without blocking eligible local work.
- [x] Operational guidance covers installation, Codex-first use, CLI fallback, upgrades, backups, recovery, audit verification, telemetry/export, trusted executable packs, and incident stop procedures.
- [x] Packaging is reproducible and versioned; install, upgrade-from-prior, rollback/recovery, and clean-uninstall boundaries are verified.
- [x] Self-hosted Tasktra development records durable goals, staged handoffs, authority decisions, validations, review findings, measured usage, and recoverable interruptions without circularly treating generated output as authority.
- [x] Efficiency evidence reports measured successful outcomes, retries, elapsed time, context/evidence counts, and token usage when available; required validation is never skipped to improve a metric.
- [ ] Security, privacy, migration, provider, scheduler, cross-platform, documentation, and acceptance reviews find no unresolved release blocker.
- [x] Requirements coverage, changelog/release notes, license, version metadata, examples, and local completion evidence are current and mutually consistent.
- [ ] The 1.0 release decision explicitly records known limitations, compatibility promises, recovery expectations, and the post-1.0 upgrade policy.

## Exit evidence

Record exact CI runs and platform versions, scheduler fixtures and fallback behavior, packaged artifacts and hashes, example commands and outcomes, offline and degraded-provider proofs, migration/recovery results, self-hosting ledger references, measured efficiency observations, independent findings, and the signed 1.0 release decision.

Historical local evidence is archived in `docs/development-history/tasktra-1.0/evidence/` and `docs/development-history/tasktra-1.0/workflows/`. The three unchecked criteria are deliberate release blockers: authoritative cross-platform execution, blocker-free final review, and the signed release decision.
