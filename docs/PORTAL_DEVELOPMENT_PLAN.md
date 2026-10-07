# Portal development plan

Status: in progress. The human authorized phased implementation, validation, and a push to `main` at each completed phase. The working branch is `codex/portal-roadmap`, starting at `f4fe3f62b41d64b23b2e7122b815bc2849e1293c`.

## Outcome

Make the portal answer what needs attention, where execution time goes, and what verified work has been delivered. Keep existing goals, job filters, agent activity, usage analytics, and the Cytoscape relationship map usable throughout delivery.

## Delivery phases

| Phase | Deliverable | Acceptance checkpoint |
| --- | --- | --- |
| 1. Attention and diagnostics | An actionable attention queue; runtime/build and telemetry coverage information; retained validation reports. | Issues link to their exact records, missing data is distinct from failure, scoped issues respect goal selection, diagnostics label project-wide information, and validation results survive a failed run. |
| 2. Execution visibility | A recorded execution timeline; selected-agent tool spans where exact starts and finishes exist; clearer map identities, activity presets, and saved manual positions. | Parallel work and recorded durations can be inspected without implying unobserved activity. Map search and canonical relationships remain correct, active/failed presets are explicit, and saved positions remain isolated to the project and scope. |
| 3. Results and efficiency | Job evidence summaries and safe deliverable references; recorded attempts, durations, and usage per verified completed job. | Evidence is attributable to exact jobs and acceptance records; missing links/timing remain unknown; metrics reconcile to recorded usage, exclude unmatched work from denominators, and disclose coverage and sample sizes. |
| 4. Saved workspaces | Named saved views, validated restorable links, and side-by-side agent comparison. | Restoring a view preserves its intended scope and filters, malformed or stale links have clear fallbacks, projects and demo data remain isolated, and two exact runs can be compared without mixing identities or totals. |

Each phase includes its documentation, focused regression coverage, and applicable browser checks. Each phase is a separate reviewed commit pushed to `main`; later phases build on the previous published checkpoint.

## Shared constraints

- The portal remains local, offline-capable, and read-only. Viewing an issue or evidence never retries work, changes authority, or modifies project state.
- No database migrations or changes to the Friendsmas game are required. Preserve the unrelated edits in the primary Tasktra checkout.
- Only explicit recorded IDs create project, goal, job, execution, and parent relationships. Titles and roles never establish assignment.
- Public progress, tool names, and recorded events may be displayed. Private analysis, prompts, tool arguments, raw tool output, credentials, and unrestricted project files are excluded.
- Distinguish observation windows, complete durations, ongoing work, and missing timestamps. Silence does not establish inactivity.
- Bound reads, responses, retained browser state, and rendered collections. Show partial coverage and truncation.
- Render untrusted strings as text. Any evidence links must use a narrow safe scheme/provider policy; no arbitrary file-serving endpoint.
- Preserve keyboard access, reduced-motion behavior, small-screen controls, selected records, and camera position during live updates.
- Use existing Python and bundled browser code first. Add dependencies only where a concrete need warrants them.

## Phase architecture

Phase 1 adds a versioned, bounded `insights` document to the existing snapshot and an opaque project key for browser-state isolation. Attention and diagnostics are explicit safe projections. Validation reporting writes only when an authorized validation command runs, never when the portal reads its report.

Phase 2 builds timeline rows from recorded timestamps and adds exact tool-event correlation only within the already verified selected-agent activity boundary. Unknown intervals remain labeled. The map continues to project canonical identities and edges.

Phase 3 reads recorded workflow/acceptance evidence through an allowlisted summary rather than exposing arbitrary audit payloads. Outcome metrics include their attributable execution set, timing/usage coverage, and verification definition. Model comparisons describe comparable recorded groups, not controlled token savings.

Phase 4 persists bounded, versioned browser preferences under the project key. Links contain selected record IDs and view settings, never local paths or private transcript content. Comparison panels display independent per-run measurements and coverage.

## Checkpoint procedure

1. Complete scoped backend and frontend units against an agreed contract.
2. Run focused behavioral and API/privacy checks, plus real browser flows with representative and actual Friendsmas data.
3. Preview and run the configured checks: `python -m unittest discover -s tests` and `python -m tasktra compile --root . --check --trust-catalog`.
4. Obtain independent review of the final source and evidence. Resolve findings before freezing hashes.
5. Commit only the phase's reviewed paths. Push the feature branch through the recorded Git adapter and wait for all nine exact-commit CI jobs (three operating systems by three Python versions).
6. Advance `main` only by an expected-old, verified fast-forward push. Record the commit and CI run below.
7. Install the exact published package in Friendsmas using a checksum-bound package-only preview, distinct backups, and a guarded rollback. Retain every validation result before raising errors. Run the Friendsmas configuration and game checks, restart only the verified portal process, and verify the live phase.
8. Record acceptance and advance the phase checkpoint. Continue to the next phase without a new permission request while scope remains covered by the user's instruction.

Failed validation stops the checkpoint. A rolled-back installation retains its original receipt and diagnostics; recovery is recorded explicitly, and a justified retry uses a new effect identity. Never overwrite unrelated work or bypass a failed required check.

## Release record

| Phase | Status | Reviewed commit / CI | Live validation |
| --- | --- | --- | --- |
| 1 | Locally validated; publication checkpoint in progress | 638 tests, 17 platform skips; generated files clean; 9 UI contracts | 13 fixture browser scenarios and 7 actual-project preview checks passed |
| 2 | Planned | Pending | Pending |
| 3 | Planned | Pending | Pending |
| 4 | Planned | Pending | Pending |

Phase source commits carry their completed local validation notes. Publication and live-install receipts remain in the project's ignored runtime evidence, and the next phase's plan update records the preceding release identifiers.

### Phase 1 local checkpoint

Implemented the bounded attention queue, exact-record navigation, scoped diagnostics, packaged build identity, and retained per-run validation reports. Independent review drove regression fixes for malformed report data, bounded reads, canonical run identity, polling focus, and refreshed record labels. Reports keep failure evidence and distinguish partial output; portal responses expose only safe metadata. The full configured checks passed, followed by focused UI and browser checks on the final interface changes.
