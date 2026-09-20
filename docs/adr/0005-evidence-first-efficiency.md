# ADR 0005: Evidence-first token-efficient orchestration

## Status

Accepted.

## Context

High-quality orchestration can waste tokens through duplicate exploration, oversized context, and indiscriminate escalation.

## Decision

Optimize after correctness, safety, and required validation. Store evidence once, reference it compactly, invalidate it deterministically, and route work through deterministic tools and narrow retrieval before broader reasoning. Record measured usage locally where available and test representative workflows against efficiency regressions.

## Consequences

Roles require compact evidence contracts and routing discipline. A low-cost attempt that causes rework is treated as a failure of total efficiency.
