# Platform behavior and fail-closed fallbacks

Tasktra uses Python's direct process APIs, SQLite, and contained project paths. The same authority and recovery rules apply on every platform; a platform feature that cannot be verified is unavailable rather than silently weakened.

## Filesystem links and paths

- Windows junctions, symbolic links, and other reparse points are treated as link boundaries. Runtime databases and managed-write destinations fail closed when an ancestor crosses one.
- On macOS and Linux, symbolic-link ancestors and resolved path escapes receive the same rejection.
- Path containment is resolved by filesystem semantics, not by string prefix. Windows drive and case behavior is normalized by the platform path implementation.
- Tests that need link-creation privileges may skip only when the operating system denies fixture creation. The corresponding hosted CI platform must still report the skip or pass explicitly in release evidence.

## Processes and shells

Validation and trusted-pack commands are passed as argument arrays with `shell=False`; project text is never interpolated into a shell command. POSIX process groups and Windows process trees are terminated through platform-specific helpers. If Tasktra cannot establish or terminate the process boundary, it reports a failure and does not claim success.

PowerShell is used in examples for Windows readability but is not a Tasktra runtime dependency. Equivalent direct `python -m tasktra ...` invocations work from POSIX shells.

## Locking and durable state

SQLite transactions are the concurrency boundary on Windows, macOS, and Linux. Claims are atomic, leases expire durably, and database lock contention returns a visible failure or bounded retry outcome. Tasktra does not infer success from a timed-out writer.

Backups use SQLite's consistent backup mechanism or the exact versioned migration backup recorded by Tasktra. Never copy only a live database file while a writer may have uncheckpointed WAL state.

## Verification boundary

The committed CI matrix covers Windows, macOS, and Linux on every supported Python version. A workflow definition is not execution evidence. Until hosted runs identify the exact runner image, Python version, outcome, and artifact hashes, the cross-platform release gate remains open.
