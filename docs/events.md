# Event Payload Schema Reference

This document defines the stable payload fields for Ouroboros EventStore
events. Consumers that read events -- TUI, `ooo status`, `ooo resume-session`,
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

Legacy execution event emitted when a worker execution unit associated with a
source acceptance criterion finishes. Despite the `ac` name and the historical
`passed`/`failed` status values, this event records **worker task completion**,
not a formal acceptance-criterion verdict. Formal AC verdicts are produced by
the evaluation pipeline (`ACResult` / `EvaluationSummary.ac_results`).

The event name and payload remain documented for compatibility with existing
EventStore consumers. New code that needs task-native execution events should
prefer an additive task/node event family instead of overloading this legacy
name further.

| Field | Type | Description |
|-------|------|-------------|
| `ac_id` | `string` | Legacy source acceptance-criterion identifier for the execution unit |
| `status` | `string` | Legacy worker completion status: `"passed"` means completed, `"failed"` means failed |

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

### workflow.run.created / completed / failed / cancelled

Durable lifecycle events for #956 Workflow IR runs. All events share the
``workflow_ir`` aggregate type and use ``WorkflowSpec.spec_id`` as
``aggregate_id``. See ``docs/agentos/workflow-ir-v1.md`` for the boundary
contract; ``#1131`` adds the durable lifecycle family on top.

| Field | Type | Description |
|-------|------|-------------|
| `workflow_id` | `string` | ``WorkflowSpec.spec_id`` (mirrors ``aggregate_id``) |
| `schema_version` | `int` | Lifecycle schema version (currently `1`) |
| `timestamp` | `string` | ISO 8601 UTC timestamp |
| `reason_code` | `string?` | Required on `run.failed` and `run.cancelled` |
| `refs` | `string[]?` | Bounded ``ControlContract`` / ``IOJournal`` ids — never raw payload |

### workflow.node.scheduled / started / completed / failed / retried

Per-node lifecycle records anchored to a ``WorkflowNode.node_id``.

| Field | Type | Description |
|-------|------|-------------|
| `workflow_id` | `string` | ``WorkflowSpec.spec_id`` (mirrors ``aggregate_id``) |
| `node_id` | `string` | ``WorkflowNode.node_id`` |
| `attempt` | `int?` | Node attempt number (>= 1); absent on run-level events |
| `reason_code` | `string?` | Required on `node.failed` and `node.retried` |
| `data` | `object?` | Bounded, redacted hints — raw prompt/stdout/stderr/credentials are rejected by validation |

### workflow.edge.traversed

Records that an ``WorkflowEdge.edge_id`` was traversed during execution.

| Field | Type | Description |
|-------|------|-------------|
| `edge_id` | `string` | ``WorkflowEdge.edge_id`` |
| `attempt` | `int?` | Source node attempt at traversal time |

### workflow.checkpoint.saved

Links a checkpoint save to its ``CheckpointStore`` reference ids.

| Field | Type | Description |
|-------|------|-------------|
| `refs` | `string[]` | One or more bounded checkpoint references |

The lifecycle family is registered on the EventStore via
``append_workflow_lifecycle_event`` / ``replay_workflow_lifecycle``. No
existing event family is modified. Payloads are size-bounded
(``MAX_WORKFLOW_LIFECYCLE_DATA_BYTES``) and reject replay-unsafe keys
(``stdout``, ``stderr``, ``prompt``, ``api_key``, ``token`` and similar
secret/raw-output names) so durable lifecycle history can be replayed
without leaking raw payload material.

## Adding new event types

When introducing a new event type:

1. Add a factory function in `src/ouroboros/events/`.
2. Document the payload fields in this file under the current version.
3. Existing consumers are not affected — new types are additive.

When changing an existing event type's payload:

1. If adding a new field: add it here, no version bump needed.
2. If removing or renaming a field: bump `event_version` in `BaseEvent`,
   document the change under the new version heading, and update consumers.
