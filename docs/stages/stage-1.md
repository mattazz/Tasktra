# Stage 1 acceptance checklist — Foundation

Stage 1 establishes compilation and configuration safety. It does not authorize claims about autonomous goal execution, remote adapters, or completed specialist packs.

## Scope

- Canonical source directories and schemas for project configuration, lockfile, pack manifest, and generated-file manifest.
- A standard-library Python CLI skeleton with deterministic `init` preview, validation, compilation, and drift-check commands.
- Codex and Claude generated projections from the same canonical representation.
- Explicit managed, project-owned, generated, and runtime ownership boundaries.
- Initial core role catalog metadata and a project-facing entrypoint strategy.
- Documentation, requirements traceability, decisions, and fixtures needed to exercise the foundation.

## Acceptance criteria

- [x] A minimal generic project configuration validates against its versioned schema.
- [x] Invalid configuration reports useful, path-specific errors without modifying project files.
- [x] Pack resolution has deterministic order and fails on declared conflicts.
- [x] Compilation produces reproducible Codex and Claude outputs from one canonical input.
- [x] The generated-file manifest captures source identity and content hashes.
- [x] Drift check detects a changed generated file and reports the owned source or takeover path.
- [x] Initialization inventories existing agent instructions, creates a no-write preview, and does not overwrite them.
- [x] The lockfile records core version, enabled packs, schema versions, checksums, and generated hashes.
- [x] Runtime-state locations are excluded from generated commits by documented configuration.
- [x] CLI help and generated entrypoints point to the canonical project profile and explain their managed status.
- [x] Unit and integration tests cover configuration, resolution, compilation, drift, and no-write previews.
- [x] Golden tests cover Codex and Claude output for a generic fixture.
- [x] Documentation links pass a mechanical check and no delivered document introduces historical-source references.
- [x] The requirements matrix is updated with verified Stage 1 evidence before stage closure.

## Exit evidence

Record command lines and outcomes for schema, unit, integration, golden-output, drift, and documentation-link checks; changed schema versions; known limitations; independent review findings; and the decision whether Stage 2 is eligible.

The recorded evidence and closure decision are in the [Stage 1 completion report](stage-1-completion.md).
