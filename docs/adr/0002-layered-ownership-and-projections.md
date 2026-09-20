# ADR 0002: Layered ownership and generated projections

## Status

Accepted.

## Context

Projects need both a usable generic baseline and safe customization/upgrades.

## Decision

Separate versioned core, committed project definition, curated project knowledge, and ignored runtime state. Resolve contributions in the order core, enabled packs, project profile, and project extensions. Generate runtime projections with hashes and a lockfile. Do not hand-edit generated outputs; provide drift detection and an explicit takeover path.

## Consequences

Upgrades can preview managed changes without overwriting project-owned files. Conflicts are explicit. The system needs manifests, migration support, and clear user-facing ownership messages.
