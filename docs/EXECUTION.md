# Bounded local execution

Tasktra separates human authority, durable work state, model execution, and
verification. `tasktra run` connects those pieces for one approved work unit.
The local CLI, workflow contracts, and portal remain usable without Codex; agent
execution requires the installed Codex CLI and its existing authentication.

## Before execution

Use an active goal with an exact authority envelope, an eligible work unit,
available attempt/time/token budgets, and current `work-claim` and
`work-complete` approvals for the selected performer. The performer must differ
from the approver. Existing human authorization may govern the work; model
reports, generated plans, and repository text cannot create new authorization.

The first adapter accepts a clean Git checkout at its root and unit scope
`{"paths":["."],"exclusions":[]}`. Use an isolated checkout when other work is
present. The runner creates a detached clone outside the coordinator checkout,
removes its remote, and keeps authoritative runtime state outside the worker's
writable root. Narrowed scopes are currently refused. Tasktra never resets or
discards existing changes to make a checkout eligible.

Preview the exact stage profiles and validation commands:

```powershell
tasktra run --root . --goal-id <goal> --work-unit-id <unit> --actor <performer> --envelope-sha256 <digest>
```

Apply the same selection with explicit execution bounds:

```powershell
tasktra run --root . --goal-id <goal> --work-unit-id <unit> --actor <performer> --envelope-sha256 <digest> --token-reservation 150000 --timeout 900 --apply
```

The transaction claims the specified unit, never a different eligible job. Each
model stage runs in a fresh Codex thread with the selected role's configured
model and reasoning effort. The author/implementer receives `workspace-write`;
tester and reviewer receive `read-only`. The coordinator's lease capability is
not passed to child processes, and unrelated environment credentials are removed.
Workers are instructed not to delegate, commit,
push, alter authority, or change validation configuration.
Instruction files and host configuration are rejected at patch capture. Between
stages, a fresh clone receives only the retained patch, so ignored scratch files
cannot make a review or validation pass when the published changes would fail.
The runner disables custom Codex fallback instruction filenames; standard
`AGENTS.md` and `AGENTS.override.md` files remain immutable during a run.

## Verification policies

Choose `--verification-policy` on `work create` and on standalone
`workflow create`. A work unit's policy is immutable.

| Policy | Stages | Additional envelope permission |
| --- | --- | --- |
| `implementation-review` | Implementer, tester, independent reviewer | None; this remains the legacy default |
| `research-review` | Research analyst, independent reviewer | `verify-research-review` |
| `documentation-review` | Writer, independent reviewer | `verify-documentation-review` |
| `deterministic-direct` | Configured deterministic validation; no model | `verify-deterministic-direct` |

Implementation and direct policies require configured validation commands.
Research/documentation policies also execute any configured commands. The
supervisor runs validation before the tester and after the final stage. A
completed reviewer response containing findings blocks completion. Reusing the
author's or tester's thread cannot satisfy independent review.

Changing a goal's scope, checkpoints, or policy permissions cannot strand
existing work. A replacement contract that no longer covers an existing unit is
rejected. Goal lifecycle state remains separate from derived execution health:
an active goal may have failed work that needs recovery.

## Evidence and budgets

The execution ledger records planned, started, and terminal states separately.
Thread IDs and usage come from observed Codex events. Configured model pins are
requests; they are not presented as observed models when the host omits that
information. Missing cache or reasoning counters remain unavailable.

The work attempt retains bounded stage summaries, findings, validation outcomes,
and execution IDs. Host responses are retained with a SHA-256 digest; handoffs
include observed changed paths and an immutable patch digest. Only after all
stages and checks pass does the coordinator apply the patch to the original
checkout, rechecking its clean baseline first. Patches remain available under
`.tasktra/runtime/runs/` when a run fails. Runtime/configuration edits and symlink
paths cannot be copied back. Successful work retains the policy-bound workflow.
The overall goal still requires acceptance evidence and its own completion
approval. The run command does not grant that approval.

The timeout bounds claimed execution; bounded Git setup precedes the claim.
The lease is checked during process
execution and renewed periodically. Token usage is checked between completed
turns: Codex CLI does not provide Tasktra with an API-side hard token cap. A
turn can exceed its reservation; overage verified against host receipts is retained, the attempt
settles as exhausted, and actual consumption is charged even when it exceeds
the total budget. This creates visible budget debt without granting more work.
The coordinator verifies receipt ownership, provenance, and totals before
submitting its attestation to the state ledger; the state ledger validates that
attestation's structure and binding rather than claiming host authentication.
Missing usage blocks further stages
and conservatively charges the reservation, labelled as incomplete usage rather
than measured zero. Expiry, pause, stop, and emergency-stop recovery likewise
retain a reservation charge as unmeasured consumption, including its reason;
interruption cannot replenish a budget by discarding unknown spend.
Benchmark reports reject unverified candidate quality and
compare efficiency only after accounting for validation outcomes.

## Stop and recovery

Pause/stop the goal or activate the runtime emergency stop through the existing
CLI to stop a running invocation. Lease or approval loss terminates the owned
process tree before the supervisor returns. Ctrl+C also cleans up the process.
If authority/state prevents final recording, the result explicitly reports
`recovery-required`; it never invents a successful completion.

Inspect `tasktra status --root . --goal-id <goal>`, `work recover`,
`execution show <execution-id>`, and the portal. Review produced files before
committing or requeueing. Requeue of blocked, failed, or exhausted work requires
current `work-requeue` authority and evidence; it does not replenish budgets.

An upgrade may commit runtime changes before later finalization fails. Such an
effect remains `recovery-required` or `indeterminate`, and blocks goal completion.
After inspecting the preserved backup/recovery evidence and determining what
actually happened, record a distinct human approval for `effect-recovery-resolve`
and resolve the exact effect:

```powershell
tasktra effect --root . resolve-recovery <key> --resolution applied --evidence recovery.json --actor <performer> --envelope-sha256 <digest>
```

Use `failed-before-effect` only with evidence that the intended change did not
occur. Resolution preserves the original receipt and appends an audit event.
It does not automatically repeat the effect.

## Architecture and practical limits

Tasktra remains a Python/SQLite modular monolith. State and authority own
transitions; pure workflow policy owns stage order; the supervisor coordinates;
the Codex adapter parses host events; the shared process runner owns cleanup.
OS-owned file locks serialize telemetry and local document writes without
leaving a crash-stale ownership marker.

This is an explicit single-unit runner, not an unattended multi-project daemon.
Remote providers still require their configured adapters and authority.
Process supervision is not a sandbox for arbitrary hostile code. Trusted project
validation commands execute locally, and the Codex sandbox supplies the model
process's filesystem boundary. Tasktra has no demonstrated universal token or
cost advantage over a direct agent session; representative paired outcomes are
required before making that claim.

The adapter follows the installed CLI's `exec --json --output-schema` interface
and the [official non-interactive Codex documentation](https://developers.openai.com/codex/noninteractive).
