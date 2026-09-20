# Stage 2 completion report — Local workflows

Status: complete on 2026-09-19. Stage 3 is eligible.

## Delivered outcome

Stage 2 provides a useful offline software-development workflow. Tasktra now has strict versioned handoffs, deterministic core-role routing, bounded downstream briefs, project-scoped Markdown work items, preview-first validation, an implement-test-review gate, read-only Git workspace assessment, and visible optional-capability degradation. Codex-facing skills use installed CLI interfaces rather than source-checkout paths.

The authority boundary remains planned-only. Workflow completion tokens prove structural integrity of local evidence but do not authorize external effects; durable authority, leases, and recovery belong to Stage 3.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```

Result: 104 tests passed and three symlink tests skipped because this Windows account lacks symbolic-link privilege. The unprivileged Windows junction regression passed. The suite covers versioned handoffs, evidence attribution, size and path limits, exact shared identifiers, persisted-invalid diagnostics, routing-budget closure, deep-copy isolation, workflow transition integrity, reviewer independence, completion gating, Markdown concurrency, junction and symlink containment, canonical argument arrays, shell-injection resistance, bounded live output, descendant termination on timeout, workspace guidance, capability degradation, documentation links, and generated-output determinism.

```powershell
$env:PYTHONPATH='src'
python -m tasktra validate --root . --run --timeout 60
```

Result: exit 0. Both configured commands passed: the complete unit suite and `python -m tasktra compile --root . --check`.

```powershell
$env:PYTHONPATH='src'
python -m tasktra compile --root . --check
```

Result: exit 0 with no missing, changed, locally edited, stale, project-owned, or metadata-drift findings.

Manual acceptance also confirmed that dirty Git state yields isolation or escalation guidance without mutation, and that unavailable GitHub and Jira capabilities remain visible while local work continues.

## Contracts and efficiency

Tasktra remains at `0.2.0`. Stage 2 adds schema version 1 for handoffs, routing, work items, workflows, and workflow completion tokens; runtime state remains schema version 2. All durable references use one lowercase-slug contract with a 64-character maximum and no silent normalization.

Token and context efficiency are enforced structurally: list operations return summaries, handoffs and workflow files have pre-parse byte limits, validation output is bounded while processes run, routing is deterministic, briefs retain only complete evidence-backed facts within an eight-reference budget, and downstream roles receive concise evidence-linked context instead of replayed transcripts.

## Independent review closure

The first semantic review found junction and symlink escapes, completion bypasses, descendant-process leakage, platform-dependent command parsing, aliased or unsafe routing evidence, unbounded inputs and output, source-checkout-only skills, and inconsistent identifiers. Each issue received a regression and was re-reviewed.

The final semantic review found no correctness or security blocker. It specifically verified evidence-backed completion for every role, evidenced reviewer disposition, independent review identity, shared identifiers, internal-target symlink and Windows junction rejection, safe routing locators and closed evidence budgets, descendant termination, canonical argument execution, bounded contracts, and operational CLI-backed generated skills. The independent functional audit passed criteria 1–13; this report and the updated requirements matrix satisfy criteria 14–15.

## Known limitations

- Three POSIX-style symbolic-link tests could not execute under this Windows account; they remain in the cross-platform suite, while equivalent traversal and Windows junction paths pass locally.
- Windows process-tree cleanup uses a kill-on-close Job Object and falls back to `taskkill /T` if policy blocks Job assignment. Processes outside the caller's permissions cannot be guaranteed removable.
- Workflow state is explicit JSON rather than durable leased runtime state. Tokens are structural checksums, not authority credentials. Stage 3 must make the SQLite ledger authoritative before autonomous execution.
- Workspace handling is read-only guidance in this stage. Actual isolated-worktree lifecycle belongs to Stage 7.
- GitHub and Jira are reported as unavailable optional capabilities until Stage 4 adapters are configured.

## Decision

All Stage 2 criteria are satisfied. The local baseline is useful without remote accounts, its evidence and execution boundaries fail closed, and no independent-review blocker remains. Stage 3 may implement durable autonomous execution without weakening the local safety gates.
