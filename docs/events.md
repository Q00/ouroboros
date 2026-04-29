# Event Payload Schema Reference

This document defines the stable payload fields for Ouroboros EventStore
events. Consumers that read events -- TUI, `ooo status`, `ooo resume`,
`ouroboros_query_events` -- can rely on these
fields not being removed or renamed within a given `event_version`.

## Versioning

All events persisted by Ouroboros include an `event_version` integer inside
their JSON payload.

| Version | Meaning |
|---------|---------|
| `0` | Legacy event written before schema stabilization (field absent) |
| `1` | Baseline stable schema (this document) |

**Stability guarantee:** fields documented under a given version will not be
removed or renamed within that version. New fields may be added at any time.

When `event_version` is bumped, consumers should check the version before
parsing and fail explicitly on unsupported versions rather than silently
misinterpreting changed fields.

## How event_version is stored

`event_version` lives inside the `payload` JSON column — not as a separate
database column. This avoids schema migrations and keeps the change additive.

```
events table row:
  id            = "abc-123"
  event_type    = "orchestrator.session.started"
  payload       = {"execution_id": "exec-1", ..., "event_version": 1}
  timestamp     = 2026-04-15T00:00:00Z
```

`BaseEvent.from_db_row()` extracts `event_version` from the payload and
exposes it as a first-class attribute. It does not appear in `event.data`.

## Event Type Schemas (Version 1)

### orchestrator.session.started

Emitted when a new orchestrator session begins execution.

| Field | Type | Description |
|-------|------|-------------|
| `execution_id` | `string` | Unique execution identifier |
| `seed_id` | `string` | Seed specification being executed |
| `start_time` | `string` | ISO 8601 timestamp of session start |

### orchestrator.session.completed

Emitted when a session finishes successfully.

| Field | Type | Description |
|-------|------|-------------|
| `summary` | `string` | Human-readable completion summary |

### orchestrator.session.cancelled

Emitted when a session is cancelled by the user or by auto-cleanup.

| Field | Type | Description |
|-------|------|-------------|
| `reason` | `string` | Why the session was cancelled |
| `cancelled_by` | `string` | `"user"`, `"auto_cleanup"`, or agent identifier |

### orchestrator.session.failed

Emitted when a session terminates due to an error.

| Field | Type | Description |
|-------|------|-------------|
| `error` | `string` | Error description |

### execution.ac.completed

Emitted when an individual Acceptance Criterion finishes execution.

| Field | Type | Description |
|-------|------|-------------|
| `ac_id` | `string` | Acceptance criterion identifier |
| `status` | `string` | `"passed"` or `"failed"` |

### ac.decomposition.completed

Emitted when a parent Acceptance Criterion is decomposed into child ACs.

| Field | Type | Description |
|-------|------|-------------|
| `execution_id` | `string` | Execution identifier associated with the decomposition |
| `child_ac_ids` | `string[]` | Generated child Acceptance Criterion identifiers |
| `child_contents` | `string[]` | Generated child Acceptance Criterion statements |
| `child_ac_nodes` | `object[]?` | Materialized child AC nodes for replay; each node includes `originating_subcall_trace_id` when created from a Hermes/RLM sub-call trace |
| `hermes_subquestion_results` | `object[]?` | Structured Hermes decomposition results mapped to generated child AC IDs |
| `depth` | `integer` | Parent AC depth before decomposition |
| `reasoning` | `string` | Decomposition rationale |

### rlm.hermes.call.started

