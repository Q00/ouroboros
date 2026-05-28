# AgentOS Runtime Contract — implementation snapshot

## 1. Status

**Realized — 2026-05-28.** Closes [#476](https://github.com/Q00/ouroboros/issues/476).

This document records the *as-implemented* answers to the five discussion
questions enumerated in RFC #476. Every claim below points at code that
has already merged on `main`. The RFC's Phase 1 (Capability OS) and
Phase 2 (Control OS) are now reachable in production; Phase 3 (Agent
Process OS) remains a future surface tracked separately under #1157 and
the `ooo auto` track, not under #476.

The document is reference-only: it does not introduce any new types,
events, or wiring. Its purpose is to fold the answered questions back
into the SSOT so #476 can close without losing the decision record.

## 2. Primitives that landed

| RFC primitive | Module | Notes |
|---|---|---|
| `AgentRuntimeContext` | `src/ouroboros/orchestrator/agent_runtime_context.py` | Frozen dataclass with five narrow fields: `event_store`, `runtime_backend`, `llm_backend`, `mcp_bridge`, `control`. Module docstring records the *"narrow-membership commitment"* from Q1. |
| `Directive` | `src/ouroboros/core/directive.py` | StrEnum syscall vocabulary; module docstring maps `StepAction` to `Directive` at the adapter boundary. |
| `ControlContract` | `src/ouroboros/core/control_contract.py` | Validated payload for `control.directive.emitted` events; rejects non-`Directive` values at construction. |
| `ControlBus` | `src/ouroboros/orchestrator/control_bus.py` | In-process delivery surface referenced by `AgentRuntimeContext.control`. |
| `ControlDirectiveEmission` (projection) | `src/ouroboros/core/lineage.py` | Read-model representation appended to `OntologyLineage`; lets replayers reconstruct emitted directives without re-running handlers. |
| `MCPBridge` (capability source) | `src/ouroboros/mcp/bridge/bridge.py` | Pulled in via `AgentRuntimeContext.mcp_bridge`; consumed by `src/ouroboros/mcp/tools/bridge_mixin.py`. |

## 3. Discussion questions — answered by code

### Q1. `AgentRuntimeContext + ControlBus` vs `PolicyBus`

**Chosen: `AgentRuntimeContext + ControlBus`.** `PolicyBus` was rejected
to avoid drifting toward a narrow per-aspect bus.

Evidence: `src/ouroboros/orchestrator/agent_runtime_context.py` exists
with the five-field minimal membership; its docstring records that
*"every new field added later must include a one-line PR-body
justification so the type does not drift into a service locator."*
`PolicyBus` is not used anywhere in `src/`.

### Q2. Where does `Directive` live?

**Chosen: `core/`.** The `Directive` enum lives in
`src/ouroboros/core/directive.py`. The docstring states the location
explicitly: *"Directives describe workflow control. They do not describe
capability or policy."*

This keeps the directive vocabulary independent of both orchestration
plumbing and runtime/control modules, so adapters can map their local
enums (e.g. `StepAction`) onto `Directive` without circular imports.

### Q3. `control.directive.emitted` — observational only or react?

**Chosen: durable journal + observational projection.** The
journal is the source of truth for any consumer; reactive subscribers
are forward-compatible but not yet wired.

What is implemented today:

- The journal has four production producers of
  `control.directive.emitted`, split across two failure stances and
  two atomicity stances. Subscribers must not assume one stance covers
  the whole stream:

  | Producer | Code | Target aggregate | Append semantics |
  |---|---|---|---|
  | `AgentProcessRuntime._make_emitter` | `src/ouroboros/orchestrator/agent_process.py:994-1029` | `("agent_process", process_id)` | **Observational best-effort.** Returns `None` when no `EventStore` is configured; when wired, `event_store.append(...)` is wrapped in `asyncio.wait_for(...)` and `except Exception` catches+logs `agent_process.directive_emit_failed` (the #476 *"journal stays out of the way"* rule). Lifecycle transitions complete regardless. |
  | `EvolutionLoop._emit_step_directive` | `src/ouroboros/evolution/loop.py:307-339` | `("lineage", lineage_id)` | **Strict single-event append.** `await self.event_store.append(...)` is uncaught — append failure propagates and aborts the step. |
  | `EvolutionLoop._emit_watchdog_timeout_directive` | `src/ouroboros/evolution/loop.py:341-372` | `("lineage", lineage_id)` | **Strict single-event append**, same shape as the step directive. |
  | `GenerationProgressWatchdog.emit_decision` | `src/ouroboros/evolution/watchdog.py:209-307` (atomic batch at `:301`) | `("lineage", lineage_id)` | **Strict atomic batch append** via `event_store.append_batch([decision_event, directive_event])`; both rows commit or neither does. Carries an `idempotency_key` exposing the `ControlContract.effective_idempotency_key` decision identity. |

  Q3 therefore answers two things at once: (a) the journal is the
  source of truth for any consumer, and (b) the failure/atomicity
  stance is per-producer, not uniform — a subscriber that expects
  every directive to be appended must remember the
  `agent_process` emitter may have caught its append.
- `ControlDirectiveEmission` (`src/ouroboros/core/lineage.py`)
  provides the projection that lets replayers reconstruct emitted
  directives without re-running handlers.
- Aggregate-scoped replay uses
  `EventStore.get_events_after(aggregate_type, aggregate_id,
  last_row_id=...)` per `(target_type, target_id)`. The canonical
  cursor pattern lives at `src/ouroboros/auto/listeners.py:319` (using
  the `"job"` aggregate, but the same shape applies to control
  aggregates).
- `ControlBus` (`src/ouroboros/orchestrator/control_bus.py`) is
  implemented and instantiated in
  `src/ouroboros/mcp/server/adapter.py:1872`, but no production
  callsite invokes `ControlBus.publish(...)` yet. The bus is in place
  ahead of subscribers so the wiring stays stable.

**Scope boundary with #575.** The producer-side journal contract
above is what #476 closes. The separate question of how a future
`ControlBus.publish(...)` or cross-process subscriber should observe
that journal — outbox vs best-effort delivery, replay cursor
mechanics, idempotency keys — is tracked under #575 and is *not*
formalized by this document. #476 closure here is independent of
where #575 stands today; a reader who needs the subscriber-side
decision should look at #575 directly. (The follow-up map at
`docs/contributing/control-plane-followups.md` still tracks #575 as
the canonical entry for that question.)

### Q4. Minimum dynamic MCP addition story

**Chosen: bridge-as-driver via `AgentRuntimeContext.mcp_bridge`.**
Capability changes propagate through the bridge handle rather than via
mutable global state. `bridge_mixin.inject_runtime_context`
(`src/ouroboros/mcp/tools/bridge_mixin.py:73`, called from
`src/ouroboros/mcp/server/adapter.py:1896`) shows the pull-based
shape: handlers pull capabilities from the context they were handed,
not from a process-global registry.

`MCPBridge | None` is intentional — non-MCP code paths stay valid
without forcing a bridge to be constructed.

### Q5. First reference migration site

**Chosen: MCP tool dispatch.** The first handler family to consume
`AgentRuntimeContext` was the MCP tool layer in
`src/ouroboros/mcp/tools/definitions.py` (see the `context:
AgentRuntimeContext | None` parameter on the dispatch path). This kept
the migration scoped to a single boundary that already had per-tool
permission/audit invariants.

## 4. Elegance bar — anti-actions still in force

The RFC's guardrails remain operative; this document is not a license
to expand the primitive set:

- Do **not** add fields to `AgentRuntimeContext` without a one-line
  justification in the PR body. The five-field membership is the
  contract.
- Do **not** broaden `Directive` into a dumping ground for every enum
  in the codebase. New directives need a workflow-control rationale, not
  a "this enum belongs together" rationale.
- Do **not** introduce a second control bus, policy bus, or capability
  registry. Future runtime surfaces should compose with the existing
  primitives or motivate the addition under a fresh canonical issue.
- Do **not** treat `MCPBridge` as a mutable global. It must be passed
  through the context.

## 5. What this document does not promise

- Phase 3 (Agent Process OS — long-running session lifecycle, hot-plug
  capability re-policy, replay primitive) is not closed by #476. That
  surface is being worked under #1157 (`ooo auto` SSOT) and its slice
  issues (#1170, etc.), and may eventually motivate new canonical
  surfaces — but only via the #961 SSOT sequencing rules.
- Plugin runtime contract extensions (#939) and external guidance
  contracts (#614) are tracked separately. They reuse `EventStore` /
  `ControlBus` / `AgentRuntimeContext` but do not extend the RFC #476
  primitive set.

## 6. Closure

#476 may close now. New design discussion that previously routed to
#476 should land as comments on the relevant canonical issue (#1157 for
`ooo auto` runtime evolution; #946/#956/#939 for projection / IR /
plugin substrate), per the [#961 SSOT](https://github.com/Q00/ouroboros/issues/961)
process rules.
