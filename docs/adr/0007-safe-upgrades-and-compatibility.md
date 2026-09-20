# ADR 0007: Safe upgrades and compatibility

## Status

Accepted.

## Context

Tasktra must evolve without trampling a consuming project's own workflow additions.

## Decision

Use semantic versioning, versioned schemas, lockfiles, previewable migrations, checksums, and managed/project-owned boundaries. Do not perform unattended upgrades. Preserve or compose existing instructions during adoption and require review before replacing them.

## Consequences

Compatibility and migration tests are release requirements. Projects can choose an ownership escape hatch at the cost of managing the component themselves.
