# Release and compatibility policy

## Version contract

Tasktra follows semantic versioning for its Python package, catalog, packs, schemas, lockfile, and generated projections. A release is valid only when those surfaces agree and an exact upgrade preview can describe every managed create, update, deletion, pack migration, and runtime-schema transition.

- Patch releases contain compatible corrections.
- Minor releases may add backward-compatible capabilities and contracts.
- Major releases may change contracts only through declared, previewable compatibility edges and explicit recovery guidance.

Project configuration, extensions, curated knowledge, and unrelated files remain project-owned. Generated content is replaced only when its prior manifest proves ownership and local edits are absent.

## Release gate

A release candidate must pass the complete test suite, configured validation, clean projection drift, all supported-platform CI, package build/install/removal checks, representative offline and degraded-capability examples, self-hosting checks, and independent security and acceptance review.

The release audit accepts only a closed, hash-bound evidence chain. Package, catalog, runtime, and every source pack version surface must exist; selected lock and generated-manifest pack sets must be exact and nonempty. The decision must name the local committed `HEAD` and its configured HTTPS GitHub origin. CI evidence must identify the committed workflow and all nine Windows/macOS/Linux by Python 3.11/3.12/3.13 GitHub Actions jobs with exact run/job URLs, the same commit, and the same release-artifact SHA-256. The independent review report must parse as a closed record bound to that version, repository, revision, CI evidence, and artifact with no blockers.

The decision itself is canonical JSON signed with RSA PKCS#1 v1.5 and SHA-256. Its public trust key and CI workflow must already exist in the named commit; pointing at an uncommitted or decision-supplied key is insufficient. Arbitrary HTTPS hosts, self-asserted review status, mismatched artifacts, open schemas, missing component collections, and unsigned decisions fail closed.

No release gate authorizes publishing, pushing, merging, deployment, or external communication. Those remain separate consequential effects.

## Recovery and rollback

Before applying an upgrade, retain its exact plan digest and bounded pre-mutation snapshot. File-only changes may be rolled back from the recorded snapshot. When a runtime schema changes, Tasktra records the exact database-backup path and SHA-256 and disables automatic file rollback; recovery requires an explicit human decision using that database backup.

Never restore a database backup over a live process. Stop Tasktra activity, preserve the failed database, verify the backup digest, restore to the configured contained path, run `tasktra doctor`, verify the audit chain, and preview the upgrade again.

## Installation and removal boundary

Installing or uninstalling the Python distribution may add or remove package and console-script files only. It must never delete or rewrite a project's `.tasktra`, `.agents`, `.codex`, `.claude`, `AGENTS.md`, `CLAUDE.md`, extensions, knowledge, runtime database, or generated manifest. Project cleanup is a distinct, explicit operation and is not part of package uninstall.

## Post-1.0 compatibility

The 1.x line preserves published configuration, authority, handoff, work-item, provider, scheduler, telemetry, lesson, and lock contracts unless an additive versioned field is explicitly supported. Contract removal or incompatible meaning requires a new major version, an exact migration preview, retained recovery evidence, and updated examples and release notes.

Security corrections may fail closed on previously accepted unsafe input without waiting for a major release. Such changes must be called out in the changelog with recovery or migration guidance.
