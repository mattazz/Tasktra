# Stage 2 acceptance checklist — Local workflows

Stage 2 makes Tasktra useful for bounded software work without online services. It may create explicitly requested local work artifacts and run configured local checks, but it does not activate autonomous goals or authorize remote effects.

## Scope

- Versioned structured handoffs with concise human summaries and attributable evidence.
- Project-scoped Markdown work items with deterministic, conflict-safe operations.
- Preview-first execution of project-owned validation commands without shell interpretation.
- Core role dispatch and review loops that use bounded briefs and validated handoffs.
- Git workspace inspection and safe worktree guidance that preserve unrelated changes.
- Local capability reporting and clear degradation when optional tools are absent.

## Acceptance criteria

- [x] Handoff envelopes validate version, status, facts, inferences, changed paths, checks, evidence, blockers, downstream context, and requested actions.
- [x] Invalid or oversized handoffs fail before they can advance a workflow.
- [x] Local work items can be created, read, listed, and version-safely updated without a remote account.
- [x] Work-item identifiers, paths, concurrent updates, and symlink boundaries fail closed.
- [x] Validation commands are previewed by default and execute directly only after an explicit run request.
- [x] Validation stops on failure or timeout and returns bounded evidence without shell interpretation.
- [x] Core role dispatch chooses the smallest suitable role and emits a compact downstream brief.
- [x] An implement-test-review workflow validates every handoff before transition and preserves failed evidence.
- [x] Git inspection reports repository, branch, revision, dirty state, and worktree suitability without modifying Git state.
- [x] Unrelated dirty changes cause isolation guidance or escalation, never silent overwrite or cleanup.
- [x] Missing Git or optional online capabilities remain visible and do not block non-Git local workflows.
- [x] Codex-facing skills and deterministic CLI entrypoints cover handoffs, work items, validation, status, and diagnostics.
- [x] Unit, integration, adversarial contract, documentation-link, and generated-output tests pass.
- [x] Independent semantic and acceptance reviews find no unresolved blocker.
- [x] Requirements coverage and a Stage 2 completion report are current before closure.

## Exit evidence

Record exact commands and outcomes, fixture scenarios, changed contract versions, known limitations, token-efficiency design choices, independent findings, and the Stage 3 eligibility decision.

The recorded evidence and eligibility decision are in the [Stage 2 completion report](stage-2-completion.md).
