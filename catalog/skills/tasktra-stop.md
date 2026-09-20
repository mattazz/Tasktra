---
id = "tasktra-stop"
title = "Pause, stop, or emergency-stop work"
family = "core"
---
# Pause, stop, or emergency-stop work

Use when work must halt while preserving durable evidence and recoverability.

1. Use `tasktra goal --root <root> pause <goal> --actor <actor>` for a resumable goal pause. This invalidates active leases and releases token reservations.
2. Use `tasktra goal --root <root> stop <goal> --actor <actor>` when the goal should not resume without a new explicit lifecycle decision.
3. Use `tasktra runtime --root <root> emergency-stop --actor <actor> --reason <reason>` when all active work must halt. Only an explicitly identified human may clear it with `tasktra runtime --root <root> clear-emergency-stop --actor <human> --actor-kind human`; clearing does not silently resume paused goals.
4. Verify `tasktra status --root <root>` and `tasktra audit --root <root> verify`. Preserve completed workflow and outcome evidence; never delete attempts to make the state look clean.
5. Resume only with a current human `goal-resume` approval bound to the exact envelope, then run `tasktra goal --root <root> resume <goal> --actor <coordinator> --envelope-sha256 <sha256>`.

Return which control was applied, affected goal, released or invalidated work, audit result, and the exact condition for safe resumption.
