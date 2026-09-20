# Stage 6 completion report — Lifecycle and optimization

Status: complete on 2026-09-20. Stage 7 is eligible and active.

## Delivered outcome

Tasktra now supports safe existing-project adoption, exact preview-first upgrades, local privacy-preserving telemetry, deterministic comparative benchmarks, and independently reviewed lesson proposals. These capabilities are generic software-development infrastructure and contain no source-project history.

Adoption inventory is bounded and read-only. Existing `AGENTS.md`, `.agents`, `.codex`, and `.claude` surfaces remain project-owned, and Tasktra never claims that differently shaped instruction systems are semantically equivalent. Upgrade previews bind installed locks, manifests, pack contracts, the actual on-disk runtime schema, creates, updates, stale-file deletions, capability gaps, migrations, and validation commands into one digest.

Applying that plan requires durable `local-effect` authority. Project writes are snapshotted, stale generated files may be removed only when their exact deletion was previewed, and the runtime database must resolve inside the project without symbolic-link, junction, or reparse-point ancestry. A schema migration records the exact versioned database backup and SHA-256. Automatic file rollback is explicitly unavailable after a committed runtime-schema change; human recovery uses that exact database backup.

## Acceptance evidence

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -q
python -m unittest discover -s tests -p 'test_stage6*.py' -v
python -m tasktra compile --root . --check --trust-catalog
python -m tasktra validate --root . --run
python -m tasktra audit --root . verify
```

Result: 308 tests passed in the final source, with six symbolic-link tests skipped because this Windows account lacks symbolic-link privilege. Windows junction and reparse-point regressions passed. The Stage 6-focused surface passed 51 tests with two environment-dependent skips. Generated projections were clean, configured validation passed, and the authority audit chain verified without issue.

Independent acceptance reviewed all 16 criteria. Independent security review repeatedly reproduced migration, ownership, privacy, authority-ledger, and path-boundary attacks; each confirmed issue received a fail-closed regression before the final GO verdict.

## Adoption and ownership

`tasktra adopt` reports bounded metadata, every proposed managed write, preserved project paths, conflicts, uncertainty, capability gaps, and validations without changing the repository. Existing instructions are neither overwritten nor treated as equivalent based on filenames. Directory enumeration is stable across case-colliding names.

Generated state is manifest-bound. Upgrade preview rejects a manifest that is not bound to the lock, local edits to active or stale managed outputs, unsupported semantic-version and schema edges, undeclared pack migrations, and project-owned path collisions. Obsolete generated files are explicit `delete` entries with their observed size and digest; apply refuses an omitted deletion.

## Upgrade, migration, and recovery boundaries

Pack and runtime migrations are ordered and checksum-bound. Preview executes nothing. Apply independently rechecks the installed lock/manifest, actual runtime schema, exact plan transition, authority, and plan digest before mutation. External validations run before runtime migration; canonical lock finalization and compiler drift are verified afterward.

A genuine schema-7 fixture upgrades to schema 8 and records a collision-safe backup path and digest even when the conventional backup name already exists. The receipt sets `rollback_available` to false when runtime recovery requires that backup. Validation or pack-migration failure restores the bounded file snapshot and never reports partial success as completion.

The runtime database cannot be absolute or escape through `..`, a symbolic link, a Windows junction, or another reparse point. Adversarial preview and apply fixtures prove that an external schema-7 database remains unchanged and receives no backup.

Third-party executable packs still require an exact reviewed trust token and checksum. That trust is deliberately strong: executable migrations are full-host code execution. Environment scrubbing and declared file/network effects aid review and reduce accidental disclosure, but are not represented as an operating-system sandbox.

## Privacy, measurement, and lessons

Telemetry is disabled by default, local-only when enabled, and constrained to a closed schema of registered labels and bounded numeric observations. Prompts, source text, credentials, tokens, arbitrary labels, links, oversized storage, and secret-shaped nested data are rejected. Leaving the machine requires an explicit sanitized, project-local export.

Representative benchmark observations preserve measured retrieval counts, distinct evidence counts, context tokens when supplied, escalations, retries, elapsed milliseconds, validation outcomes, and human interventions. The comparator flags repeated retrieval, context growth without new evidence, added escalation, and retry regressions. It does not estimate or invent token savings.

Lesson candidates remain bounded proposals with evidence hashes, applicability, risks, affected contracts, and regression checks. Author and reviewer separation is enforced. Approval only enables a deterministic promotion preview; canonical edits require separate authority, then normal compiler, drift, and regression gates.

## Independent review closure

Adversarial review found and closed: lock/manifest rebinding, direct state-migration bypass, telemetry label exfiltration, nondeterministic inventory order, host-secret inheritance, rollback deletion of authority receipts, undisclosed runtime migration, missing exact backup evidence, false rollback availability, undisclosed stale-file deletion, and runtime-database escape beyond project authority. The final implementation binds every such effect to preview, authority, containment, or a durable receipt.

## Known limitations

- Six symbolic-link tests require a Windows privilege unavailable to this account; equivalent Windows junction/reparse and containment tests pass, and the symbolic-link checks remain for privileged Windows and POSIX CI.
- Explicitly trusted executable migrations are not OS-sandboxed. Only install them after reviewing their exact checksum-bound payload and declared effects.
- Telemetry and benchmarks intentionally report measured local observations only; they do not infer dollar, latency, or token savings that were not observed.
- Scheduler adapters, cross-platform CI proof, packaged examples, operational runbooks, self-hosted 1.0 evidence, and the release gate remain Stage 7 work.

## Decision

All Stage 6 criteria are satisfied. Tasktra has a reviewed lifecycle for adoption, upgrades, recovery, privacy-safe measurement, efficiency regression detection, and deliberate lesson promotion. Stage 7 may now complete operations, examples, cross-platform proof, self-hosting evidence, and the 1.0 release gate.
