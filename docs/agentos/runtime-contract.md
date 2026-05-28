# Agent OS Runtime Contract — implementation snapshot

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

**Chosen: durable journal + observational projection.**
`ControlContract` events append to `EventStore` first, and
`ControlDirectiveEmission` provides a projected read model. Subscribers
on `ControlBus` may react to the live publish, but the journal is the
source of truth — recovery scans the EventStore via
`EventStore.get_events_after` (`src/ouroboros/auto/listeners.py:319`).
Best-effort `ControlBus` delivery is acceptable because the journal is
guaranteed.

This is the same option-A semantics formalized in #575 (see
[`docs/agentos/control-journal.md`](./control-journal.md)).

### Q4. Minimum dynamic MCP addition story

**Chosen: bridge-as-driver via `AgentRuntimeContext.mcp_bridge`.**
Capability changes propagate through the bridge handle rather than via
mutable global state. `bridge_mixin.inject_context_into_bridge_mixin`
(`src/ouroboros/mcp/tools/bridge_mixin.py:75`) shows the pull-based
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
