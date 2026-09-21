# Self-hosting evidence

Tasktra was developed using the same durable goals, approvals, work items,
handoffs, validation, and audit concepts that it ships to other projects.
The resulting 1.0 records are preserved as historical evidence in
[`docs/development-history/tasktra-1.0`](development-history/tasktra-1.0/).

They are deliberately outside the active `.tasktra` project directory so a
fresh source checkout is not presented as an in-progress Tasktra workflow.
The active root profile and generated metadata remain so the checked-in agent
projections can be verified. Run `tasktra bootstrap --root .` to create the
ignored local runtime database for development.
