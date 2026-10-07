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
  breakdown, and provenance details. The selected-agent panel also follows
  reported progress and tool activity from an exactly linked local Codex rollout.
- **Map** links recorded goals, jobs, agents, and parent work in an interactive
  node diagram. Search, zoom, and select a node to trace its connections and open
  the related record.
- **Token usage** compares measured totals, averages, medians, cache utilization,
  uncached input, and recorded outcomes by model. Model, role, and time filters
  narrow the comparison. The hourly chart includes an expandable values table.

Select a goal card or use **Goal scope** to narrow jobs, agents, the map, usage, activity,
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

## Relationship map

Open **Map** to explore recorded goal membership, job assignments, and parent
work in an interactive network. The portal bundles Cytoscape.js and its fCoSE
layout locally; it works without a CDN. See [graph dependency provenance](PORTAL_GRAPH_DEPENDENCIES.md)
for versions, licenses, and upgrade checks.

The overview keeps goals and active runs visible, with counted summaries for
jobs and historical runs. Summary shapes and labels identify groups clearly.
Every summary retains its exact member IDs and recorded connections. Job-owned
runs stay connected through their job summary; grouping does not create new
assignments. Parent lineage is optional to keep the overview readable.

Open a summary to inspect a bounded page of its records. Paging through a large
group keeps the network readable; return with **Overview**. Search and the
adjacent record picker cover all loaded records, including members inside
summaries. Choosing a record opens its immediate recorded neighborhood.

Drag nodes to arrange them, drag the background to pan, and use zoom and fit
controls to explore. Select a node to see its recorded state, context, metrics,
and immediate connections in the detail panel below the map. Open an agent's
record to reach its activity panel. Selection does not change global goal scope.

Metrics-only live refreshes preserve manual positions, view, and selection
without rerunning layout. When recorded connections or group membership change,
the layout updates while keeping your pan and zoom. Changing goal scope or
switching demo mode resets the map context. Layout changes have no animation,
including when reduced motion is requested. Keyboard users can navigate the record picker, scope controls,
and detail actions without pointing at canvas nodes.

Only recorded IDs create edges. When a loaded job already links an agent to
the same goal, the map shows the goal -> job -> agent path. Otherwise, the agent
keeps its recorded goal link; shared roles, names, and similar identifiers do
not imply a job assignment. Missing referenced records are marked as not loaded.
An unresolved parent work ID is not assumed to be an agent or a job.

The map covers the loaded snapshot, which can be partial. Counts distinguish
loaded records, collapsed members, and references whose records are not loaded.
Display limits are separate from the full graph; search can find records beyond
the display limit. Viewing and navigating the map never changes goals, job
assignments, execution records, or runtime state.

## Selected-agent activity

Select an agent to open its activity panel. It combines project, recorded job and
goal links, routing, heartbeat, recorded token usage, and a timestamped output
feed. The feed refreshes while the panel is open and live updates are enabled.
Pause updates or turn off follow-output to inspect earlier entries without losing
your place. A missing job link is shown explicitly; the portal does not infer a
job from message content.

The feed shows public assistant progress messages, final output, and tool names
with observed start/finish events. It never returns private analysis or reasoning,
user/system/developer messages, tool arguments, or raw tool output. Text is rendered
as text, not executable HTML. Reported progress is not a continuous view into the
model's internal state, and silence does not establish that an agent is idle.

Use **Show activity** to select **All activity**, **Progress** (public updates),
**Tools** (starts, completions, and failures), **Status** (execution phase changes),
or **Final output** (public final responses). Counts cover the loaded events,
which may be a bounded or partial history. The selected category stays in effect
when new activity arrives and when another agent is opened in this page. A category
with no matching events explains the empty result; it does not mean the agent is
idle. Filtering the feed leaves the agent's state and token metrics unchanged.

Activity requires an existing Codex execution receipt with a registered thread,
turn, and rollout path fingerprint. The server searches only the host's Codex
sessions directory (`CODEX_HOME/sessions`, or `~/.codex/sessions`), then checks the
exact path fingerprint, session identity, project directory, and turn before
exposing events. HTTP callers can select a work ID; they cannot supply a file path
or sessions directory. Unlinked records keep their normal details and explain
why live activity is unavailable. No import or database write occurs while viewing.

Reads and retained events are bounded. Truncated, growing, or unavailable logs are
labeled; file modification time is never presented as agent activity. Live token
observations, when the parser can verify them for the selected turn, are separate
from the last imported ledger totals and are never added to them.

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

The server serves only bundled portal assets and read-only `/api/snapshot`
and `/api/agent-activity?work_id=<recorded-work-id>` endpoints. It rejects foreign Host and Origin headers and exposes selected progress
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
