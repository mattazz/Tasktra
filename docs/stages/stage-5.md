# Stage 5 acceptance checklist — Specialist and ecosystem packs

Stage 5 turns the generic orchestration core into a composable software-development template family without making project detection mutate a repository or silently changing authority.

## Scope

- A discoverable specialist catalog spanning architecture, application, frontend, backend/API, data/migration, integration, test automation, end-to-end behavior, reliability/observability, developer experience, refactoring, product, delivery, operations, and knowledge work.
- Ready-to-use generic, Python, TypeScript/web, and monorepo packs.
- Detection-assisted, preview-only pack recommendations based on bounded local evidence.
- Deterministic pack composition, dependencies, incompatibilities, capability requirements, and project-owned extensions.
- Versioned pack contracts and migration validation suitable for the Stage 6 upgrade lifecycle.

## Acceptance criteria

- [x] Every specialist role has an explicit trigger, owned responsibility, input/output contract, stop conditions, validation expectations, and token-efficient evidence policy.
- [x] Core routing selects the smallest qualified role or specialist set and preserves independent tester/reviewer separation where required.
- [x] Generic software-development pack works without ecosystem-specific tools or online services.
- [x] Python pack provides bounded detection, validation defaults, and specialist routing without overwriting project configuration.
- [x] TypeScript/web pack provides bounded detection, validation defaults, and frontend/backend/test routing without assuming one framework.
- [x] Monorepo pack models package boundaries, affected scopes, validation fan-out, and isolation without scanning or loading unrelated packages unboundedly.
- [x] Pack recommendation is preview-only, evidence-backed, deterministic, and makes uncertainty visible.
- [x] Pack dependency order and composition are deterministic; duplicate ownership and incompatible contracts fail visibly.
- [x] Required capabilities are reported before activation, and missing optional capabilities do not block unrelated local work.
- [x] Project-owned roles, skills, policies, workflows, and extensions survive compilation and pack changes.
- [x] Third-party data-only and executable-pack trust boundaries are explicit and fail closed.
- [x] Pack version changes and migration declarations are schema-validated for later previewable upgrades.
- [x] Representative generic, Python, TypeScript/web, and monorepo fixtures compile deterministically and pass drift checks.
- [x] Measured routing/context observations record bounded evidence counts and retries without inventing token savings.
- [x] Independent semantic, security, and acceptance reviews find no unresolved blocker.
- [x] Requirements coverage and a Stage 5 completion report are current before closure.

## Exit evidence

Record exact commands and outcomes, catalog inventories, representative compiled fixtures, detection and no-mutation proofs, composition/conflict failures, extension-preservation checks, capability degradation, pack migration validation, measured efficiency observations, independent findings, and the Stage 6 eligibility decision.
