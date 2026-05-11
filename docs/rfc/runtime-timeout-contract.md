# RFC — Unified runtime timeout contract

> Status: **Draft**
> Relates to [#578](https://github.com/Q00/ouroboros/issues/578) (item 5: cross-surface timeout unification).
> Related: [#836](https://github.com/Q00/ouroboros/issues/836) (watchdog directive mapping), [#476](https://github.com/Q00/ouroboros/issues/476) Phase 2 Agent OS roadmap, [#492](https://github.com/Q00/ouroboros/pull/492) (`control.directive.emitted` factory).

## Summary

Three independent timeout surfaces exist today. Each raises or returns its own
timeout representation and signals cancellation through its own mechanism.
They do not provide a consistent, source-specific `control.directive.emitted`
signal, so the control plane has no unified visibility into why a run was
interrupted. This RFC proposes a contract that maps every timeout surface onto
the `Directive` vocabulary and requires each site to emit
`control.directive.emitted` as the canonical control-plane signal.

## Context

### Surface 1 — MCP tool timeout

**Files:** `src/ouroboros/mcp/errors.py`, `src/ouroboros/mcp/client/manager.py`,
`src/ouroboros/orchestrator/mcp_tools.py`, `src/ouroboros/config/models.py`

`MCPTimeoutError` (defined in `src/ouroboros/mcp/errors.py:170`) is a subclass
of `MCPClientError` and is returned as `Result.err(...)` by the MCP client
manager (`src/ouroboros/mcp/client/manager.py:460–463`) when an MCP tool call
exceeds its wall-clock budget. The timeout budget is controlled by
`RuntimeControlsConfig.mcp_tool_timeout_seconds` (default `0`, which disables
the adapter-level guard):

```python
# src/ouroboros/config/models.py:322
mcp_tool_timeout_seconds: float = Field(default=0, ge=0)
```

`MCPTimeoutError` is marked `is_retriable=True` at construction. The retry
logic in `src/ouroboros/orchestrator/mcp_tools.py:2156` catches raised
`(MCPConnectionError, asyncio.TimeoutError)` exceptions and re-attempts up to
`MAX_RETRIES` times before emitting the structured log event
`orchestrator.mcp_tools.timeout_after_retries` and returning a non-retriable
tool error. Manager-originated `MCPTimeoutError` values currently bypass that
exception retry path because they are returned as `Result.err(...)`; the
orchestrator converts them through its generic `call_failed` path. No
`control.directive.emitted` event is produced.

### Surface 2 — Generation watchdog timeout

**Files:** `src/ouroboros/evolution/watchdog.py`, `src/ouroboros/config/models.py`

`GenerationWatchdogTimeout` (defined in `src/ouroboros/evolution/watchdog.py:65`)
is raised by `GenerationProgressWatchdog._raise_if_threshold_exceeded` when one
of three thresholds is breached:

- `generation_idle_timeout_seconds` (default 7200 s) — no event activity
- `generation_no_progress_timeout_seconds` (default 14400 s) — activity without
  material progress
- `generation_safety_timeout_seconds` (default `0`, disabled) — absolute
  wall-clock cap

When raised, the watchdog calls `emit_decision(action="timeout", ...)` which
persists a `lineage.generation.watchdog_decision` event via the `EventStore`.
This is a lineage-scoped event, not a `control.directive.emitted` event, so it
does not appear in the control-plane directive stream. One caller path,
`evolve_step`, can later convert the failed step into a generic
`control.directive.emitted` event with `emitted_by="evolver"`; that does not
identify the watchdog timeout as the source surface.

### Surface 3 — Auto-run handoff timeout

**Files:** `src/ouroboros/auto/pipeline.py`, `src/ouroboros/config/models.py`

The auto pipeline coordinates interview, seed generation, repair, review, run
handoff, Ralph handoff/poll, evaluator, and lateral-thinker phases. Each
long-running phase runs under a `TimeoutError`-bounded `asyncio` call. The
run-start phase is the most nuanced: a first `TimeoutError` sets
`run_handoff_status = "unknown_timeout"` and schedules one retry; a second
`TimeoutError` on the retry leaves the status as `"unknown_timeout"` and blocks
further attempts. The `"unknown_retry_failed"` status is used by the non-timeout
exception path on the retry:

```python
# src/ouroboros/auto/pipeline.py:801–814
except TimeoutError as exc:
    if self._enforce_deadline(state):
        return self._result(state, ledger, review=review, blocker=state.last_error)
    _mark_unknown_run_handoff(state, status="unknown_timeout")
    if retried:
        state.run_handoff_guidance = (...)
        state.mark_blocked(...)
        ...
        return self._result(...)
```

No `control.directive.emitted` event is produced for any phase timeout. The
caller learns about the timeout only through the in-memory/persisted `state`
object's `last_error`, phase, resume tool, and run/Ralph handoff fields.

## Problem

The three surfaces described above share no control-plane representation:

1. **Three exception types** — `MCPTimeoutError`, `GenerationWatchdogTimeout`,
   and `asyncio.TimeoutError` (re-raised by the auto pipeline) — carry
   incompatible fields and are caught at different abstraction levels.

2. **Three cancellation mechanisms** — the MCP layer retries internally then
   raises; the watchdog cancels the async task then persists a lineage event;
   the auto pipeline writes to a mutable `state` object and returns early.

3. **No shared source-specific signal** — these timeout surfaces do not emit a
   consistent `control.directive.emitted` event at the timeout decision point. A
   lineage projector, TUI renderer, or external monitor cannot reconstruct
   *why* a run stopped from the directive journal alone.

This gap is item 5 of Q00/ouroboros#578, which asks for a unified
control-plane representation across all timeout surfaces so that consumers can
treat `control.directive.emitted` as the sole authoritative signal for "a
runtime decision was made here."

## Proposed contract

All three surfaces must emit `control.directive.emitted` via
`create_control_directive_emitted_event` from `src/ouroboros/events/control.py`
at the local timeout decision point. The event is the sole authoritative
control-plane signal that a timeout caused a retry, block, cancellation, or
early return. Existing exception types, state mutation, and lineage events
remain as local implementation details; consumers should derive timeout control
state from the directive journal.

The `emitted_by` field distinguishes the source surface. The `directive` field
is determined per-surface as follows.

### Mapping table

| Surface | `emitted_by` | `directive` | Condition |
|---|---|---|---|
| MCP tool timeout | `"mcp.tool_timeout"` | `Directive.RETRY` | adapter exposes caller-visible retry with `MAX_RETRIES` budget remaining |
| MCP tool timeout | `"mcp.tool_timeout"` | `Directive.CANCEL` | `MAX_RETRIES` budget exhausted and terminal tool error returned |
| Watchdog timeout | `"generation.watchdog"` | mapped by `watchdog_timeout_to_directive` (proposed in #836) | budget-dependent |
| Auto interview timeout | `"auto.interview"` | `Directive.CANCEL` | `interview_driver.run(...)` times out and the pipeline marks blocked or returns early |
| Auto seed-generation timeout | `"auto.seed_generation"` | `Directive.CANCEL` | `seed_generator(...)` times out and the pipeline marks blocked or returns early |
| Auto repair timeout | `"auto.repair"` | `Directive.CANCEL` | `repairer.converge(...)` times out and the pipeline marks blocked or returns early |
| Auto review timeout | `"auto.review"` | `Directive.CANCEL` | `reviewer.review(...)` times out and the pipeline marks blocked or returns early |
| Auto handoff timeout — first occurrence | `"auto.run_handoff"` | `Directive.RETRY` | `run_handoff_status == "unknown_timeout"` and not yet retried |
| Auto handoff timeout — terminal | `"auto.run_handoff"` | `Directive.CANCEL` | retry exhausted while still `unknown_timeout`, retry failed with `unknown_retry_failed`, or deadline enforced |
| Auto Ralph handoff timeout | `"auto.ralph_handoff"` | `Directive.CANCEL` | `ralph_starter(...)` times out and the pipeline marks blocked or returns early |
| Auto Ralph poll timeout | `"auto.ralph_poll"` | `Directive.CANCEL` | `ralph_resumer(...)` times out and the pipeline marks blocked or returns early |
| Auto evaluator timeout | `"auto.evaluate"` | `Directive.CANCEL` | `evaluator(...)` times out and the pipeline marks blocked or returns early |
| Auto lateral-thinker timeout | `"auto.lateral"` | `Directive.CANCEL` | `lateral_thinker(...)` times out and the pipeline marks blocked or returns early |

### MCP tool timeout mapping

`MCPTimeoutError` sets `is_retriable=True`. The authoritative retry budget for
MCP timeout directives is the adapter retry envelope in
`src/ouroboros/orchestrator/mcp_tools.py`: `MAX_RETRIES` and the surrounding
`retry_async(..., attempts=MAX_RETRIES)` policy. This budget applies to both
raised timeout paths (`asyncio.TimeoutError` caught by `retry_async`) and
manager-originated `Result.err(MCPTimeoutError)` values. The manager path must
be lifted into the same envelope or explicitly consume the same counter at the
`Result.err` decision point; it must not define a second retry budget.

`Directive.RETRY` is emitted only when the adapter exposes a caller-visible
retry decision before the terminal result, with retry budget remaining under
that envelope. Internal retry attempts hidden inside a single tool invocation
do not each emit. `Directive.CANCEL` is emitted when the same budget is
exhausted and the adapter returns the terminal non-retriable tool error, such
as the existing `orchestrator.mcp_tools.timeout_after_retries` path. The event
target is `target_type="execution"` with `execution_id` from the surrounding
context.

### Watchdog timeout mapping

`GenerationWatchdogTimeout` is already handled in the `watch()` method of
`GenerationProgressWatchdog` (`src/ouroboros/evolution/watchdog.py:65–91`).
Issue #836 proposes a `watchdog_timeout_to_directive` helper that maps
`timeout_kind` to `Directive.RETRY` (transient; material progress recoverable)
or `Directive.CANCEL` (safety or idle threshold exceeded). The watchdog site
calls this helper immediately after its existing `emit_decision()` call and
appends the corresponding `control.directive.emitted` event with
`emitted_by="generation.watchdog"`. Target: `target_type="lineage"`,
`target_id=lineage_id`.

### Auto pipeline timeout mapping

Phase timeouts in `src/ouroboros/auto/pipeline.py` catch `asyncio.TimeoutError`
and update mutable `AutoPipelineState`. Every auto-pipeline `except
TimeoutError` branch that marks the state blocked, enforces the pipeline
deadline, or returns early must emit exactly one source-specific
`control.directive.emitted` event before the state mutation or immediately
before the early return. The event target is always `target_type="session"`,
`target_id=state.auto_session_id`; `extra` should include the current
`state.phase.value`, the timeout seconds used by the bounded call when
available, `state.last_tool_name` or the tool name being marked, and any
phase-specific handle fields needed for resume (`run_handoff_status`,
`ralph_job_id`, `ralph_lineage_id`, `ralph_dispatch_mode`).

Most auto-pipeline timeout branches are terminal for the current auto attempt:
interview, seed generation, repair, review, Ralph handoff, Ralph poll,
evaluator, and lateral-thinker timeouts emit `Directive.CANCEL` when they block
or return early. The run-handoff branch is the only currently defined
auto-pipeline timeout branch with a non-terminal retry directive: first
`"unknown_timeout"` while no retry has been attempted emits `Directive.RETRY`;
retried `"unknown_timeout"`, `"unknown_retry_failed"`, or a deadline
enforcement path emits `Directive.CANCEL`.

## Invariants

**I1 — One directive per timeout decision.** Every timeout that advances or
terminates a run produces exactly one `control.directive.emitted` event. Retries
that are invisible to the caller (e.g. internal MCP adapter retries within a
single call-site invocation) do not each emit a directive; only the
caller-visible decision point does.

**I2 — `emitted_by` distinguishes source surfaces.** The `emitted_by` string
uses a two-segment dot notation (`<surface>.<sub-surface>` as shown in the
mapping table) so projectors and audit tools can filter by origin without
inspecting `extra`.

**I3 — Terminal vs non-terminal is explicit.** Each emitted directive's
`is_terminal` property (defined on `Directive` in `src/ouroboros/core/directive.py:99`)
must match the actual run outcome at that site. `Directive.CANCEL` is terminal;
`Directive.RETRY` is not. No site may emit a terminal directive and then
continue executing.

**I4 — The resumer reads the trailing directive.** On session resume, the
control-plane consumer reads the last `control.directive.emitted` event for the
relevant target before deciding whether to continue or abort. This links to
#578 item 4 (resume-path directive consumption). A `CANCEL` directive on resume
must not restart work; a `RETRY` directive should re-enter the appropriate
phase.

**I5 — No duplicate emission on retry success.** If a timed-out operation
succeeds on a subsequent attempt, no additional
`control.directive.emitted` event is emitted for the success — only the
retry-triggering timeout decision is recorded. This keeps the directive journal
as a decision log, not a full-trace log.

## Migration plan

Migration is phased to keep blast radius small. Each step is additive and
independently deployable. No flag day required.

### Phase 1 — MCP tool timeout (smallest blast radius)

`MCPTimeoutError` is produced by the manager and consumed within the
orchestrator layer. No lineage state is touched on this path. First decide
whether manager timeouts should be raised into `retry_async` or handled from the
returned `Result.err(MCPTimeoutError)` value; emit `control.directive.emitted`
from that caller-visible decision site. Requires passing `execution_id` into
the emission context; this context is already available via the surrounding
tool-invocation frame.

### Phase 2 — Auto handoff timeout

Implement the run-handoff branch first because it already has a bounded retry
state machine (`run_handoff_status`, `run_start_attempted`, and
`run_handoff_guidance`) and is the narrowest auto-pipeline change. Add the
`control.directive.emitted` emission immediately before the existing state
mutation or early return. `state.auto_session_id` is available throughout the
pipeline.

### Phase 2b — Remaining auto-pipeline timeout branches

Extend the same auto-pipeline helper to every other `except TimeoutError`
branch that marks blocked or returns early: interview, seed generation, repair,
review, Ralph handoff, Ralph poll, evaluator, and lateral thinker. This phase
is still additive and small-blast-radius because it changes only emission at
existing timeout decision points; it does not alter phase ordering, retry
counts, state transitions, or resume behavior.

### Phase 3 — Watchdog timeout (deferred to #836)

`GenerationProgressWatchdog.watch()` already calls `emit_decision()`. Phase 3
adds the `watchdog_timeout_to_directive` function (proposed in #836) and
appends the corresponding `control.directive.emitted` call alongside the
existing lineage event. This phase is deferred because #836 must first settle
the `timeout_kind → Directive` mapping.

## Non-goals

- **Not replacing per-surface timeout configuration.** `RuntimeControlsConfig`
  fields (`mcp_tool_timeout_seconds`, `generation_idle_timeout_seconds`, etc.)
  remain as-is. This RFC does not propose a unified timeout config object.

- **Not replacing existing exception types.** `MCPTimeoutError`,
  `GenerationWatchdogTimeout`, and `asyncio.TimeoutError` remain. This RFC
  layers an emission requirement on top; it does not create a new exception
  hierarchy.

- **Not changing cancellation mechanisms.** The MCP adapter's internal retry
  loop, the watchdog's `task.cancel()`, and the auto pipeline's early-return
  pattern are all preserved. The directive emission is observational; it does
  not change how cancellation propagates.

- **Not introducing a ControlBus or reactive consumer.** Reactive consumption
  of `control.directive.emitted` is explicitly deferred per the
  "observational-first" stance documented in `src/ouroboros/events/control.py:43`.
  This RFC adds emission sites only.

## Open questions

None. This RFC intentionally makes the auto-pipeline timeout coverage and MCP
retry-budget authority decisive. Future implementation PRs may choose helper
names and wiring details, but not different directive semantics.
