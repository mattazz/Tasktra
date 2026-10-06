# Token efficiency

Tasktra can reduce avoidable agent calls and repeated context. Whether it uses fewer tokens than direct execution depends on the task, selected workflow, host context, and retries. Independent review has a cost. Compare successful outcomes under the same acceptance checks before claiming savings.

Existing projects need runtime schema 12 for the immutable acceptance-check evidence. Use the [previewed upgrade and backup procedure](OPERATIONS.md#upgrades-backup-and-recovery). During upgrade validation, generated files and the lock describe the approved target while the live database still uses its prior schema. Health checks may report this temporary mismatch; a validation failure restores the previous files before any database migration commits.

## Measure first

```sh
python -m tasktra efficiency --root . report
python -m tasktra portal --root .
```

The CLI and portal's **Token usage** view aggregate the entire local execution ledger. They distinguish measured counters, unknown usage, failed/cancelled spend, and role/model attribution. Cached input is a subset of input; reasoning output is a subset of output. Neither is added twice. Missing measurements remain unavailable. Ambiguous overlapping receipts prevent complete coverage claims. This ledger does not automatically observe unrelated native Codex conversations.

For paired experiments, supply trial metadata and receipt IDs:

```sh
python -m tasktra efficiency --root . compare trials.json --resolve-local-receipts
```

Each trial names `scenario`, `replicate`, `mode` (`direct`, `current`, `optimized`), `config_fingerprint`, `acceptance_fingerprint`, `success`, `validation_outcome`, and `receipt_ids`. Optional elapsed time is caller-attested. Configuration fingerprints identify shared controls, such as repository revision and model/effort settings; keep deliberate treatment differences in the experiment record. Acceptance fingerprints identify the same checks and expected outcome across arms.

Tasktra resolves receipt IDs against its local ledger and includes related execution records so a failed sibling cannot be omitted. Receipt lineages cannot be reused across arms. Resolution establishes the report's source; it is not cryptographic proof of provider usage. Quality remains explicitly caller-attested: retain the actual validation artifacts with the experiment. Retry counts remain unavailable unless supplied as caller-attested metadata because sibling stages do not prove retries. Without `--resolve-local-receipts`, the command organizes caller-attested metadata and reports no measured savings. `--baseline direct` or `--baseline current` limits the comparison. The two baselines always have separate aggregates, with total paired spend as the primary measure and medians as secondary evidence. Aggregates cover eligible complete pairs only; ineligible counts stay visible and no overall savings claim is inferred.

## Choose the smallest authorized workflow

```sh
python -m tasktra work --root . plan-policy GOAL --work-type implementation --independent-review
```

The planner uses explicit requirements and the current goal contract. `--exploratory-tests` retains a dedicated tester. It makes a recommendation; it does not change existing work units or grant authority.

The default remains `implementation-review`: implementer, tester, independent reviewer. The opt-in `implementation-deterministic-review` uses implementer and independent reviewer, with configured deterministic checks before review and after the final handoff. It requires the exact envelope action `verify-implementation-deterministic-review`. Work-unit creation binds the configured commands; subsequent configuration drift blocks execution. Completion evidence must bind passed checks to the reviewed patch. A manual completion cannot substitute a generic successful handoff for those checks.

```sh
python -m tasktra work --root . create GOAL "Small change" --id UNIT \
  --scope scope.json --verification-policy implementation-deterministic-review
```

Use this route when existing deterministic acceptance is sufficient. New behavior needing exploratory test design should retain the tester. Research, documentation, and deterministic-only policies retain their own authorization requirements.

## Keep context focused

`tasktra run` defaults to compact context packets. Each stage receives the goal, acceptance criteria, scope, constraints, validation results, and patch digest. Source navigation includes file hashes and bounded Python symbol locations. Prior successful agents' prose is omitted; the reviewer still inspects actual source and dependencies independently. Required constraints are never silently truncated: oversized required context stops with a request to split the unit. Receipts record packet bytes and hashes, which are text-volume measurements, not token savings.

```sh
python -m tasktra context --root . inspect --path src/example.py --path tests/test_example.py
```

This deterministic command needs no model. The coordinator's in-memory cache reuses parsed facts only after hashing current bytes again. It rejects paths outside the project and linked sources. `--context-mode legacy` on `run` is available for controlled comparison.

## Select roles, skills, and effort explicitly

Optional project configuration can reduce the generated role and skill catalog:

```toml
[projection]
roles = ["implementer", "reviewer", "scout"]
# Omit skills to keep every skill contributed by enabled packs.

[agents.codex]
effort_profile = "efficient"
```

Omitted selectors retain current defaults. Names must come from enabled packs, and configured routes must still resolve. Project-owned custom agents remain intact. Compilation and upgrade previews include the exact managed-file changes; apply them using the existing managed-file workflow. Selecting fewer generated files does not disable globally installed skills or plugins.

The `efficient` profile maps fast-tier roles to low effort, balanced roles to medium, and deep/exceptional roles to high. Explicit role overrides, including `inherit`, win. Model choices and sandboxes are unchanged. This is an opt-in setting, not a claim that lower effort always yields lower total usage: retries can erase the benefit.

Skills use progressive disclosure: the host generally loads their descriptions before selecting a body. File-size reductions must not be presented as measured reductions in idle prompt tokens.

## Optional host tool selection

```sh
python -m tasktra run --root . --goal-id GOAL --work-unit-id UNIT \
  --actor coordinator --envelope-sha256 SHA --worker-context worker-context.json
```

Example explicit, credential-free selection:

```json
{
  "focused": true,
  "observed_mcp_servers": ["research", "unused"],
  "disable_mcp_servers": ["unused"],
  "requested_capabilities": ["research"]
}
```

An optional `user_profile` selects a named Codex profile. Plugin server selectors use `{ "plugin": "name@marketplace.name", "server": "files" }`; observed plugin selectors also require an `enabled` boolean. Selections must name observed identities, and requested capability names cannot be disabled. Keep every tool required by the task. Nothing changes global configuration.

Tasktra checks the installed CLI's supported launch flags and fails before dispatch if they are unavailable. Model, effort, and sandbox pins remain explicit. The receipt records requested overrides and labels their effective tool reduction **unverified**: CLI help alone cannot prove that a particular host honored an MCP setting. Applicable project instructions are retained.

## Reproduce a live comparison

```sh
python scripts/benchmark_efficiency.py --output .tasktra/runtime/experiment
python scripts/benchmark_efficiency.py --execute --output .tasktra/runtime/experiment --repetitions 3
```

The first command previews. The second spends model tokens using saved Codex CLI authentication. Each arm gets an isolated disposable Git repository with identical source and external acceptance checks. The direct arm uses one implementer; current uses three stages and legacy context; optimized uses two stages and compact context. Role-specific model pins are recorded and preserved. Arm order rotates across repetitions. All receipts, failed runs, checks, final source, and comparison results are retained in the output directory, which must be new. Optional `--worker-context worker-context.json` applies an explicit tool selection to the optimized arm only; record that treatment when comparing results.

This small coding experiment tests execution overhead. It does not evaluate selective projections, efficient effort settings, research tasks, or general defect rates. Provider caching and natural model variability remain uncontrolled; report cached input and sample size alongside total usage. A single pair cannot establish a broad percentage saving.

## Initial live evidence

One local name-normalization task was run on 2026-10-05 using the same initial source, Unicode/generator acceptance checks, model pins, and high reasoning effort. Implementer/tester used `gpt-5.6-terra`; reviewer used `gpt-5.6-sol`. All four completed outputs passed the unchanged external checks.

| Arm | Model stages | Total tokens | Cached input (included in total) | Elapsed |
| --- | ---: | ---: | ---: | ---: |
| Direct implementer | 1 | 110,757 | 82,944 | 97 s |
| Existing workflow, legacy context | 3 | 322,400 | 249,600 | 380 s |
| Smaller workflow, compact context | 2 | 186,635 | 138,368 | 213 s |
| Smaller workflow plus focused tools | 2 | 220,615 | 167,680 | 275 s |

In this sample, the compact two-stage workflow used **42.1% fewer tokens than the existing workflow**, but **68.5% more than the direct implementer**. The focused-tool extension used more tokens than the ordinary compact run. These results support reducing orchestration overhead; they do not demonstrate that Tasktra beats direct execution or isolate the contribution of compact context versus removing a stage. Independent review adds work that the direct arm does not perform. The fixed checks cannot establish equivalent defect detection generally.

The focused extension requested disabling five observed MCP services unnecessary for this local coding fixture. Its receipts still label effective host tool reduction unverified. Both optimized variants are retained, and the extension reuses the same direct/current controls rather than pretending to be another independent replicate.

Two initial workflow setup attempts failed before model dispatch because the temporary harness generated invalid unit IDs. The successful direct result was reused after repairing the harness. A temporary resume helper also decoded the Unicode acceptance file with the Windows default encoding and incorrectly marked both resumed arms unsuccessful. Their original execution results and deterministic checks passed. Explicit UTF-8 comparison plus fresh deterministic checks corrected only that scoring; original artifacts and receipts were retained. The shipped harness uses explicit UTF-8. Late contract-context parity and receipt fixes landed after these live launches, so this is development-build evidence rather than an exact final-commit benchmark.

The compact [evidence record](TOKEN_EFFICIENCY_RESULTS.json) retains counters, fingerprints, receipt IDs, artifact hashes, and limitations. Full local artifacts remain under `.tasktra/runtime/token-efficiency/`. Tokens used to develop Tasktra or perform this audit are outside the experiment. These single-run observations do not measure billing cost, cache-adjusted cost, repeated-task effectiveness, or total project savings.
