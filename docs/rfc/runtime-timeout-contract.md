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

The auto pipeline coordinates interview, seed generation, repair, review, and
run handoff phases. Each phase runs under a `TimeoutError`-bounded `asyncio`
call. The run-start phase is the most nuanced: a first `TimeoutError` sets
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
object's `last_error` and `run_handoff_status` fields.

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

All three surfaces must, after exhausting their local retry/cancellation
logic, emit one `control.directive.emitted` event via
`create_control_directive_emitted_event` from `src/ouroboros/events/control.py`.
The `emitted_by` field distinguishes the source surface. The `directive` field
is determined per-surface as follows.

### Mapping table

| Surface | `emitted_by` | `directive` | Condition |
|---|---|---|---|
| MCP tool timeout | `"mcp.tool_timeout"` | `Directive.RETRY` | retry budget > 0 |
| MCP tool timeout | `"mcp.tool_timeout"` | `Directive.CANCEL` | retry budget exhausted |
| Watchdog timeout | `"watchdog"` | mapped by `watchdog_timeout_to_directive` (proposed in #836) | budget-dependent |
| Auto handoff timeout — first occurrence | `"auto.run_handoff"` | `Directive.RETRY` | `run_handoff_status == "unknown_timeout"` and not yet retried |
| Auto handoff timeout — terminal | `"auto.run_handoff"` | `Directive.CANCEL` | retry exhausted while still `unknown_timeout`, retry failed with `unknown_retry_failed`, or deadline enforced |

### MCP tool timeout mapping

`MCPTimeoutError` sets `is_retriable=True`. The adapter in
`src/ouroboros/orchestrator/mcp_tools.py` already tracks retry iterations for
raised retryable exceptions, but manager-originated timeouts are currently
returned as `Result.err(MCPTimeoutError)` and must either be lifted into the
retry path or handled explicitly at the `Result.err` decision point. A
caller-visible retry decision emits `Directive.RETRY`; an exhausted retry budget
emits `Directive.CANCEL`. The event target is `target_type="execution"` with
`execution_id` from the surrounding context.

### Watchdog timeout mapping

`GenerationWatchdogTimeout` is already handled in the `watch()` method of
`GenerationProgressWatchdog` (`src/ouroboros/evolution/watchdog.py:65–91`).
Issue #836 proposes a `watchdog_timeout_to_directive` helper that maps
`timeout_kind` to `Directive.RETRY` (transient; material progress recoverable)
or `Directive.CANCEL` (safety or idle threshold exceeded). The watchdog site
calls this helper immediately after its existing `emit_decision()` call and
appends the corresponding `control.directive.emitted` event. Target:
`target_type="lineage"`, `target_id=lineage_id`.

### Auto handoff timeout mapping

Phase timeouts in `src/ouroboros/auto/pipeline.py` catch `asyncio.TimeoutError`
and update mutable `AutoPipelineState`. The proposal requires each
`except TimeoutError` branch to additionally call
`create_control_directive_emitted_event` before returning or blocking. The
`run_handoff_status` value and retry context at the catch site determine the
directive: first `"unknown_timeout"` → `Directive.RETRY`; retried
`"unknown_timeout"`, `"unknown_retry_failed"`, or a deadline enforcement path →
`Directive.CANCEL`. Target: `target_type="session"`,
`target_id=state.auto_session_id`.

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

Each `except TimeoutError` branch in `src/ouroboros/auto/pipeline.py`
already calls `state.mark_blocked()` or returns early — a well-defined decision
point. Add the `control.directive.emitted` emission immediately before the
existing state mutation. `state.auto_session_id` is available throughout the
pipeline.

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

**Q1 — Should phase-internal timeouts also emit directives?**
The mapping table covers the run-handoff `TimeoutError` branches explicitly.
The interview phase (`src/ouroboros/auto/pipeline.py:368`), seed-generation
phase (`src/ouroboros/auto/pipeline.py:455`), repair phase
(`src/ouroboros/auto/pipeline.py:603`), review phase
(`src/ouroboros/auto/pipeline.py:700`), and later Ralph/evaluate/unstuck/poll
helpers also catch `TimeoutError` and call `state.mark_blocked()` or return
early. Should those sites also emit `Directive.CANCEL`, or is the run-handoff
path sufficient for #578 item 5? The maintainer's answer will determine
whether Phase 2 covers all `except TimeoutError` branches or only the
run-handoff branch.

**Q2 — What is the authoritative retry budget for the MCP surface?**
`MCPTimeoutError.is_retriable=True` signals the error is retriable, but the
adapter in `src/ouroboros/orchestrator/mcp_tools.py` caps retries at
`MAX_RETRIES` internally via the local `retry_async` helper for raised
exceptions. Manager-originated `Result.err(MCPTimeoutError)` values do not
currently consume that retry budget. The `Directive.RETRY` emission proposed
here should fire once per caller-visible retry opportunity. Is `MAX_RETRIES`
the right boundary, or should the directive budget be configurable
independently of the transport-level retry count?
