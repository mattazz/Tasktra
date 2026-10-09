# ADR 0013: Anchored dependency impact

## Status

Accepted after independent architecture review, 2026-10-08.

## Context

The direct-prerequisite view explains one dependency gate, but large work graphs
also need upstream blocker tracing and downstream impact inspection. Work units
have no duration estimates, so the graph cannot support time-based critical-path
or delivery-date predictions.

## Decision

Add a read-only projection for one selected unit in one goal. A focused
`dependency_impact` module reuses the existing verified graph reader inside one
SQLite read transaction. It traverses prerequisite and reverse edges iteratively
to compute unique ancestors, dependents, and minimum edge distances.

Report incomplete prerequisite ancestors and direct prerequisite gates that
would change from false to true if the selected unit completed. The latter is
a structural hypothetical, including dependents whose own status prevents
execution. An already complete anchor yields zero. Full claim authorization
remains the responsibility of the existing explanation and claim operations.

Compute counts before filtering and pagination. Order relation details by
direction, distance, and identifier; return identifiers, statuses, checkpoint
identifiers, counts, and booleans without titles or scopes. Preserve existing
dependency and explanation responses.

## Consequences

One anchored request requires linear graph work plus sorting its related units.
There is no all-pairs reachability matrix, global priority score, persistent
cache, or schema change. Repeated anchor inspections repeat validation and graph
loading; measurement should precede any caching design.

The projection reports one coherent ledger snapshot and fails without partial
results when graph or authority integrity cannot be established. Bounded detail
pages make large graphs inspectable without implying permission to execute work.
