---
id = "tasktra-handoff"
title = "Create or validate a handoff"
family = "core"
---
# Create or validate a handoff

Use when bounded work must cross an agent, workflow, or session boundary.

## Procedure

1. Start from `tasktra handoff template --goal-id <goal> --work-unit-id <item> --role <role> --actor-id <actor>`. Its `handoff` object is a valid bounded v1 `partial` envelope; save and edit that object as JSON in the adopted project.
2. Identify the goal, work unit, producer role, and actor. Never infer or expand authority from a handoff.
3. Keep `human_summary` concise. Separate verified facts from inferences, and attach every verified fact to a minimal evidence reference.
4. Include only changed project-relative paths, bounded validation outcomes, blockers, pending approval actions, and downstream context needed for the next role.
5. Run `tasktra handoff validate <path>` before a transition. Treat invalid, oversized, cross-goal, wrong-role, failed-check, blocking, or approval-pending output as non-advancing evidence.
6. For implement-test-review work, initialize with `tasktra workflow create --goal-id <goal> --work-unit-id <item>` and save its `workflow` object. Run `tasktra workflow accept <workflow-path> <handoff-path>` for each eligible result, replacing the saved state with its returned `workflow` object. Validate it with `tasktra workflow validate <workflow-path>`. A declared `completed` status alone is never completion evidence.

Return the concise summary, validated envelope location, accepted evidence identifiers, and the next eligible role or stop reason.
