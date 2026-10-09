# ADR 0014: Offline operator cockpit

## Status

Accepted after independent architecture review, 2026-10-08.

## Context

Operators need to move from project attention to goal progress and individual
work without assembling several command responses. Existing overview and impact
projections provide the necessary facts, but separate calls can observe different
ledger states. A live service would add process, origin, and refresh concerns.

## Decision

Export a self-contained HTML file from one verified read-only SQLite transaction.
Share the connection-scoped overview builder with the existing CLI projection.
Capture a fixed clock, audit head, and state-manifest identity. Label filesystem
observations separately and keep the capture's static nature visible throughout.

Retain ordinary SQLite locking and WAL snapshot behavior. Read-only means no
durable Tasktra transition or SQL write. SQLite may create an empty WAL and
create or update SHM reader coordination; these files are not manually removed.
Tests protect main database bytes and existing WAL transaction bytes in isolated
fixtures, and verify coherent snapshots separately under concurrent writes.

Use a pure renderer with fixed local CSS and JavaScript, deterministic CSP hashes,
escaped JSON data, and text-only insertion of captured values. The artifact needs
no network, server, framework, persistent cache, or runtime schema change.

Bound goal, unit, edge, JSON, and HTML sizes. Preserve every attention goal within
the goal limit and explicitly count omitted normal goals. Transport adjacency
only for completely captured goal graphs. Compute one selected unit's impact
with iterative browser traversal, verified against the Python projection.
Incomplete graphs expose stored facts and exact CLI guidance without derived
readiness or partial blocker claims.

Represent command guidance once as a captured interpreter/source context and
five closed read-only templates. The page expands only selected identifiers and
validated page values. Structural readiness never implies execution authority.
Lifecycle and claim operations remain in the existing control plane.

Publish only a new `.html` file through an exclusively created sibling temporary
file and atomic no-replace hard link. Flush and close before publication. Refuse
unsupported filesystems and destination collisions; remove only the temporary
file owned by the export.

## Consequences

All dashboard views describe one coherent capture, and identical snapshot inputs
produce identical HTML bytes. Operators regenerate with a new filename for fresh
data. Large projects may require CLI drilldown where capture limits omit detail.

The browser duplicates a bounded structural traversal, making cross-language
parity tests necessary. File-origin behavior and accessibility require browser
verification. Local names, paths, and relationships remain in the artifact even
though private authority, evidence, provider payloads, and credentials are omitted.

Live refresh, multi-project aggregation, and mutation controls need separate
designs justified by observed operator workflows.
