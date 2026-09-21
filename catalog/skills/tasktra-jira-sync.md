---
id = "tasktra-jira-sync"
title = "Plan an optional Jira status update"
family = "jira-sync"
---
# Plan an optional Jira status update

Use only when this project explicitly enabled the `jira-sync` pack and has a `[jira_sync]` policy. This pack is one-way: Tasktra remains the source of truth for its own work, handoffs, workflow review, and completion. Jira is updated only through an approved provider effect.

## What maps to what

- `claimed` uses `jira_sync.claim_transition`, normally `In Progress`.
- `review-ready` uses `jira_sync.review_transition`, only when the project configured one.
- `completed` uses `jira_sync.complete_transition`, normally `Done`, and only after Tasktra's implementation, test, and review workflow is complete.

Do not plan a Jira update for a failed, expired, blocked, or merely retried Tasktra attempt. If Jira is unavailable, continue local work and leave the remote update pending.

## Procedure

1. Confirm the project has `[jira_sync]` in `.tasktra/project.toml`, the optional `jira-sync` pack is enabled, and the linked issue belongs to the configured Jira project. Never store credentials in this file.
2. Build the exact, read-only plan. For example:

   `tasktra jira-sync --root <root> plan --event claimed --issue PROJ-123 --goal-id <goal> --work-unit-id <unit>`

3. For an actual remote update, use the plan's closed descriptor, request, and stable idempotency key with the normal `tasktra effect --root <root> provider-prepare` workflow. That still requires the exact active authority envelope, resource scope, approval, lease, and configured host executor.
4. Report whether the Jira transition was prepared, executed, pending, unavailable, or needs reconciliation. Never claim Jira changed just because a plan was created.
