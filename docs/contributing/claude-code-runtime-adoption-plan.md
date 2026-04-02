# Claude Code Runtime Adoption Plan for Ouroboros

## Purpose

This document translates the Claude Code analysis artifacts into concrete recommendations for `~/Project/ouroboros`.

The goal is not to copy Claude Code wholesale. The goal is to import the parts that strengthen Ouroboros as a runtime-agnostic engine, while keeping runtime-specific UX and product-shell concerns outside the engine boundary.

Primary source artifacts used for this recommendation:

- `/Users/jaegyu.lee/src/claude_code_ac2_subac1_identified_item_universe.md`
- `/Users/jaegyu.lee/src/RuntimeAgnosticPatternAssessment.md`

Primary Ouroboros code surfaces reviewed for this recommendation:

- `src/ouroboros/orchestrator/adapter.py`
- `src/ouroboros/orchestrator/runtime_message_projection.py`
- `src/ouroboros/orchestrator/session.py`
- `src/ouroboros/orchestrator/mcp_tools.py`
- `src/ouroboros/orchestrator/runner.py`
- `src/ouroboros/orchestrator/parallel_executor.py`
- `src/ouroboros/orchestrator/coordinator.py`
- `src/ouroboros/orchestrator/execution_runtime_scope.py`
- `src/ouroboros/orchestrator/level_context.py`
- `src/ouroboros/providers/claude_code_adapter.py`
- `src/ouroboros/providers/codex_cli_adapter.py`
- `src/ouroboros/codex_permissions.py`
- `docs/runtime-capability-matrix.md`

## Executive Summary

Ouroboros already has the right engine direction in three places:

- a backend-neutral runtime/session handle in `orchestrator/adapter.py`
- a shared runtime-message projection layer in `orchestrator/runtime_message_projection.py`
- a deterministic merged session tool catalog in `orchestrator/mcp_tools.py`

The best Claude Code ideas to import are therefore not UI patterns or shell ergonomics. They are:

1. Formalize the existing runtime/message seam as the engine kernel.
2. Upgrade the tool catalog into a semantics-bearing capability graph.
3. Add a unified policy plane for capability visibility and execution authorization.
4. Add a scheduler/control-plane contract that consumes capability semantics.
5. Derive coordinator capability envelopes from that same graph instead of hardcoding them.

The right things to keep out of the engine are:

- CLI and TUI details
- installer/bootstrap/update flows
- auth/browser consent UX
- voice or client-device integrations
- optional sidecars such as transport, memory, LSP, timers, and computer-use, unless product demand proves they deserve first-class status

## What Ouroboros Already Has

### 1. Runtime-neutral kernel seam is partially present

Ouroboros already has the equivalent of the Claude Code `P01` insight:

- `RuntimeHandle` in `src/ouroboros/orchestrator/adapter.py` provides a backend-neutral resume/control handle.
- `ProjectedRuntimeMessage` in `src/ouroboros/orchestrator/runtime_message_projection.py` normalizes runtime-specific messages into shared workflow/session updates.
- `SessionTracker` and `SessionRepository` in `src/ouroboros/orchestrator/session.py` persist session state through event sourcing.

Recommendation:

- Keep this seam stable.
- Treat `RuntimeHandle + ProjectedRuntimeMessage + SessionTracker` as the core engine contract.
- Avoid leaking TUI, CLI, provider-specific, or product-shell fields into this contract.

### 2. Canonical tool-catalog foundation is already present

Ouroboros already has the early form of the Claude Code `P05` insight:

- `SessionToolCatalog` in `src/ouroboros/orchestrator/mcp_tools.py` merges built-in and attached tools deterministically.
- `serialize_tool_catalog()` persists the same catalog into runtime/session metadata.
- `runner.py`, `parallel_executor.py`, and `runtime_message_projection.py` already thread that catalog through execution state.

Recommendation:

- Do not replace this system.
- Promote it into the canonical engine capability surface.

### 3. Runtime-agnostic philosophy is already explicit in docs

`docs/runtime-capability-matrix.md` already states the right architectural boundary:

- the workflow model is runtime-agnostic
- the runtime backend changes execution surface, not core specification semantics

Recommendation:

- Use that documented boundary as the acceptance test for all future imports from Claude Code.

## What To Introduce

## 1. Promote `SessionToolCatalog` into a semantics-bearing capability graph

### Why

Claude Code's strongest portable idea is not "more tools". It is that the engine should understand what a capability means, not just what it is called.

Right now Ouroboros has:

- stable tool names
- source provenance
- serialization and merge logic

Right now Ouroboros does not yet have a first-class engine-owned notion of:

- read-only vs mutating
- safe-parallel vs must-serialize
- interruptible vs non-interruptible
- approval-sensitive vs non-sensitive
- runtime-local vs attached-capability

### Recommendation

Introduce a new engine-level capability model, for example:

- `CapabilityDescriptor`
- `CapabilitySemantics`
- `CapabilityGraph`

