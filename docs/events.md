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
| `project_id` | `string` | Optional Project Map V1 join key (`project_` + full UUIDv5 hex); present on runner-owned new sessions |
| `project_root` | `string` | Optional canonical identity root used to derive `project_id`; normally the source checkout, or a validated common Git directory for positively proven linked peers with no primary owner |
| `workspace_path` | `string` | Optional canonical POSIX workspace scope relative to the active checkout root |

The three project fields are one additive identity anchor and must be consumed
together. Historical events may omit all three. They grant no execution or
acceptance authority; see [Project Map V1](./rfc/project-map-v1.md).

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

### execution.ac.capsule.compiled

Authority-bearing event written before an AC provider attempt. It stores only
the versioned redacted capsule manifest and its fingerprint; raw prompts,
credentials, transcripts, and absolute workspace paths are never persisted.

### execution.ac.attempt.dispatched

Authority-bearing provider-entry boundary written immediately before a runtime
is invoked. It carries a unique `ac_dispatch_id`, the capsule fingerprint, and
the minimal reconnect handle. A missing prerequisite capsule or a mismatched
fingerprint is not recoverable.

### execution.ac.dispatch.sealed

Fail-closed marker for a provider boundary whose effects may have occurred but
whose terminal result is not yet durable. Recovery must not redispatch a sealed
attempt; a later terminal lifecycle event supersedes the seal.

### mcp.job.created

Emitted when a background MCP job is created. Its owner fields let a later
process determine whether recovery is authorized.

| Field | Type | Description |
|-------|------|-------------|
| `owner_pid` | `integer` | Owning process ID; must agree with `owner_identity.pid` when the versioned identity is present |
| `owner_start_time` | `number?` | Legacy epoch process start time used for liveness on non-Linux platforms |
| `owner_identity` | `object?` | Versioned Linux owner identity; absent from historical events, non-Linux events, or when the Linux kernel identity cannot be read |
| `owner_identity.version` | `integer` | Identity schema version; currently `1` |
| `owner_identity.platform` | `string` | Always `"linux"` for version `1` |
| `owner_identity.pid` | `integer` | Positive process ID matching `owner_pid` |
| `owner_identity.boot_id` | `string` | Canonical UUID-shaped Linux kernel boot ID |
| `owner_identity.start_ticks` | `integer` | Non-negative raw `/proc/<pid>/stat` process start ticks |

`owner_identity` is additive within event version 1. Linux recovery readers
must treat a missing, malformed, or unsupported identity as unknown and must
not fall back to the legacy epoch field to prove owner death. Non-Linux readers
retain the legacy `owner_pid` plus `owner_start_time` behavior.

### mcp.job.cancelled

Emitted when a background MCP job is cancelled.

| Field | Type | Description |
|-------|------|-------------|
| `status` | `string` | Always `"cancelled"` |
| `message` | `string` | Human-readable cancellation message |

### artifact.referenced

Bounded Disposable Memory projection emitted only after the body is durable.
The aggregate is `contract/<contract_id>`. Raw child output and transcripts are
forbidden from this event; consumers must call the explicit artifact
fetch/replay API.

The event id is a UUIDv5 over the contract id alone, and exactly-once appending
matches on that id. A row written before the derivation dropped the content
address carries a different id and names a body in the filesystem store, so it
does not stand in for a publication into the database — a contract re-run after
the cutover appends its own row rather than being masked by the older one.

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `int` | Disposable envelope schema, currently `2`. Version `1` additionally required `artifact_ref`; rows written under it keep that field and keep saying `1`, so a reader distinguishes the two shapes by this value |
| `contract_id` | `string` | Contract owning this result |
| `result.status` | `string` | `completed` or `failed` |
| `runtime_id` | `string` | Runtime that produced the artifact |
| `duration_ms` | `int` | Non-negative child duration |
| `events_emitted_count` | `int` | Runtime-authored event count; never inline events |

### orchestrator.progress.updated

Emitted periodically during execution with runtime progress.

| Field | Type | Description |
|-------|------|-------------|
| `progress` | `object` | Nested progress state (structure varies by runtime) |
| `progress.runtime_status` | `string?` | Runtime-reported status when available |

### Interview latency events

The following interview events carry the same privacy-safe `timings_ms`
object for newly measured handler turns:

- `interview.response.emitted`: a question-bearing start, answer, or resume response.
- `interview.completed`: a turn that completes without returning another question.
- `interview.failed`: a terminal failure, including completion and question generation.
- `interview.question_generation.parent_handoff`: question generation delegated to the
  parent session after a provider envelope violation.

Historical rows written before phase timing was introduced may not contain
`timings_ms`. A `null` phase in a new timing object means that phase did not
execute during the measured turn; it does not mean zero milliseconds.

| `timings_ms` field | Type | Description |
|--------------------|------|-------------|
| `total` | `number?` | Monotonic server-side handler time through terminal event creation |
| `ambiguity_scoring` | `number?` | Time spent in live ambiguity scoring |
| `question_generation` | `number?` | Time spent awaiting `ask_next_question` |
| `advisory_build` | `number?` | Time spent preparing server-side advisory request metadata |

`total` is not user-observed wall time. It excludes work performed by the host
after the MCP response returns. In particular, `advisory_build` does not
measure execution of host-side advisory fan-out. `question_generation` may
include adapter/client startup and provider latency because those operations
remain inside the question-generation boundary.

Event-specific fields remain additive. The benchmark exporter allowlists only
the non-content metadata needed for phase analysis:

| Event | Additional fields |
|-------|-------------------|
| `interview.response.emitted` | `response_kind`, `round_number`, `payload_chars`, `transcript_chars`, `ambiguity_prefix_present`, `is_length_guard` |
| `interview.completed` | `total_rounds` |
| `interview.failed` | `phase`, existing bounded `error` diagnostics |
| `interview.question_generation.parent_handoff` | `phase`, `reason_code`, optional `provider_error_type` |

Use `scripts/export_interview_latency.py` when sharing benchmark evidence. The
exporter excludes failure text and all non-allowlisted payload fields.

### workflow.run.created / completed / failed / cancelled

Durable lifecycle events for #956 Workflow IR runs. All events share the
``workflow_ir`` aggregate type and use ``WorkflowSpec.spec_id`` as
``aggregate_id``. See ``docs/agentos/workflow-ir-v1.md`` for the boundary
contract; ``#1134`` adds the durable lifecycle family on top.

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
(``MAX_WORKFLOW_LIFECYCLE_DATA_BYTES``), refs are count/per-ref/serialized
size-bounded, and both reject replay-unsafe names (``stdout``, ``stderr``,
``prompt``, ``api_key``, ``token`` and similar secret/raw-output names) so
durable lifecycle history can be replayed without leaking raw payload material.

## Adding new event types

When introducing a new event type:

1. Add a factory function in `src/ouroboros/events/`.
2. Document the payload fields in this file under the current version.
3. Existing consumers are not affected — new types are additive.

When changing an existing event type's payload:

1. If adding a new field: add it here, no version bump needed.
2. If removing or renaming a field: bump `event_version` in `BaseEvent`,
   document the change under the new version heading, and update consumers.
