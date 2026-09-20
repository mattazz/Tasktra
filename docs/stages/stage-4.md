# Stage 4 acceptance checklist — Remote capabilities

Stage 4 adds optional Git, GitHub, and Jira providers behind explicit capability and authority boundaries. Local development must remain fully useful when every remote provider is unavailable.

## Scope

- A provider-neutral capability contract and registry.
- Read-only and mutating Git operations with explicit repository boundaries.
- GitHub issue, pull-request, review, and status capabilities.
- Jira issue discovery, transition, and comment capabilities suitable for Codex orchestration.
- External credentials supplied by the host environment and never stored in Tasktra state or artifacts.
- Idempotency keys, durable intents, sanitized receipts, retry classification, and reconciliation for remote effects.
- Deterministic offline mocks and visible degraded-capability reporting.

## Acceptance criteria

- [x] Provider contracts separate discovery, read-only operations, and protected remote effects.
- [x] The capability registry reports available, unavailable, misconfigured, and degraded providers without exposing credentials.
- [x] Git operations are repository-contained, use argument arrays, preserve unrelated work, and never push implicitly.
- [x] GitHub and Jira adapters support bounded reads and separately authorized writes through replaceable provider interfaces.
- [x] Remote writes require an exact current authority envelope, effect class, scope, performer approval, and idempotency key.
- [x] Pending, succeeded, failed, indeterminate, and reconciled effects have durable sanitized receipts.
- [x] Retries cannot duplicate comments, transitions, issues, pull requests, or pushes.
- [x] Credentials remain outside project files, logs, handoffs, audit payloads, and receipts.
- [x] Offline fixtures and provider fakes cover success, throttling, timeout, malformed response, partial failure, and replay.
- [x] Missing GitHub or Jira access does not block local work or hide capability loss.
- [x] Codex-facing procedures prefer connected capabilities and can orchestrate an optional CLI fallback.
- [x] Status and diagnostics remain concise, bounded, and actionable.
- [x] Independent security, semantic, and acceptance reviews find no unresolved blocker.
- [x] Requirements coverage and a Stage 4 completion report are current before closure.

## Exit evidence

Record exact commands and outcomes, provider contract fixtures, credential-redaction checks, idempotency and reconciliation results, local fallback behavior, failure classification, known limitations, measured efficiency observations, independent findings, and the Stage 5 eligibility decision.
