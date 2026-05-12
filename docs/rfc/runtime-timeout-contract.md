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
the `Directive` vocabulary and, once that surface's target plumbing is
migrated, requires each site to emit `control.directive.emitted` as the
canonical control-plane signal.

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
   and `asyncio.TimeoutError` (handled in place by the auto pipeline and
   returned as blocked or early-return state) — carry incompatible fields and
   are caught at different abstraction levels.

2. **Three cancellation mechanisms** — the MCP layer retries internally then
   returns a terminal tool error; the watchdog cancels the async task then
   persists a lineage event; the auto pipeline writes to a mutable `state`
   object and returns early.

3. **No shared source-specific signal** — these timeout surfaces do not emit a
   consistent `control.directive.emitted` event at the timeout decision point. A
   lineage projector, TUI renderer, or external monitor cannot reconstruct
   *why* a run stopped from the directive journal alone.

This gap is item 5 of Q00/ouroboros#578, which asks for a unified
control-plane representation across all timeout surfaces so that consumers can
treat `control.directive.emitted` as the sole authoritative signal for "a
runtime decision was made here."

## Proposed contract

In the completed migration state, all three surfaces must emit
`control.directive.emitted` via `create_control_directive_emitted_event` from
`src/ouroboros/events/control.py` at the local timeout decision point. The event
is the sole authoritative control-plane signal that a migrated timeout decision
caused a retry, block, cancellation, or early return. A migration phase is not
complete for a surface until every covered decision site has both the local
timeout decision and the durable control target needed to emit that directive;
unplumbed sites remain explicitly outside that phase's completed coverage rather
than silently violating I1. Existing exception types, state mutation, and
lineage events remain as local implementation details; consumers should derive
timeout control decisions from the directive journal for migrated sites, with
non-terminal retry actionability resolved against the owning surface's persisted
retry-pending state as defined in I4.

The `emitted_by` field distinguishes the source surface. The `directive` field
is determined per-surface as follows.

Target types follow `CANONICAL_CONTROL_TARGET_TYPES` where an existing durable
target has the right replay semantics (`session`, `execution`, `lineage`,
`agent_process`, `contract`, `execution_node`). Generation-local watchdog
decisions still aggregate under `target_type="lineage"`, `target_id=lineage_id`
so `EventStore.replay_lineage(lineage_id)` and existing lineage projectors see
the interleaved directive stream. Their generation scope is expressed by the
required `generation_number` correlation field and idempotency key, not by a
new aggregate type.

### Mapping table