Emitted by the isolated `ooo rlm` path before Ouroboros invokes Hermes as the
inner language model.

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `string` | RLM trace schema, currently `"rlm.trace.v1"` |
| `trace_id` | `string?` | Stable trace-record identifier for this persisted sub-call record |
| `subcall_id` | `string?` | Ouroboros-owned stable sub-call identity; defaults to `hermes.call_id` when not separately assigned |
| `parent_trace_id` | `string?` | Stable parent trace-record identifier for reconstructing recursive trace ancestry |
| `causal_parent_event_id` | `string?` | EventStore event ID that causally scheduled or preceded this sub-call |
| `generation_id` | `string?` | RLM run/generation identifier |
| `mode` | `string` | Hermes sub-call mode, such as `"decompose_ac"` or `"execute_atomic"` |
| `rlm_node` | `object?` | Linked RLM node metadata, including `id` and `depth` when available |
| `ac_node` | `object?` | Linked AC node metadata, including `id`, `depth`, and `child_ids` when generated child AC nodes are recorded |
| `context.selected_chunk_ids` | `string[]` | Context chunks supplied to this Hermes call |
| `recursion.generated_child_ac_node_ids` | `string[]` | Generated child AC node IDs created by the outer scaffold from this sub-call |
| `replay.creates_ac_node_ids` | `string[]` | Replay-oriented alias for generated child AC node IDs |
| `hermes.call_id` | `string?` | Hermes sub-call identifier |
| `hermes.subcall_id` | `string?` | Stable sub-call identity duplicated in the Hermes fragment for flat trace readers |
| `hermes.parent_call_id` | `string?` | Parent Hermes call identifier for recursive/chunk calls |
| `hermes.runtime` | `string` | Runtime adapter name, normally `"hermes"` |
| `hermes.resume_handle_id` | `string?` | Readable resume handle identifier when available |
| `hermes.runtime_handle_id` | `string?` | Readable runtime handle identifier when available |
| `hermes.prompt` | `string` | Rendered prompt envelope sent to Hermes |
| `hermes.prompt_hash` | `string?` | Stable hash of the prompt payload |
| `hermes.system_prompt_hash` | `string?` | Stable hash of the system-prompt policy when recorded |
| `hermes.depth` | `integer` | Recursive Hermes call depth |

### rlm.hermes.call.completed

Emitted by the isolated `ooo rlm` path after a Hermes inner-model sub-call
returns or fails.

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `string` | RLM trace schema, currently `"rlm.trace.v1"` |
| `trace_id` | `string?` | Stable trace-record identifier for this persisted sub-call record |
| `subcall_id` | `string?` | Ouroboros-owned stable sub-call identity; defaults to `hermes.call_id` when not separately assigned |
| `parent_trace_id` | `string?` | Stable parent trace-record identifier for reconstructing recursive trace ancestry |
| `causal_parent_event_id` | `string?` | EventStore event ID that causally scheduled or preceded this sub-call |
| `generation_id` | `string?` | RLM run/generation identifier |
| `mode` | `string` | Hermes sub-call mode, such as `"decompose_ac"` or `"execute_atomic"` |
| `rlm_node` | `object?` | Linked RLM node metadata, including `id` and `depth` when available |
| `ac_node` | `object?` | Linked AC node metadata, including `id`, `depth`, and `child_ids` when generated child AC nodes are recorded |
| `context.selected_chunk_ids` | `string[]` | Context chunks supplied to this Hermes call |
| `recursion.generated_child_ac_node_ids` | `string[]` | Generated child AC node IDs created by the outer scaffold from this sub-call |
| `replay.creates_ac_node_ids` | `string[]` | Replay-oriented alias for generated child AC node IDs |
| `hermes.call_id` | `string?` | Hermes sub-call identifier |
| `hermes.subcall_id` | `string?` | Stable sub-call identity duplicated in the Hermes fragment for flat trace readers |
| `hermes.parent_call_id` | `string?` | Parent Hermes call identifier for recursive/chunk calls |
| `hermes.completion` | `string` | Raw Hermes final message |
| `hermes.response_hash` | `string?` | Stable hash of the completion payload |
| `hermes.success` | `boolean?` | Whether the Hermes sub-call completed successfully |
| `hermes.exit_code` | `integer?` | Normalized process/task exit code |
| `hermes.elapsed_ms` | `integer?` | Elapsed call time in milliseconds when available |
| `hermes.adapter_error` | `object?` | Adapter/provider error details when the call fails |
| `hermes.runtime_handle_id` | `string?` | Readable runtime handle identifier when available |

### mcp.job.cancelled

Emitted when a background MCP job is cancelled.

| Field | Type | Description |
|-------|------|-------------|
| `status` | `string` | Always `"cancelled"` |
| `message` | `string` | Human-readable cancellation message |

### orchestrator.progress.updated

Emitted periodically during execution with runtime progress.

| Field | Type | Description |
|-------|------|-------------|
| `progress` | `object` | Nested progress state (structure varies by runtime) |
| `progress.runtime_status` | `string?` | Runtime-reported status when available |

## Adding new event types

When introducing a new event type:

1. Add a factory function in `src/ouroboros/events/`.
2. Document the payload fields in this file under the current version.
3. Existing consumers are not affected — new types are additive.

When changing an existing event type's payload:

1. If adding a new field: add it here, no version bump needed.
2. If removing or renaming a field: bump `event_version` in `BaseEvent`,
   document the change under the new version heading, and update consumers.