Suggested placement:

- new module: `src/ouroboros/orchestrator/capabilities.py`

Suggested ownership model:

- raw tool shape remains in `MCPToolDefinition`
- engine semantics live in a separate wrapper owned by Ouroboros
- provider adapters should consume this wrapper, not invent semantics themselves

Suggested semantics fields:

- `mutation_class`: `read_only | workspace_write | external_side_effect | destructive`
- `parallel_safety`: `safe | serialized | isolated_session_required`
- `interruptibility`: `none | soft | hard`
- `approval_class`: `default | elevated | bypass_forbidden`
- `origin`: `builtin | attached_mcp | provider_native | future_runtime`
- `scope`: `kernel | sidecar | attachment | shell_only`

Primary touchpoints:

- `src/ouroboros/orchestrator/mcp_tools.py`
- `src/ouroboros/orchestrator/runner.py`
- `src/ouroboros/orchestrator/parallel_executor.py`
- `src/ouroboros/orchestrator/runtime_message_projection.py`

### Decision

- `ADOPT / ADAPT`: high priority

## 2. Add a unified policy plane above runtime-specific permission modes

### Why

Claude Code's `P04` insight maps cleanly to a gap in Ouroboros.

Today permission and capability filtering are split across:

- `allowed_tools` handling in `providers/claude_code_adapter.py`
- `allowed_tools` prompt constraints in `providers/codex_cli_adapter.py`
- permission mode translation in `codex_permissions.py`
- backend selection in `orchestrator/runtime_factory.py`

This means capability visibility and execution authorization are not yet expressed as one engine policy.

### Recommendation

Add an engine-owned policy layer that decides:

- which capabilities are visible to a runtime session
- which capabilities are executable in the current context
- what approval class is required
- which capabilities should be hidden entirely for a coordinator, an interview session, or a parallel AC worker

Suggested placement:

- new module: `src/ouroboros/orchestrator/policy.py`

Suggested model:

- `CapabilityPolicy`
- `PolicyDecision`
- `PolicyContext`

Suggested inputs:

- runtime backend
- execution phase
- session role: implementation / coordinator / interview / evaluation
- current AC scope
- capability semantics from the capability graph

Primary touchpoints:

- `src/ouroboros/providers/claude_code_adapter.py`
- `src/ouroboros/providers/codex_cli_adapter.py`
- `src/ouroboros/codex_permissions.py`
- `src/ouroboros/orchestrator/runtime_factory.py`
- `src/ouroboros/orchestrator/coordinator.py`

### Decision

- `ADOPT / ADAPT`: high priority

## 3. Add an engine-owned execution control plane

### Why

Claude Code's `P02` and `P03` are the most valuable but also the easiest to over-import badly.

Ouroboros already has parallel AC execution in `parallel_executor.py`, but that is AC-level scheduling, not a capability-level control plane. The engine does not yet own a first-class model for:

- tool concurrency safety
- ordered context visibility across tool calls
- serialization requirements for mutating tools
- interrupt and cancellation semantics by capability class

### Recommendation

Do not jump directly to a fully intercepted tool scheduler. That would overfit to a single runtime.

Instead, introduce a layered control plane:

1. model capability semantics
2. derive scheduling hints from semantics
3. apply those hints first to policy, prompts, and audit metadata
4. only later decide whether direct engine-side scheduling/interception is worth the complexity

Suggested placement:

- new module: `src/ouroboros/orchestrator/control_plane.py`

Suggested responsibilities:

- build execution hints from capability semantics
- serialize those hints into runtime/session metadata
- feed coordinator and parallel-executor prompts with explicit allowed execution shapes
- provide a future insertion point for engine-owned tool scheduling if needed

Primary touchpoints:

- `src/ouroboros/orchestrator/parallel_executor.py`
- `src/ouroboros/orchestrator/runner.py`
- `src/ouroboros/orchestrator/runtime_message_projection.py`
- `src/ouroboros/orchestrator/adapter.py`

### Decision

- `ADAPT`: high priority, but staged rollout

## 4. Derive coordinator capability envelopes instead of hardcoding them

### Why

Claude Code's coordinator pattern maps well onto Ouroboros, but Ouroboros currently hardcodes the coordinator envelope:

- `COORDINATOR_TOOLS = ["Read", "Bash", "Edit", "Grep", "Glob"]`
- one static `COORDINATOR_SYSTEM_PROMPT`

This works, but it bypasses the richer engine boundary already forming elsewhere.

### Recommendation

Make coordinator capabilities a derived view over the capability graph and policy plane.

That means:

- the coordinator should receive a role-specific capability envelope
- the envelope should be reproducible from policy
- the envelope should be serializable into runtime state for audit and replay

Primary touchpoints:

- `src/ouroboros/orchestrator/coordinator.py`
- `src/ouroboros/orchestrator/execution_runtime_scope.py`
- `src/ouroboros/orchestrator/level_context.py`

### Decision

