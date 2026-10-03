# Tasktra program review — 2026-10-02

This review covers the program at baseline `7fe095e`, the local progress portal,
and the corrections recorded in the Unreleased changelog. It combines the full
discovered test suite with independent source review, isolated Git reproductions,
failure injection, and browser checks. A passing review is evidence for the
tested behavior, not proof that every possible program state is defect-free.

## Findings addressed

| Area | Finding | Correction and regression evidence |
| --- | --- | --- |
| Git authorization | Commit requests could name staged files outside an approved work scope. | Check requested paths against envelope, work-unit, and human-approval scopes at preparation, dispatch, and retry; reject exclusions before invoking the adapter. |
| Read-only Git | Diff text converters could execute repository-configured commands. | Both diff forms disable text conversion; a real Git fixture verifies that a malicious converter is never invoked. |
| Initialization | Linked `.tasktra` directories could redirect writes, and concurrent creation could overwrite a profile. | Reject links/reparse points and publish the completed profile exclusively; test junctions, links, and competing creation. |
| Configuration | Certain project names produced invalid TOML. | Preserve quotes, backslashes, control characters, DEL, and emoji with round-trip tests. |
| Metadata | Windows junctions could redirect manifest, lockfile, snapshot, or receipt writes. | Validate metadata ancestors before and after directory creation. |
| Compile recovery | A failed multi-file update could leave partial output or deleted stale files. | Restore captured generated files and metadata on caught failures; revalidate root identity and path topology before recovery writes. |
| Upgrade recovery | Failures after a database commit could misleadingly report a file rollback. | Preserve recovery-required state and exact database backup evidence through finalization, interrupts, capture failures, and receipt failures; detect concurrent schema advancement against the preview; validate bounded prepared journals and expose a closed recovery payload in the CLI. |
| Validation | A child retaining output pipes could block after its parent exited; a POSIX child ignoring termination could survive. | Clean up the owned Job/process group before closing streams and escalate against the group independently of parent exit. |
| Portal degradation | A malformed optional execution ledger could hide healthy runtime workers. | Fall back to runtime leases with one warning, retaining accurate worker counts. |
| Test selection | The configured list omitted Jira and discovery-skill tests. | Use test discovery so current and future test modules are included. |

The security, configuration/validation/portal, and lifecycle corrections received
independent re-review. Follow-up recovery findings were corrected before the
final verification run.

## Verification scope

- `python -m tasktra validate --root . --run --timeout 300` runs the complete
  discovered test suite and checks generated projections against canonical sources.
- Packaging tests build reproducible wheels/source archives, install into an
  isolated target, exercise adoption and compilation, check bundled portal assets,
  and verify that removing the distribution preserves project state.
- Browser checks cover overview/goals/jobs/agents, search and filtering, record
  details, demo transitions, refresh, loss/recovery of connectivity, motion
  controls, and the responsive phone layout.
- JavaScript syntax, frontend formatting, and Git whitespace checks pass.
- The CI matrix runs Windows, macOS, and Linux on Python 3.11, 3.12, and 3.13.
  Consult the commit's GitHub Actions run for hosted execution results; the
  workflow definition alone is not a test result.

## Boundaries and remaining limitations

- Platform-specific skips are reported explicitly. Local execution used Windows
  and Python 3.11; hosted CI supplies the other platform/version results.
- When a managed Windows host denies Job Object assignment and a parent exits
  early, fallback process-tree discovery cannot reliably find every descendant.
  Retained output pipes cause validation to fail instead of claiming verified
  cleanup. Trusted validation commands are not an operating-system sandbox.
- Provider behavior is tested with isolated Git repositories and provider
  fixtures. This review does not perform live Jira mutations or deploy software.
- Portal states are recorded observations. A started receipt without a linked
  lease is not proof of a currently running operating-system process.
- The formal signed 1.0 release decision and remaining Stage 7 release evidence
  are still open. Updating `main` does not publish a package or close that gate.
