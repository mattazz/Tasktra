# Tasktra program review — 2026-10-03

This review covers the program, the local progress portal, and the corrections
recorded in the Unreleased changelog. The first correctness review used baseline
`7fe095e`; the architecture and execution follow-up used `6e90251`. It combines the full
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
| Validation | A child retaining output pipes could block after its parent exited; a POSIX child ignoring termination could survive. | Clean up the owned Job/process group before closing streams and escalate against the group independently of parent exit. Verify absent or zombie-only groups when macOS denies a signal, and report unresolved cleanup as failure. |
| Portal degradation | A malformed optional execution ledger could hide healthy runtime workers. | Fall back to runtime leases with one warning, retaining accurate worker counts. |
| Test selection | The configured list omitted Jira and discovery-skill tests. | Use test discovery so current and future test modules are included. |

The security, configuration/validation/portal, and lifecycle corrections received
independent re-review. Follow-up recovery findings were corrected before the
final verification run.

## Architecture and execution follow-up

The architecture remains a modular Python/SQLite application. That is a useful
fit for a local control plane: authority, work ownership, budgets, and audit
evidence can commit together without introducing a distributed transaction.
Pure workflow rules, host execution, process cleanup, and patch transport now
have separate interfaces. A service split would add operational complexity
without evidence of a scale requirement.

| Finding | Resolution |
| --- | --- |
| Work plans and receipts did not dispatch actual agents. | `tasktra run` previews or executes one approved unit through the installed Codex CLI, with fresh stage threads, persisted host results, validation, and reviewed patch publication. |
| All successful work required a software implementation pipeline. | Immutable implementation, research, documentation, and deterministic verification policies preserve review appropriate to each work type. Non-default policies need explicit envelope permission. |
| Contract replacement and terminal work could strand a goal. | Replacement validates existing scopes, checkpoints, and policy permissions. Failed/exhausted work can be requeued with current authority and remaining budgets. Status separates lifecycle from execution health. |
| Ambiguous upgrade/local effects could look terminal. | Indeterminate and recovery-required effects remain outstanding until an exact, evidence-backed recovery decision is recorded; original receipts remain available. |
| Process supervision was duplicated and could miss descendants. | A shared argv runner owns bounded output, cancellation, Job/process-group cleanup, and failure reporting for providers, migrations, validation, and Codex execution. |
| Concurrent telemetry could lose writes; benchmark verdicts could punish improved correctness. | OS-owned locks serialize complete read/update/write operations and release on crash. Benchmark comparison gates efficiency on verified quality. |
| Hosted Windows/macOS paths differed from their canonical root spelling. | Lock containment accepts targets beneath the supplied or canonical root without resolving project-controlled target components. Workspace setup accepts aliases above that root while retaining checks on project-controlled paths. Real alias fixtures cover this boundary. |
| A worker could affect later review through runtime state, instruction changes, or ignored files. | Workers use detached clones without a remote. Reserved control paths are rejected, later stages receive only the retained patch, and publication checks the original baseline. |
| Over-budget host turns and missing usage could corrupt accounting. | Known overage is reconciled against host receipts and charged as budget debt; missing usage is explicitly labelled and conservatively charged. Configured model pins remain requests, not falsely observed models. |

## Usefulness and product boundaries

Tasktra is most useful for consequential, multi-session agent work that benefits
from explicit authority, durable ownership, independent review, and recovery.
The single-unit runner removes manual stage dispatch and evidence plumbing, but
initial goal/envelope approval still requires deliberate setup. A short solo
edit may not justify that overhead. There is no paired benchmark demonstrating
a general token, cost, or wall-time advantage over ordinary Codex use.

The implemented runner is explicit and bounded. It does not supply an unattended
multi-project daemon. Jira and scheduler discovery/planning are not claims of
live mutations or schedule creation. See [execution](EXECUTION.md) for the exact
supported checkout, scope, approval, and recovery contract.

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
- Real Codex CLI smoke execution verified fresh thread events, a schema-bound
  response, and observed usage against a temporary read-only Git fixture. The
  multi-stage integration tests use controlled hosts and real Git clones;
  they are not presented as live model-quality benchmarks.

## Boundaries and remaining limitations

- Platform-specific skips are reported explicitly. Local execution used Windows
  and Python 3.11; hosted CI supplies the other platform/version results.
- Windows processes are assigned to an owned Job before execution is resumed;
  assignment or cleanup failures fail the invocation. Trusted project validation
  commands are full-host code, not an operating-system sandbox.
- Provider behavior is tested with isolated Git repositories and provider
  fixtures. This review does not perform live Jira mutations or deploy software.
- Portal states are recorded observations. A started receipt without a linked
  lease is not proof of a currently running operating-system process.
- The formal signed 1.0 release decision and remaining Stage 7 release evidence
  are still open. Updating `main` does not publish a package or close that gate.
