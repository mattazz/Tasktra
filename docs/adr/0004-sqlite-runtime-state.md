# ADR 0004: SQLite runtime state

## Status

Accepted.

## Context

Goals, leases, budgets, approvals, and audit records need atomic updates and recovery without requiring another service.

## Decision

Use local SQLite, accessed through Python's standard library, as the runtime authority. Keep an append-only audit journal and provide human-readable exports. Backup before migrations and expose integrity and repair operations that never silently discard records.

## Consequences

Tasktra supports portable transactional recovery and offline work. Cross-machine coordination remains an adapter concern rather than an assumption of the local ledger.
