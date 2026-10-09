# Tasktra orchestration

Tasktra coordinates bounded work toward human-approved goals and preserves the
evidence needed to understand and control that work.

## Language

**Goal**: An intended outcome with acceptance criteria and an explicit boundary
for authorized work.

**Work unit**: A bounded part of a goal that can be assigned and evaluated.

**Work inspection**: A view of one work unit's current condition, attempts,
activity and evidence references at a single observed instant. It does not
authorize work or guarantee that a later action remains applicable.

**Action candidate**: A diagnostic command or possible next transition described
by a work inspection. A transition still requires its own inputs and authority.

**Ready frontier**: Incomplete work whose direct prerequisites are all complete.
It includes leased or blocked work and later checkpoints; structural readiness
does not establish permission to claim a unit.

**Blocking frontier**: Incomplete work with incomplete direct dependents, plus
terminal work needing attention even when it has no dependents. Its ordering
shows immediate dependency facts, not business priority.

**Remaining wave**: A zero-based dependency layer after completed work is treated
as satisfied. Wave zero is the ready frontier.

**Remaining structural depth**: The largest number of incomplete units on a
remaining dependency path. It measures graph shape, not time or a delivery date.

**Attempt**: One execution of a work unit by a performer holding its lease.

**Codex run**: One bounded host worker launch associated with an attempt. An
attempt can contain several sequential runs. The worker's host identity and
result are observations; they do not grant authority or complete the work unit.

**Launch slot**: The durable preparation that permits one immediate host launch.
Repeating preparation returns reconciliation guidance, never another launch.
An unresolved slot records uncertainty about whether a worker exists.

**Execution receipt**: An immutable record of the host identity or terminal
result observed for a prepared run. A receipt can arrive after the parent lease
expires without reopening that attempt.

**Unresolved run**: A prepared Codex run without a terminal execution receipt.
Its worker may be running, finished or unknown; the missing receipt retains its
capacity reservation until the recorded history resolves it.

**Detached worker**: An unresolved run whose parent attempt is no longer leased.
It retains its own capacity reservation until a terminal receipt arrives.

**Execution reconciliation**: Recording an observed worker identity or completed
result against an existing run. It preserves historical attribution and leaves
work-unit status and execution authority unchanged.

**Token accounting source**: Whether an attempt's token counter is legacy,
pending, caller-declared, derived from measured host receipts, or unavailable.
An arithmetic zero with unavailable measurements does not mean zero usage.

**Intervention request**: A specific request for input recorded when a performer
yields an attempt that cannot continue. One work unit has at most one current
intervention request.
_Avoid_: Approval, notification

**Intervention response**: An attributed answer to a request. A correction is a
new response revision; a response does not grant execution authority.
_Avoid_: Transition approval

**Transition approval**: A decision authorizing a specific transition within a
goal's authority boundary.

**Requeue**: An authorized transition that makes blocked work eligible for a new
attempt after its required evidence has been supplied.
_Avoid_: Resume attempt

**Intervention closure**: The record that a request was resolved through requeue
using a particular response revision. A declined or cancelled response leaves
the request open for a correction.
