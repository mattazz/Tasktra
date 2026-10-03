# Changelog

All notable changes to Tasktra are documented here. Dates use ISO 8601.

## Unreleased

### Added

- An explicit `tasktra run` preview/apply path for one approved work unit, with
  isolated Codex checkouts, fresh verification stages, attributable receipts,
  bounded patch publication, cancellation, and observed token accounting.
- Immutable research, documentation, and deterministic verification policies,
  gated by explicit envelope permissions; existing work keeps its default policy.
- Derived execution health and parent-linked agent stages in status and the portal.
- A bundled, read-only local web portal for goals, jobs, agent records, budgets,
  and recent activity, with responsive layouts, animated robot avatars, and a
  clearly labeled browser-only demo.

### Corrected

- Goal-contract changes cannot strand existing scopes, checkpoints, or policies;
  failed work can be requeued with current authority and remaining budgets.
- Interrupted reservations remain explicitly unmeasured budget charges, and
  verified soft token overruns remain visible as debt instead of lost spend.
- Ambiguous local effects remain outstanding until an evidence-backed human
  recovery decision is recorded. Upgrade CLI failures preserve that distinction.
- Provider, validation, migration, and host processes use one supervised runner.
- Concurrent telemetry appends preserve events, and OS-owned file locks release
  automatically after crashes. Benchmark verdicts prioritize verified quality
  and reject unverified efficiency claims.
- Locking and isolated workspaces handle macOS temporary-directory aliases and
  Windows short paths while retaining checks against links inside the project.
- Git commit requests must remain within the approved envelope, work-unit, and
  human-approval file scopes. Previously accepted out-of-scope requests now fail
  closed; obtain appropriately scoped authorization before preparing new work.
- Read-only Git diffs disable repository-configured text converters.
- Initialization and metadata/journal writes reject symbolic-link and Windows
  reparse-point redirection. Keep these Tasktra directories inside the project
  rather than linking them to another location.
- New project profiles are published without replacing a concurrent file, and
  names containing quotes, backslashes, controls, or emoji round-trip correctly.
- Compile failures restore affected generated files and metadata. Upgrade
  failures after a database migration retain explicit recovery evidence instead
  of falsely reporting a file rollback; follow the recorded database backup and
  recovery guidance before further work.
- Validation cleans up owned descendants before closing output streams, including
  children that outlive their parent or ignore POSIX termination signals.
- An unavailable optional execution ledger no longer hides workers recorded by
  runtime leases.
- Configured validation discovers every test instead of relying on a manually
  maintained module list.

## 1.0.0 — 2026-09-20

Initial release candidate of the generic Codex-first, runtime-neutral orchestration foundation.

### Included

- Deterministic Codex and Claude projections with drift detection.
- Local workflows, structured handoffs, work items, validation, and review gates.
- Durable goals, steward approvals, budgets, leases, checkpoints, recovery, and audit integrity.
- Optional Git, GitHub, and Jira capabilities with local fallback.
- Universal and specialist software-development roles and ecosystem packs.
- Preview-first adoption, checksum-bound upgrades, migration evidence, and explicit recovery boundaries.
- Opt-in local telemetry, measured efficiency benchmarks, and reviewed lesson promotion.
- Capability-based scheduling, cross-platform CI definition, generic examples, operational guidance, packaging checks, and self-hosting evidence.

### Compatibility and safety

- Local work remains available without remote accounts.
- Existing project instructions and project-owned files are preserved.
- Consequential remote, history, deployment, merge, communication, and destructive effects require separate authority.
- Explicitly trusted executable packs run as reviewed full-host code; declared effects and environment scrubbing are review aids, not an OS sandbox.
- Package uninstall never removes project state.

The release remains a candidate until every supported-platform CI job and independent release review passes. See `docs/RELEASE_POLICY.md` for the 1.x compatibility and recovery contract.
