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

Emitted when an individual Acceptance Criterion finishes execution.

| Field | Type | Description |
|-------|------|-------------|
| `ac_id` | `string` | Acceptance criterion identifier |
| `status` | `string` | `"passed"` or `"failed"` |

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
