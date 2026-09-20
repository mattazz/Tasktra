# Stage 1 completion report — Foundation

Status: complete on 2026-09-19. Stage 2 is eligible.

## Delivered outcome

Stage 1 established a canonical runtime-neutral catalog, versioned project and artifact contracts, deterministic Codex and Claude projection generation, preview-first adoption, explicit ownership boundaries, drift reporting, a reproducible lockfile, and a planned-only transactional ledger. The foundation deliberately does not activate autonomous goals or claim the later workflow, adapter, upgrade, or release capabilities.

The self-hosted projection contains 47 managed files. Its lock pins Tasktra `0.1.0`, catalog `0.1.0`, core pack `0.1.0`, the full catalog SHA-256, the generated-manifest SHA-256, configuration schema 1, and runtime schema 2.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```

Result: 36 tests passed; one symlink integration test was skipped because the host did not grant Windows symlink privilege. The passing suite includes configuration and contract errors, deterministic pack resolution and conflicts, golden Codex and Claude outputs, manifest and lock round trips, lockfile drift enforcement, read-only diagnostics, migration backups, no-write adoption previews, preservation of existing instructions, project-owned output handling, hash-verified stale pruning, planned-goal authority boundaries, approval separation, transaction rollback, concurrent migration, documentation links, and source-hint drift reports.

```powershell
$env:PYTHONPATH='src'
python -m tasktra compile --root . --check
```

Result: exit 0 with no missing, changed, locally edited, or stale managed files. A separate end-to-end acceptance run initialized two fresh projects and confirmed byte-identical manifests and lockfiles; every generated-file hash matched its on-disk bytes.

The initialization acceptance fixture confirmed that preview creates no `.tasktra` directory, `--apply` preserves existing top-level instructions, and unmanaged projection collisions fail closed. Runtime state is excluded by `.gitignore`.

## Review closure

The initial independent review identified unsafe path possibilities, stale ownership ambiguity, incomplete Stage 1 authority constraints, approval cross-goal and self-approval risk, transaction and migration race risk, record/schema mismatch, and incomplete source identity. The implementation now:

- validates every output path before any write and rejects traversal or symlink ancestors;
- distinguishes project-owned extras, source-driven regeneration, local edits, and stale managed files;
- prunes stale files only with an explicit flag and only when their bytes match the prior ownership record;
- keeps Stage 1 goals planned-only and enforces approval goal membership and separation of duties;
- performs state mutations and audit events in one transaction and serializes migrations;
- validates persisted goals, work units, and approvals against bundled contracts; and
- locks catalog content, enabled packs, per-pack versions, schema versions, and generated output identity.

The independent acceptance audit passed 13 of 14 criteria before this report; the sole remaining criterion was recording this evidence and updating requirements coverage. No functional blocker remained.

## Known limitations

- The symlink rejection test is present but could not execute on this Windows host without symlink privilege. The containment checks are exercised by traversal tests and the CI matrix is configured for Windows, macOS, and Linux.
- Projection files are individually staged and atomically replaced; an interrupted multi-file compile is recoverable by rerunning, but it is not a single filesystem transaction.
- The ledger is intentionally planned-only until the Stage 3 authority envelope and recovery model are delivered.
- Cross-platform CI is configured but its hosted-run evidence belongs to the release stages.

## Decision

All Stage 1 criteria are satisfied with the limitations above explicitly carried forward. Stage 2 may implement useful local workflows without broadening the planned-only authority boundary.
