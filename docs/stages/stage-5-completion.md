# Stage 5 completion report — Specialist and ecosystem packs

Status: complete on 2026-09-20. Stage 6 is eligible and active.

## Delivered outcome

Tasktra now has a composable, software-development-oriented specialist ecosystem without depending on source-project content, online services, or ecosystem-specific tooling. The catalog contains 36 roles: seven always-available core roles and 29 specialists with explicit triggers, responsibilities, inputs, outputs, stop conditions, validation expectations, and bounded evidence policies.

Twelve versioned packs cover core, architecture, quality, delivery, product, operations, knowledge, software development, generic repositories, Python, TypeScript/web, and monorepos. The Python profile resolves a deterministic ten-pack dependency order. Contributions across workflows, schemas, policies, adapters, and tests have explicit ownership; cross-form duplicates and incompatibilities fail rather than using last-write-wins behavior.

The compiler emits a deterministic `.tasktra/generated/pack-plan.json` alongside runtime projections. The lock schema records pack contract versions, trust classes, manifest-and-payload checksums, and the aggregate catalog hash. Project-owned files remain outside managed replacement and survive representative pack changes.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -q
```

Result: 254 tests passed in the final source, with three symbolic-link tests skipped because this Windows account lacks symbolic-link privilege. Windows junction/reparse, traversal, pack-shadow, and generated-path containment regressions passed.

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_stage5_ecosystem tests.test_compiler -q
python -m tasktra compile --root . --check
python -m tasktra audit --root . verify
```

Result: the 21-test ecosystem/compiler surface passed with one unrelated symlink-privilege skip; projections had no changed, missing, stale, locally edited, or metadata-drift entries; all 26 pre-completion audit events verified with no issue.

Independent acceptance separately passed all 14 Stage 5 tests, the complete suite, compile drift, pack previews, and audit verification. Independent semantic and security review returned GO after reproducing and closing trust-binding, Windows path/reparse, generated-junction, and ownership-namespace edge cases.

## Packs, detection, and local degradation

`tasktra packs recommend` is a read-only bounded planner. On Tasktra it recommended `python`, observed 193 files, retained 16 representative evidence records, performed zero retries, reported evidence compaction as uncertainty, and made no mutation. Exact markers are checked directly; directory enumeration, depth, file count, and evidence count are bounded; repositories, generated runtime roots, dependencies, virtual environments, links, and Windows reparse points are excluded or rejected.

`tasktra packs preflight --pack python` resolved core, architecture, delivery, knowledge, operations, product, quality, software-development, generic, and Python. Missing optional Git, GitHub, web-research, and Python capability observations remained visible while `unrelated_local_work_blocked` stayed false. Core compile-check and Python unittest defaults were surfaced without execution.

Generic, Python, TypeScript/web, and monorepo fixtures compile deterministically. Monorepo planning considers only bounded direct children of declared workspace roots, isolates package workspaces, and fans shared/root changes out to the bounded package set. Framework-specific assumptions are not embedded in the TypeScript or Python validation contracts.

## Trust and migration boundaries

External catalogs default to third-party data-only trust. Only the packaged catalog or an explicit reviewed `--trust-catalog` decision may project roles and skills. Third-party executable packs require an exact `id@version:payload_sha256` trust token. Their manifests declare argv, network, read, and write effects; previews expose those effects and never execute them.

Executable payload hashing covers every in-pack file named by argv and rejects unbounded directories. Existing external file or directory arguments, absolute and relative escapes, Windows drive-relative/backslash escapes, pack-local PATHEXT shadows, and exact-name links or reparse points fail closed. Executable payloads crossing a link, missing from declared reads, escaping the pack, or exceeding the one-megabyte bound are rejected. Changing a bound payload invalidates the checksum and trust token.

Pack contracts are version 1 and schema-validated with exact semantic versions, unique bounded arrays, safe paths/globs, dependencies, conflicts, activation conditions, capabilities, validation defaults, contributions, ownership, and migration edges. Stage 5 provides exact preview planning; authorized execution, rollback, and compatibility lifecycle remain Stage 6 work.

## Routing and efficiency observations

Specialist routing accepts the active profile and catalog, canonicalizes signals, selects the smallest enabled specialist set, and appends independent tester and reviewer gates when requested. Detection and routing report actual evidence counts and retries. Verified evidence is referenced instead of replayed, and pack planning is pure/read-only. No token-savings percentage is claimed; comparative local telemetry and benchmarks remain Stage 6 work.

## Independent review closure

Independent review initially found that an executable migration could bind only a decoy read, policy ownership used an invalid singular namespace, and the generated pack-plan destination did not reject Windows junctions. Further adversarial probes found external files and directories, Python directory payloads, Windows backslash and drive-relative escapes, PATHEXT shadows, and exact-name reparse points. Each finding received a fail-closed implementation and regression.

The final verdict was GO with no remaining source, security, or behavioral blocker. The reviewer confirmed checksum-bound payloads, projection junction safety, contribution collision handling, deterministic pack planning, clean lock/drift state, and the full Windows executable-resolution boundary.

## Known limitations

- Three symbolic-link tests require a Windows privilege unavailable to this account; equivalent junction/reparse and containment tests pass locally and the symbolic-link tests remain for privileged Windows and POSIX CI.
- Detection deliberately returns compact representative evidence rather than an exhaustive repository inventory.
- Executable migrations are only planned and trust-checked in Stage 5. Authorized execution, recoverable backups, rollback, and upgrade compatibility are Stage 6 deliverables.
- Usage observations are bounded and factual, but local token telemetry and comparative optimization benchmarks do not yet exist.
- Cross-platform CI proof, scheduler adapters, packaged examples, and the 1.0 release gate remain Stage 7 work.

## Decision

All Stage 5 criteria are satisfied. Tasktra now supplies a generic software-development core, a broad specialist catalog, deterministic ecosystem composition, bounded recommendations, preserved project ownership, and fail-closed pack trust. Stage 6 may build adoption, upgrade, telemetry, benchmarking, and lesson-promotion lifecycles on this reviewed contract.
