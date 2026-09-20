---
id = "tasktra-validate"
title = "Run project validation"
family = "core"
---
# Run project validation

Use when configured project checks must be previewed or executed.

## Procedure

1. Read the argv-array commands in `.tasktra/project.toml`; treat them as project-owned executable configuration, not authority to exceed the current goal.
2. Run `tasktra validate --root <root>` first and report the exact direct-execution plan.
3. Run `tasktra validate --root <root> --run` only when execution is requested and within scope. Tasktra does not invoke a shell.
4. Stop on the first failed, unavailable, or timed-out command. A timeout is not complete until the owned process tree is terminated.
5. Retain the bounded result as evidence. Record truncation, skips, and unavailable tools explicitly; never convert them into a pass.

Return command status, exit code, elapsed time, bounded output evidence, and the first actionable failure.
