# Stage 4 completion report — Remote capabilities

Status: complete on 2026-09-20. Stage 5 is eligible and active.

## Delivered outcome

Tasktra now has optional, provider-neutral Git, GitHub, and Jira capabilities without weakening its local-first baseline. Protocol-v2 authority binds every protected operation to an exact provider, capability, action, effect class, resource scope, performer, approval, live work attempt, and idempotency key. `ProviderEffectExecutor` is the only supported protected-write composition: it claims the durable dispatch slot, invokes a closed registered adapter with the persisted request, and records a sanitized result.

The schema-v8 ledger stores immutable per-attempt dispatch, receipt, and reconciliation history. Receipts and reconciliations are atomic and current-attempt-bound; raw callers cannot assert adapter absence; repeated retry cycles remain attestable; ambiguous legacy histories fail migration without partial mutation. GitHub and Jira non-idempotent writes never treat a zero-match snapshot as retry-safe absence. Git push is the sole built-in retryable absence case because exact expected-old/new object IDs and compare-and-swap semantics prevent duplicate application.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -q
```

Result: 238 tests passed in the independently reviewed source. Three symbolic-link tests skipped because this Windows account lacks symbolic-link privilege; Windows junction, path containment, hook isolation, and process-tree tests passed.

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_stage4_adapters tests.test_stage4_authority tests.test_stage4_cli tests.test_stage4_effect_ledger tests.test_stage4_provider_execution tests.test_stage4_providers -q
```

Result: the complete Stage 4 provider, authority, executor, CLI, ledger, and adapter surface passed. The adapter-only suite passed 21 tests, including live Windows descendant cleanup and fail-closed suspended launch.

```powershell
$env:PYTHONPATH='src'
python -m tasktra compile --root . --check
python -m tasktra audit --root . verify --limit 64
```

Result: canonical Codex, Agents, and Claude projections are clean at Tasktra/catalog/core 0.4.0 with runtime schema 8. Before the self-hosted Stage 4 completion transition, all 20 live audit events verified with no issue.

## Provider safety and failure semantics

- Git reads and writes bind a stable local repository fingerprint and exact ref semantics. Commits publish an already-validated index tree through `commit-tree` plus atomic `update-ref` compare-and-swap.
- Git push is HTTPS-only in the safe default, resolves and rechecks the exact authorized destination, disables hooks and inherited push expansion, rejects transport-affecting repository config and URL rewrites, and uses an exact refspec with `--force-with-lease`.
- GitHub reads and reconciliation are bounded. Comments, reviews, issues, pull requests, and statuses carry idempotency markers, but absence never unlocks an automatic retry because bounded or eventually consistent snapshots cannot prove it safe.
- Jira request targets and payload sizes are scope-bound. Timeout, transport failure, malformed post-write responses, and absence remain indeterminate; applied/conflict observations can close recovery without enabling duplicates.
- Windows provider processes launch suspended, enter a kill-on-close Job Object, and resume only after containment succeeds. Assignment or resume failure returns undispatched without executing the provider command.
- Credential-shaped fields, values, URLs, mapping keys, non-string keys, and non-JSON containers are rejected or redacted before durable persistence. Provider outputs and host health snapshots are bounded to 64 KiB.

## Capability degradation and Codex operation

Missing GitHub or Jira capability is explicit and never blocks unrelated local work. `tasktra capabilities` defaults to credential-free offline health. A connected Codex or connector host may supply a strict, bounded, one-invocation provider-health report; that report is labeled host-reported and never grants effect authority.

The generated `tasktra-remote` procedure permits CLI preparation and inspection but does not expose a manual begin/call/receipt gap. A protected write must use a configured registry-backed `ProviderEffectExecutor`; otherwise the intent stays pending. Manual reconciliation can record only applied or conflict recovery, never absence.

## Migration, self-hosting, and efficiency observations

The Tasktra repository's own runtime was explicitly migrated from schema 7 to schema 8. A recoverable `.v7.bak` snapshot was retained, 18 current authoritative rows were human-attested by `matt`, and the state manifest remained `70f0844d2e39b0912504eec2c9c7d37bcbfff5532667218175c7c7becbb1e334`.

Provider routing and validation remain deterministic. Requests, observations, status details, command output, and host reports have fixed bounds; evidence is referenced rather than replayed; local work does not spend remote calls merely to discover that an optional provider is unavailable. No token-savings percentage is claimed because comparative local telemetry belongs to Stage 6.

## Independent review closure

Independent review initially found eight source blockers: a public provider-write bypass, incomplete approval scope checks, credential leakage through nested containers and keys, cross-lease attestation failure, dropped CLI resource scope, incomplete Git destination binding, non-atomic registration, and stdin writes outside the timeout. Subsequent adversarial review found atomicity, retry-ordering, transport, exact-scope, Git hook/config, Windows process-tree, and migration edge cases. Every finding received a regression and was re-reviewed.

The final verdict was GO with no unresolved source or security blocker. Independent checks passed the 67-test focused surface and the 238-test full suite, plus adversarial legacy-migration, duplicate-receipt, active-dispatch race, chained URL rewrite, repository-hook, Jira uncertainty, and Windows descendant-sentinel cases.

## Known limitations

- Three symbolic-link tests require a Windows privilege unavailable to this account; equivalent traversal and junction protections pass locally and the tests remain for privileged Windows and POSIX CI.
- Safe-default Git push accepts HTTPS destinations only and rejects repository-controlled credential helpers or transport customization. Authenticated execution requires a trusted injected host runner.
- GitHub and Jira non-idempotent writes do not automatically retry after an uncertain outcome. Reconciliation can prove applied/conflict; otherwise human recovery is required.
- A schema-v7 database with genuinely ambiguous legacy reconciliation history fails migration visibly and requires manual repair rather than guessed authority.
- Ecosystem packs, upgrade lifecycle, efficiency telemetry, scheduling, and cross-platform release proof remain Stages 5–7 work.

## Decision

All Stage 4 criteria are satisfied. Optional remote work is authority-bound, scope-exact, credential-isolated, durable, conservative under uncertainty, and non-blocking for local development. The live self-hosting ledger is schema 8 and audit-clean; Stage 5 may add specialist and ecosystem packs without reopening the provider authority boundary.
