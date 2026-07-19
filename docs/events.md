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

### execution.ac.capsule.compiled

Authority-bearing event emitted before a provider driver may start an AC
attempt. Unlike observe-only telemetry, persistence failure blocks dispatch.
The capsule makes Ouroboros — rather than Claude, Codex, or another provider —
the owner of AC identity, context references, workspace authority, and native
session freshness.

| Field | Type | Description |
|-------|------|-------------|
| `execution_id` | `string` | Durable execution/run anchor |
| `session_id` | `string` | Orchestrator session id |
| `semantic_ac_key` | `string` | Stable semantic AC identity |
| `capsule_fingerprint` | `string` | SHA-256 identity of the complete capsule contract |
| `capsule_manifest` | `object` | Strict versioned manifest whose free-form values and paths are represented only by SHA-256 digests |
| `session_origin` | `string` | `"fresh"` or `"restored_same_attempt"` |

The nested manifest contains compact identities and typed context-reference
digests, not a provider transcript or copies of free-form Seed/prompt/workspace
text. Recovery strictly parses and re-fingerprints the manifest before a native
runtime handle may resume the same AC attempt.

When the prompt budget cannot include every optional reference, the manifest
also records `omitted_context_count` and a rolling `omitted_context_digest`.
This is an auditable bounded-retrieval fact, not a claim that a page resolver
already exists.

### execution.ac.attempt.dispatched

Authority-bearing transition persisted immediately before Ouroboros invokes a
provider for an AC attempt, including resumed SessionSignal turns. A compiled
capsule alone does not prove whether a particular provider boundary was crossed;
this event makes every entry distinct and durable. Persistence failure blocks
that provider invocation.

| Field | Type | Description |
|-------|------|-------------|
| `ac_dispatch_id` | `string` | Unique correlation id for this exact provider entry |
| `previous_ac_dispatch_id` | `string \| null` | Exact predecessor boundary in the same attempt; `null` only for the chain root |
| `dispatch_kind` | `string` | Provider-entry phase: `"primary"` replays the AC prompt, `"session_signal_followup"` replays a Synapse signal turn. Absent on legacy records is treated as `"primary"`. |
| `signal_id` | `string \| null` | SessionSignal id (follow-up dispatches only) |
| `signal_mode` | `string \| null` | Effective signal mode, e.g. `"inform"`/`"after_turn"` (follow-up dispatches only) |
| `follow_up_input_digest` | `string \| null` | `sha256:` digest of the exact rendered follow-up prompt (follow-up dispatches only) |
| `execution_id` | `string` | Durable execution/run anchor |
| `session_id` | `string` | Orchestrator session id |
| `capsule_fingerprint` | `string` | Capsule authority used for this dispatch |
| `session_origin` | `string` | `"fresh"` or `"restored_same_attempt"` |
| `runtime_backend` | `string \| null` | Provider-neutral runtime selector |
| `runtime` | `object \| null` | Minimal reconnect/authority handle; excludes cwd, transcript, tool catalog, capability graph, control plane, and arbitrary metadata |

A non-primary (`session_signal_followup`) head fails closed on recovery: the
exact follow-up phase is not reconstructed from persisted metadata, so replaying
the original AC prompt is refused rather than risking repeated acceptance work.
Capsule authority also binds the actual subprocess `executable` (canonical path
and realpath) and any launcher (e.g. a Zcode Electron/Node host), so a restart
cannot resume the same capsule under a different binary. For a runtime that wraps
a transport (e.g. `LeaderDrivenWorkerRuntime`), the transport's delegated
`executable_identity_contract()` also contributes its command-affecting policy
(e.g. Claude's `--add-dir` / `--disallowedTools`), so a change in launched-binary
access changes authority too.

Lifecycle events carry the same `ac_dispatch_id`. Dispatch events form one
validated predecessor chain, and recovery considers only its unique head; it
never infers a successor from timestamps, replay-list position, or lifecycle
event type priority across boundaries. Only a correlated reusable same-attempt
handle at that head can resume. Terminal failure takes precedence over a
resumable handle: a head with a durable `execution.session.failed` is NOT
resumed even when it carries a reusable handle, because provider effects may
precede the reported failure and resuming would send another provider turn. The
only safe resume of a failed head is an explicit `execution.session.recovered`
linkage to a live replacement session; without it, recovery fails closed. A
completed head blocks same-attempt redispatch as already terminal and
reconstructs its durable successful result for the caller. When the AC ran a
verify gate, the completed lifecycle also carries a bounded exact-shape
`verify_gate_outcome`, so crash recovery does not replay a potentially
non-idempotent `verify_command`. Recovery must not manufacture a fresh provider
session merely because no first streamed message became durable.

If `execution.session.recovered` explicitly links a failed resume id to a
replacement resume id, that durable edge supersedes the failed handle. Absent
such a linkage, conflicting session ids or equally authoritative response-chain
cursors remain ambiguous and fail closed.

The recovery handle contract is exact-shape, metadata-allowlisted, and bounded
to 64 KiB (measured in UTF-8 bytes) before deserialization. Persisted payloads
cannot smuggle excluded runtime context back into replay through unknown fields.

### execution.ac.dispatch.sealed

Seals an already-completed provider turn immediately before a SessionSignal
follow-up dispatch supersedes it. If the follow-up's own dispatch record fails
to persist — or a crash lands before the follow-up reaches a terminal outcome —
the predecessor becomes the chain head again. A sealed head with no durable
terminal completion fails closed on recovery instead of resuming and replaying
the original AC prompt over already-completed work.

| Field | Type | Description |
|-------|------|-------------|
| `ac_dispatch_id` | `string` | The predecessor dispatch being sealed |
| `execution_id` | `string` | Durable execution/run anchor |
| `session_id` | `string` | Orchestrator session id |
| `capsule_fingerprint` | `string` | Capsule authority of the sealed dispatch |

### execution.ac.verify.intent / execution.ac.verify.outcome

Make a non-idempotent `verify_command` single-shot across crash recovery. The
intent is persisted immediately before the command runs and the outcome
immediately after; both are keyed to a stable `verify_key` (the AC/node identity
plus retry attempt) and a `verify_command_digest` binding the exact command and
contract. On recovery the orchestrator consumes a recorded outcome instead of
re-running the command; an intent with no matching outcome — the command may
already have run — fails closed rather than replaying it. Applies to both atomic
and decomposed-parent verify gates.

| Field | Type | Description |
|-------|------|-------------|
| `verify_key` | `string` | Stable per-attempt gate identity. Namespaces: `atomic:…` (atomic gate), `decomposed_parent:…` (decomposed-parent gate), `apply_gate:…` (failed-runtime recovery gate), `flip_gate:…` (sibling-evidence flip gate), `skip_completed:…` (`--skip-completed` proof gate) |
| `verify_command_digest` | `string` | Digest of the exact command, expected artifacts, and output assertion |
| `execution_id` | `string` | Durable execution/run anchor |
| `session_id` | `string` | Orchestrator session id |
| `verify_gate_outcome` | `object` | (outcome only) Bounded exact-shape gate result: `passed`, `reason`, `output_tail`, `missing_artifacts` |

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
