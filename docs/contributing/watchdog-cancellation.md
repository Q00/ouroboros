# Watchdog Cancellation Contract

This document records the v0 cancellation contract for
`GenerationProgressWatchdog.watch()` and explains what would need to change
to introduce two-stage escalation in the future.

## v0 Contract: `cooperative_direct_one_stage`

The constant `WATCHDOG_CANCELLATION_MODE = "cooperative_direct_one_stage"` in
`src/ouroboros/evolution/watchdog.py` names the current behavior:

| Term          | Meaning                                                                                      |
|---------------|----------------------------------------------------------------------------------------------|
| Cooperative   | The inner task receives a `CancelledError` and may run cleanup in its `except` block.        |
| Direct        | The watchdog holds the `asyncio.Task` handle and calls `task.cancel()` inline — no `AgentProcess` intermediary. |
| One-stage     | A single cancel is issued. There is no SIGTERM-then-SIGKILL style escalation sequence.       |

### Sequence (on threshold exceeded)

1. `_raise_if_threshold_exceeded()` raises `GenerationWatchdogTimeout`.
2. `watch()` catches it, calls `task.cancel()`.
3. `await task` is called inside `except asyncio.CancelledError: pass` so the
   inner coroutine can run its own cleanup before the watchdog continues.
4. `emit_decision()` persists a `lineage.generation.watchdog_decision` event
   with `details["cancellation_mode"] == "cooperative_direct_one_stage"`.
5. The `GenerationWatchdogTimeout` is re-raised to the caller.

### Why not two-stage?

Two-stage escalation (soft cancel → grace period → hard cancel) adds
complexity that is not yet justified:

- Ouroboros generations run inside asyncio tasks, not OS processes, so there
  is no OS-level signal boundary to cross.
- The cooperative `CancelledError` already gives the inner task a chance to
  flush state via standard Python `try/finally` or `except CancelledError`.
- No production incident or performance data has shown that the current
  approach leaves tasks in a bad state.

## Future: Introducing Two-Stage Escalation

If a future use case requires a grace period before hard cancellation (e.g.
because a generation holds an external resource that needs flushing), the
implementation change would be:

1. Issue a soft-cancel signal (e.g. set a threading `Event` or push to a
   `Queue` the inner task monitors).
2. `await asyncio.wait({task}, timeout=grace_period_seconds)`.
3. If the task has not finished, call `task.cancel()` (hard cancel).
4. Update `WATCHDOG_CANCELLATION_MODE` to `"cooperative_soft_then_hard"` (or
   a more descriptive name).
5. Update this document and add a migration note in `CHANGELOG.md`.
6. Update `test_no_material_progress_timeout_emits_cancellation_mode` to
   assert the new mode value.

The `WATCHDOG_CANCELLATION_MODE` constant is the single source of truth.
Downstream projectors that branch on cancellation mode should compare against
the constant, not a hardcoded string.