- `ADAPT`: medium-high priority

## 5. Keep task/context identity as an outer layer, not a new kernel

### Why

Claude Code suggests promoting task/control identity and cached context projection.

Ouroboros already has precursors:

- `ExecutionRuntimeScope` and `ACRuntimeIdentity` in `execution_runtime_scope.py`
- `SessionTracker` in `session.py`
- `LevelContext` in `level_context.py`

These are enough to justify a future outer layer, but not enough to justify a larger kernel right now.

### Recommendation

- Keep task identity and context projection outside the kernel.
- Promote them only if detached work, pause/resume, and cross-session continuity become a dominant product requirement.

Primary touchpoints if later promoted:

- `src/ouroboros/orchestrator/execution_runtime_scope.py`
- `src/ouroboros/orchestrator/session.py`
- `src/ouroboros/orchestrator/level_context.py`

### Decision

- `DEFER`: medium priority, not immediate

## 6. Treat transport, memory, LSP, hooks, timers, observability, and computer-use as sidecars

### Why

The Claude Code analysis is strongest when it says these patterns are useful but should not define engine identity.

That matches the current Ouroboros philosophy better than pulling them inward.

### Recommendation

Keep these as optional layers:

- transport / runtime reconnect
- memory and consolidation
- observability sinks
- hooks
- LSP
- timer / cron wakeup
- computer-use or browser automation

They may become engine-managed attachments later, but they should not become mandatory kernel contracts now.

### Decision

- `ADAPT LATER`: only as sidecars / attachments

## What To Keep Out Of The Engine

These should stay outside the runtime-agnostic engine boundary:

- CLI and TUI implementation details in `cli/` and `tui/`
- shell/bootstrap composition in runtime startup flows
- auth/browser-consent UX
- installer and update mechanics
- future device-specific or client-specific surfaces such as voice

This is the most important anti-scope rule from the Claude Code analysis.

## Recommended File-Level Roadmap

## Phase 1: Formalize the kernel and capability surface

Goal:

- stabilize what already exists
- add no major behavioral risk

Recommended work:

- add `src/ouroboros/orchestrator/capabilities.py`
- wrap `SessionToolCatalog` entries in engine-owned capability descriptors
- define capability semantics and scope classes
- thread descriptors through `runner.py` and persisted runtime metadata

Expected outcome:

- one canonical capability surface across Claude and Codex runtimes

## Phase 2: Unify policy

Goal:

- stop scattering permission/capability filtering across adapters

Recommended work:

- add `src/ouroboros/orchestrator/policy.py`
- move visibility/execution authorization decisions into engine policy
- make provider adapters consume policy decisions instead of inventing their own filtering logic

Expected outcome:

- one policy decision path across runtimes and session roles

## Phase 3: Add control-plane hints

Goal:

- make scheduling semantics explicit without overcommitting to engine-side interception

Recommended work:

- add `src/ouroboros/orchestrator/control_plane.py`
- derive serial/parallel/isolated/interruptible hints from capability semantics
- persist those hints into runtime metadata and audit events
- expose them to coordinator and parallel AC execution prompts

Expected outcome:

- a runtime-agnostic control plane contract without prematurely rewriting provider behavior

## Phase 4: Optional outer layers

Goal:

- only after product need is proven

Possible later work:

- task/context first-class layer
- sidecar registry for transport, memory, LSP, timers, hooks, computer-use

Expected outcome:

- clean expansion path without kernel pollution

## Recommended Keep / Adapt / Reject Matrix

| Decision | What Ouroboros should do |
| --- | --- |
| `KEEP` | Preserve and formalize the runtime/message kernel already present in `adapter.py`, `runtime_message_projection.py`, and `session.py`. |
| `ADAPT` | Turn `SessionToolCatalog` into a semantics-bearing capability graph and unify policy above provider-specific permission modes. |
| `ADAPT` | Add a control-plane contract that consumes capability semantics and can later evolve into scheduler ownership if needed. |
| `ADAPT` | Derive coordinator capability envelopes from capability/policy instead of hardcoded tool lists. |
| `DEFER` | Promote task/context identity only when detached execution and durable control become central requirements. |
| `REJECT` | Pull CLI/TUI, installer/update, auth UX, or future client-device surfaces into the engine core. |
| `REJECT` | Treat memory, transport, LSP, hooks, timers, or computer-use as kernel identity before product demand proves they belong there. |

## Final Recommendation

The best Claude Code import for Ouroboros is not a shell, not a UI pattern, and not a runtime-specific workflow.

The best import is this engine shape:

`runtime/message kernel -> canonical capability graph -> unified policy plane -> execution control plane -> role-specific capability envelopes -> optional sidecars -> thin shells`

If only one major adoption is funded next, it should be:

- upgrade `SessionToolCatalog` into a semantics-bearing capability graph, then
- build policy and control-plane contracts on top of it

That is the shortest path to making Ouroboros more runtime-agnostic without dragging runtime-specific product surface into the engine.
