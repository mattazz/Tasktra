# Local progress portal

Run the portal from an environment where Tasktra is installed:

```powershell
python -m tasktra portal --root C:\src\my-project --open
```

The terminal prints the local address. The default is `http://127.0.0.1:8765/`.
Keep the command running while viewing the portal; press Ctrl+C to stop it.
`--open` opens your default browser and is optional. To choose a different port,
use `--port 8766`; `--port 0` selects an available port and prints its address.

The portal is included in the Python distribution. It needs no Node.js, frontend
build, account, network service, or internet connection. It listens only on IPv4
loopback and is intended for use on your own computer.

## Explore the project

- **Overview** brings together goal progress, animated agent stations, job totals,
  and recent activity.
- **Goals** shows recorded goal status, completed job counts, acceptance evidence
  counts, checkpoints, and token budgets.
- **Jobs** shows work units, including waiting, running, blocked, and completed
  work. Search and status filters help narrow the list. Select a job for details.
- **Agents** shows execution records and workers associated with work leases.
  Filter by role, observed model, state, or search text, then sort by recent
  observation or tokens. Select an agent for routing, goal, timing, token
  breakdown, and provenance details.
- **Token usage** compares measured totals, averages, medians, cache utilization,
  uncached input, and recorded outcomes by model. Model, role, and time filters
  narrow the comparison. The hourly chart includes an expandable values table.

Select a goal card or use **Goal scope** to narrow jobs, agents, usage, activity,
and summary counts to that goal. Empty goals remain empty. Changing goals resets
local filters; clearing an agent, job, or usage filter keeps the goal selected.
Filters survive refreshes and tab changes. The live view refreshes every
three seconds. Pause updates when inspecting a snapshot; refresh manually when
needed. Motion can be turned off, and the interface respects reduced-motion
preferences. If a refresh fails, the last successful view remains visible with a
connection warning.

**Explore demo** switches to clearly labeled sample data in the browser. It never
creates a goal, job, agent, or execution record. Exit demo to return to the project.

## What progress means

The portal reads the configured Tasktra runtime database and optional
`.tasktra/runtime/agent-execution.sqlite` execution ledger. It does not intercept
Codex conversations or discover every process on your machine. Work must be
recorded through Tasktra's existing goal, work, and execution commands to appear.
See [agent execution and usage](OPERATIONS.md#agent-execution-and-usage) for how
the execution ledger distinguishes planning, host callbacks, manual assertions,
and verified usage.

Goal progress is the fraction of recorded jobs marked complete. A goal with no
jobs has no calculated percentage. Completing all jobs does not automatically
complete a goal: its recorded status and acceptance evidence remain separate.
Running jobs do not have invented completion percentages. Missing token usage is
shown as unavailable. Budget consumption is the runtime's recorded accounting,
which is distinct from measured execution tokens.

A planned execution is not a running agent. A started execution means the ledger
records a start; it is not independent proof that an operating-system process is
still alive. Lease heartbeat and expiry help identify stale workers. An expired
lease is labeled stale rather than silently treated as fresh progress. Execution
records inherit goals through recorded work-unit or execution ancestry, or an
explicit goal attribution. A missing lease heartbeat means no matching lease
observation was recorded; it does not establish that the agent is inactive.
Finished receipts show the heartbeat as not applicable. Observed execution activity
is displayed separately. Animations illustrate recorded states only.

If verified historical records lack a goal relationship, explicitly attribute the
anchor execution to an existing goal after checking the source evidence:

```powershell
python -m tasktra execution --root C:\src\my-project attribute-goal <work-id> <goal-id> --reason verified-task-lineage
```

Attribution also applies to existing descendants, is immutable, and rejects a
conflicting attribution. This command writes the optional execution ledger; viewing
the portal never performs the repair automatically. Future descendants can resolve
the relationship through their recorded parent.

## Reading usage statistics

Planned work is excluded from measured run statistics. Unknown usage remains
unknown, including imports rejected by the safe parser. Cached input is a subset
of input: total tokens equal input plus output. Cache utilization is the fraction
of input tokens served from cache, not a request hit rate or a billing estimate.
Outcome percentages use finished receipts; they do not establish task quality.

Time filters total actual measured token-event deltas in the selected interval.
A run spanning the boundary contributes only its in-range events. Averages and
medians are per measured execution in that selection. Older imports can lack
complete event timing, so all-time totals may exceed chart totals; coverage notes
show this gap. Reimporting the same registered rollout can enrich timestamps while
preserving receipt identity and append-only usage checks. The portal never guesses
timestamps or counts undated tokens as zero.

Model comparisons describe recorded work. Different tasks, outcomes, and acceptance
criteria are not controlled experiments; these figures do not prove token savings.
See [controlled comparisons](TOKEN_EFFICIENCY.md) for that workflow.

## Local and read-only

Viewing the portal does not initialize or migrate databases, recover leases,
change work status, activate goals, or dispatch agents. It has no job control
buttons. Missing runtime data produces an empty state. Initialize an adopted
project's runtime separately with `python -m tasktra bootstrap --root <project>`.
For an older runtime schema, stop the portal and use Tasktra's normal bootstrap
and diagnostic workflow before restarting.

The server serves only bundled portal assets and a read-only `/api/snapshot`
endpoint. It rejects foreign Host and Origin headers and exposes selected progress
fields rather than raw audit payloads, lease tokens, authority contracts, or
arbitrary project files. Project titles and descriptions are visible to the local
browser; use the portal on a trusted local session. This is not a remotely hosted
or multi-user dashboard.

Recent activity and detail collections are bounded. The portal reports when
details are truncated; global totals can therefore exceed the visible list.

## Troubleshooting

- **Port occupied:** launch with another port or `--port 0`.
- **No recorded activity:** check the selected project root and whether goals,
  work units, or execution records exist. Demo mode can preview the interface.
- **Connection lost:** check that the terminal command is still running and use
  Refresh after restarting it at the same address.
- **Runtime unavailable:** follow the displayed configuration or schema message;
  the portal leaves the database unchanged.
- **Execution ledger unavailable:** a warning appears and runtime lease records
  still show known workers. Optional receipt or usage details may be unavailable.
