# ADR 0001: Codex-first, runtime-neutral architecture

## Status

Accepted.

## Context

Tasktra must be easy to operate conversationally while avoiding a permanent dependency on a single runtime's file format or model API.

## Decision

Codex is the primary human and orchestration interface. Canonical roles, workflows, contracts, and configuration are runtime-neutral. A Python CLI provides deterministic operations. Codex and Claude consume generated projections; direct model API invocation is deferred behind a future runner interface.

## Consequences

The first runtime experience is optimized for Codex, while a supported Claude projection prevents the canonical model from becoming provider-specific. The compiler and projection tests are critical compatibility boundaries.