| Surface | `emitted_by` | `directive` | Condition |
|---|---|---|---|
| MCP tool timeout | `"mcp.tool_timeout"` | `Directive.CANCEL` | `MAX_RETRIES`/adapter retry envelope exhausted and terminal tool error returned |
| Watchdog timeout | `"generation.watchdog"` | same directive the evolution loop would emit for the failed generation outcome via `step_action_to_directive(...)` | generation retry/resilience-budget dependent |
| Auto interview timeout | `"auto.interview"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `interview_driver.run(...)` times out and the pipeline blocks or terminates |
| Auto seed-generation timeout | `"auto.seed_generation"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `seed_generator(...)` times out and the pipeline blocks or terminates |
| Auto repair timeout | `"auto.repair"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `repairer.converge(...)` times out and the pipeline blocks or terminates |
| Auto review timeout | `"auto.review"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `reviewer.review(...)` times out and the pipeline blocks or terminates |
| Auto handoff timeout — first occurrence | `"auto.run_handoff"` | `Directive.RETRY` | `run_handoff_status == "unknown_timeout"` and not yet retried |
| Auto handoff timeout — blocked after retry | `"auto.run_handoff"` | `Directive.WAIT` when blocked awaiting user/upstream resume on the same `auto_session_id`; `Directive.CANCEL` only when the auto attempt is truly terminal/deadline-enforced/non-resumable | retry exhausted while still `unknown_timeout`, retry failed with `unknown_retry_failed`, or deadline enforced |
| Auto Ralph handoff timeout | `"auto.ralph_handoff"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `ralph_starter(...)` times out and the pipeline blocks or terminates |
| Auto Ralph poll timeout | `"auto.ralph_poll"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `ralph_resumer(...)` times out and the pipeline blocks or terminates |
| Auto evaluator timeout | `"auto.evaluate"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `evaluator(...)` times out and the pipeline blocks or terminates |
| Auto lateral-thinker timeout | `"auto.lateral"` | `Directive.WAIT` when resumably blocked; `Directive.CANCEL` only for terminal/deadline-enforced/non-resumable termination | `lateral_thinker(...)` times out and the pipeline blocks or terminates |

### MCP tool timeout mapping

`MCPTimeoutError` sets `is_retriable=True`, but Phase 1 must not translate that
flag into `Directive.RETRY`. The MCP adapter's retry envelope in
`src/ouroboros/orchestrator/mcp_tools.py` is currently in-memory:
`_call_with_retry(...)` uses `retry_async(..., attempts=MAX_RETRIES)` for raised
timeout paths, and manager-originated `Result.err(MCPTimeoutError)` values flow
through the generic `call_failed` conversion. Neither path persists a
retry-pending token or state that a resume consumer could later consult.

For current Phase 1, MCP internal retries are therefore invisible to the
directive journal. The MCP surface emits exactly one timeout directive, and
only at the terminal caller-visible decision point: `Directive.CANCEL` when the
`MAX_RETRIES`/adapter envelope is exhausted and the adapter returns the
terminal tool error, such as the existing
`orchestrator.mcp_tools.timeout_after_retries` path. Manager-originated
`Result.err(MCPTimeoutError)` values may be normalized into the same terminal
adapter envelope, but they still must not emit `Directive.RETRY` without a
durable retry token. A future MCP retry directive is allowed only if the MCP
surface first adds persisted retry-pending state that satisfies I4/I5. The
canonical MCP timeout directive target is execution-scoped:
`target_type="execution"`, `target_id=execution_id`, and
`execution_id=execution_id`. Phase 1 must plumb this execution/control target
context through both `MCPToolProvider.call_tool(...)` and `_call_with_retry(...)`
before the MCP surface is considered migrated. If a call site cannot provide
`execution_id`, Phase 1 is incomplete for that call site and it must continue
using the local terminal tool error path only; implementations must not claim
unified MCP timeout coverage until all caller-visible MCP timeout decisions in
scope have the execution target plumbing. There is no alternate MCP timeout
target.

### Watchdog timeout mapping

`GenerationWatchdogTimeout` is already handled in the `watch()` method of
`GenerationProgressWatchdog` (`src/ouroboros/evolution/watchdog.py:65–91`) and
returned to the evolution loop as a failed generation outcome. The watchdog
source-specific emission must preserve the current generation control contract:
the directive for a watchdog timeout is the same directive the existing
evolution loop would have emitted for that failed generation outcome via
`step_action_to_directive(StepAction.FAILED, retry_budget_remaining=...)`.
That means the generation retry/resilience budget decides `Directive.RETRY`
versus `Directive.CANCEL`; `timeout_kind` is metadata/classification in
`extra`, not the authority for the directive value.

Issue #836 should therefore avoid a `watchdog_timeout_to_directive` helper that
maps `timeout_kind` directly to `Directive.RETRY` or `Directive.CANCEL`. If
#836 introduces watchdog-specific helper wiring, it must either call
`step_action_to_directive(...)` with the same retry budget the evolution loop
would use, or accept the already-computed directive from the loop. The watchdog
event appends `control.directive.emitted` under the existing lineage aggregate:
`emitted_by="generation.watchdog"`, `target_type="lineage"`,
`target_id=lineage_id`, `lineage_id=lineage_id`, and
`generation_number=generation_number`. `extra` includes `timeout_kind`,
thresholds, and the watchdog decision metadata needed for audit. This preserves
`EventStore.replay_lineage(lineage_id)` visibility while making the decision
generation-local: lineage resume readers must treat a watchdog directive with a
`generation_number` as scoped to that generation and must not interpret a
watchdog `CANCEL` as canceling the whole lineage chain.

The watchdog source owns source-specific directive identity for
`GenerationWatchdogTimeout` instances raised by `GenerationProgressWatchdog`,
but it must not suppress or bypass the generation retry-budget semantics. Phase
3 must make the identity concrete: `emit_decision(...)` must return or preserve
the persisted `lineage.generation.watchdog_decision` event id, or an equivalent
stable idempotency key, and attach it to the raised
`GenerationWatchdogTimeout` before re-raise, for example as
`exc.details["watchdog_decision_event_id"]`. The source-specific
`generation.watchdog` directive must derive its `idempotency_key` from that
stable decision identity plus `lineage_id`, `generation_number`, and
`timeout_kind`.

A caller that catches the bubbled exception, including the current
`evolve_step` path that can emit a generic `emitted_by="evolver"` directive for
a failed generation, must not emit a second directive for the same watchdog
timeout. `evolve_step` and generic evolver emission sites must check the
propagated watchdog `idempotency_key` or
`watchdog_decision_event_id` on `GenerationWatchdogTimeout` and skip generic
emission for that same watchdog decision. Without this propagated stable id,
Phase 3 is not implementable. A generic evolver directive is allowed only when
no source-specific timeout directive already exists for the same timeout
decision.

### Auto pipeline timeout mapping

Auto timeout decisions are not confined to `src/ouroboros/auto/pipeline.py`.
Most phase timeouts catch `asyncio.TimeoutError` in the pipeline and update
mutable `AutoPipelineState`, but interview timeouts are decided inside
`src/ouroboros/auto/interview_driver.py`: `_with_timeout()` raises the
tool-specific timeout, and `run()` catches it, marks the interview session
blocked, saves state, and returns a blocked result. Every auto timeout branch at
the actual decision site—pipeline `except TimeoutError` branches and the
interview driver timeout handlers that mark blocked/save/return—must emit
exactly one source-specific `control.directive.emitted` event. A timeout that
blocks a resumable auto phase on the same `auto_session_id` emits
`Directive.WAIT`, not terminal `Directive.CANCEL`. `Directive.CANCEL` is
reserved for explicit terminal termination: deadline-enforced aborts, explicit
terminal decisions, and non-resumable failures that must not be resumed on the
same auto session. Non-terminal `Directive.RETRY` branches must first persist
and commit, or atomically commit with the directive emission, the retry-pending
state transition that makes the retry durable. Non-terminal `Directive.WAIT`
branches must first persist, or atomically persist with the directive emission,
the blocked-state fields and resume handle that make the wait resumable. A
non-terminal `RETRY` or `WAIT` event must never become durable without the
corresponding durable local state it asks the resume path to honor. The
event target is always `target_type="session"`,
`target_id=state.auto_session_id`; `extra` should include the current
`state.phase.value`, the timeout seconds used by the bounded call when
available, `state.last_tool_name` or the tool name being marked,
`resume_tool_name` or the resume tool surfaced by the blocked state, and any
phase-specific handle fields needed for resume
(`run_handoff_status`, `ralph_job_id`, `ralph_lineage_id`,
`ralph_dispatch_mode`).

Under the current auto resume model, blocked interview, seed generation,
repair, review, Ralph handoff, Ralph poll, evaluator, and lateral-thinker phase
timeouts are resumable when the auto session remains blocked with a resume tool
on the same `auto_session_id`; those sites emit `Directive.WAIT` only after the blocked
state, resume tool, and phase handles have been saved or atomically committed
with the event. The event `extra` repeats `extra.phase`, resume tool
information, and phase handles needed to resume. The
run-handoff branch has the only currently defined auto-pipeline timeout
`Directive.RETRY`: first `"unknown_timeout"` while no retry has been attempted
emits `Directive.RETRY` and records the durable retry marker. `run_start_attempted`
alone is not that marker: it is set before the bounded `run_starter(...)` call
and can predate a timeout decision. The retry marker is the committed
transition to `run_handoff_status == "unknown_timeout"` (plus the same
`auto_session_id`/idempotency-key context), because that transition is the
state resume readers use to prove the first unknown handoff is retryable. After
that, a
retried `"unknown_timeout"` or `"unknown_retry_failed"` emits `Directive.WAIT`
when the state is blocked awaiting user or upstream resume on the same
`auto_session_id`; it emits `Directive.CANCEL` only when deadline enforcement
or another explicit terminal path makes the auto attempt truly terminal. For
the first `auto.run_handoff` `RETRY`, the persisted
`run_handoff_status == "unknown_timeout"` transition is the retry-pending
marker and must be durable before, or atomically with,
`control.directive.emitted`.

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
`Directive.RETRY` and `Directive.WAIT` are not. No site may emit a terminal
directive and then continue executing or resume the same target. A terminal
`Directive.CANCEL` must brick only the decision scope it truly terminates: an
execution-scoped MCP timeout cancels that execution target, a watchdog terminal
outcome stored on the lineage aggregate cancels only the correlated
`generation_number` within that lineage, and a session-scoped auto `CANCEL` is
valid only when the auto session is truly terminal, deadline-enforced, or
non-resumable.

**I4 — Resume combines the trailing directive with local state.** On session
resume, the control-plane consumer reads the last `control.directive.emitted`
event for the relevant target before deciding whether to continue or abort.
This links to #578 item 4 (resume-path directive consumption). A terminal
`CANCEL` directive on resume is authoritative only for the target and
correlation scope it actually terminates and must not be generalized to
unrelated resumable state. For lineage replay, consumers selecting generation
resume behavior must filter lineage-aggregate directives by
`generation_number`; a generation-scoped watchdog `CANCEL` is not a lineage-wide
cancel. A
non-terminal `WAIT` directive means the owning surface is blocked and resume
must use the directive `extra` plus local state, such as `extra.phase` and the
recorded resume tool, to continue on the same durable target when the blocker
is cleared. A non-terminal `RETRY` directive is actionable only while the
corresponding persisted local retry-pending state or token still indicates that
the retry is pending; if the owning surface has since succeeded or transitioned
state, that local state supersedes the stale trailing `RETRY` without requiring
a success directive. The current MCP Phase 1 contract has no such persisted
token, so MCP does not emit `Directive.RETRY`. Producers must preserve the same
ordering on write: a non-terminal `RETRY` may be appended only after, or in the
same atomic commit as, the persisted retry-pending state transition that makes
it actionable on resume.

**I5 — Retry success clears actionability without success spam.** If a
timed-out operation succeeds on a subsequent attempt, no additional
`control.directive.emitted` event is emitted for the success. The owning
surface instead clears or updates its persisted retry-pending state or token as
part of the normal success/state transition. The earlier `RETRY` remains in the
directive journal as the recorded timeout decision, but it is no longer
actionable on resume once local state shows that the retry has completed or the
operation has moved to a later state. Surfaces without persisted
retry-pending state, including current MCP Phase 1, must keep internal retry
attempts out of the directive journal. This keeps the directive journal as a
decision log, not a full-trace log.

**I6 — Non-terminal retry durability is atomic with its marker.** A
non-terminal `RETRY` directive and its owning surface's retry-pending marker
are a single durable resume contract. If the marker cannot be persisted, the
directive must not be appended. If the directive append succeeds, resume must be
able to find the corresponding marker and determine whether the retry remains
pending.

## Migration plan

Migration is phased to keep blast radius small. Each step is additive and
independently deployable. No flag day required.

### Phase 1 — MCP tool timeout target plumbing and terminal emission

`MCPTimeoutError` is produced by the manager and consumed within the
orchestrator layer. No lineage state is touched on this path. Phase 1 emits
only terminal MCP timeout directives because `_call_with_retry(...)` and
`Result.err(MCPTimeoutError)` do not persist retry-pending state. Emit one
terminal `Directive.CANCEL` from the caller-visible terminal decision site when
the `MAX_RETRIES`/adapter envelope is exhausted and a terminal tool error is
returned. Phase 1 also requires an API/plumbing change for the canonical
execution target: pass `execution_id` and the execution/control target context
through `MCPToolProvider.call_tool(...)` and `_call_with_retry(...)`, then emit
with `target_type="execution"`, `target_id=execution_id`, and
`execution_id=execution_id`. The current `MCPToolProvider` call path must not
be treated as if `execution_id` is already available at `_call_with_retry(...)`.
Any site that cannot provide `execution_id` after the Phase 1 plumbing must not
emit an MCP timeout directive until that site is plumbed.

### Phase 2 — Auto handoff timeout

Implement the run-handoff branch first because it already has a bounded retry
state machine (`run_handoff_status`, `run_start_attempted`, and
`run_handoff_guidance`) and is the narrowest auto-pipeline change. Add the
`control.directive.emitted` emission at the existing timeout decision point.
For the non-terminal first-time `RETRY`, commit the
`run_start_attempted`/`run_handoff_status` retry-pending transition before, or
atomically with, the directive append. After the first retry, a timeout that
blocks the same auto session awaiting user or upstream resume emits
`Directive.WAIT` with `extra.phase` and resume tool information. Terminal
`Directive.CANCEL` paths are limited to deadline-enforced, explicit terminal,
or non-resumable termination and may emit at the terminal decision or
immediately before the early return. `state.auto_session_id` is available
throughout the pipeline.

### Phase 2b — Remaining auto-pipeline timeout branches

Extend the same emission helper to every other auto timeout decision site that
marks blocked or returns early: seed generation, repair, review, Ralph handoff,
Ralph poll, evaluator, and lateral thinker in `auto/pipeline.py`, plus the
interview timeout handlers in `auto/interview_driver.py` that already catch the
timeout, mark the interview session blocked, save state, and return a blocked
result. This phase is still additive and small-blast-radius because it changes
only emission at existing timeout decision points; it does not alter phase
ordering, retry counts, state transitions, or resume behavior. Resumable blocked
phase timeouts emit `Directive.WAIT` on `target_type="session"` with
`extra.phase`, resume tool information, and any phase handles needed to resume
on the same `auto_session_id`; `Directive.CANCEL` is reserved for explicit
terminal/deadline-enforced/non-resumable termination.

### Phase 3 — Watchdog timeout (deferred to #836)

`GenerationProgressWatchdog.watch()` already calls `emit_decision()`. Phase 3
adds the source-specific `control.directive.emitted` call alongside the
existing lineage event without changing the generation retry-budget contract.
#836 must not map `timeout_kind` directly to `Directive.RETRY` or
`Directive.CANCEL`; it should preserve the directive that the failed generation
would receive through `step_action_to_directive(...)` with the active
resilience budget, and record `timeout_kind` only in `extra`. The implementation
must also make `emit_decision(...)` return or preserve the persisted
`lineage.generation.watchdog_decision` event id, or an equivalent stable
idempotency key, attach it to `GenerationWatchdogTimeout` before re-raise, and
derive the `generation.watchdog` directive `idempotency_key` from that stable
decision id plus `lineage_id`, `generation_number`, and `timeout_kind` on the
lineage aggregate. `evolve_step` and generic evolver emission must check the
propagated key and skip generic emission for the same watchdog decision.

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

- **Not adding new ControlBus wiring or a reactive consumer.** Reactive
  consumption of `control.directive.emitted` is explicitly deferred per the
  "observational-first" stance documented in `src/ouroboros/events/control.py:43`.
  `control_bus.py` already exists; this RFC adds emission sites only and does
  not expand ControlBus wiring.

## Open questions

None. This RFC intentionally makes the auto-pipeline timeout coverage and MCP
retry-budget authority decisive. Future implementation PRs may choose helper
names and wiring details, but not different directive semantics.
