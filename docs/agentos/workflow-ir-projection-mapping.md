# Workflow IR ↔ Projection Mapping Contract

This document pins the **read-only mapping** between the #956 Workflow IR and
the #946 projection vocabulary. It is a contract that downstream Wave 3+ work
(PR F dispatch wiring, S4 StepSnapshot, conformance harness extensions, etc.)
can rely on without re-deriving the boundary each time.

## Purpose & scope

This file is a **mapping reference**, not a schema. It does not introduce new
types, new flags, new event families, or new persistence. It restates how
identifiers and lifecycle vocabulary on either side already line up under the
boundaries fixed by:

- [`workflow-ir-v1.md`](./workflow-ir-v1.md) — what the Workflow IR owns
  (planning graph, validation, lifecycle events) and what it must not embed
  (projection records, dispatch, persistence).
- [`projection-v1-scope.md`](./projection-v1-scope.md) — what the projection
  vocabulary owns (`RunRecord` / `StageRecord` / `StepRecord` /
  `ArtifactRecord` / `VerdictRecord`) as a rebuildable read model over the
  EventStore.

The locked boundary paragraph in `workflow-ir-v1.md` is treated as
authoritative: *"The default boundary fixture must stay local and
deterministic: it may pair a validated `WorkflowSpec` with synthetic
`EventStore` rows to prove source-event linkage, but it must not add
dispatch, cache, persistence, or projection-record embedding to the IR."*

Every consistency test that ships with this mapping doc must obey that
rule — see `tests/integration/test_ir_projection_consistency.py` for the
canonical fixture pattern.

## Identifier mapping

The Workflow IR plans work as a graph of `WorkflowNode` instances connected
by `WorkflowEdge` instances. The #946 projection observes the work that was
emitted to the journal afterwards. The two sides share identifiers in the
following way; they do **not** share storage.

### `WorkflowNode.node_id` → projection step/verdict identity

| Node owner / kind | Projection target | How identifiers line up |
| --- | --- | --- |
| `NodeOwner.AGENT`, `NodeOwner.PLUGIN`, `NodeOwner.HARNESS` with tool/LLM work | `StepRecord` | Runtime callers set `event.data["call_id"] == WorkflowNode.node_id` on the paired `tool.call.started` / `tool.call.returned` (or `llm.call.requested` / `llm.call.returned`) rows. `ProjectionBuilder.stable_step_id(source_key, family, call_id)` then yields a deterministic `StepRecord.step_id` keyed off the node id. The IR side never stores the step id; it stays purely derivable from journal rows. |
| `NodeOwner.AGENT`, `NodeOwner.PLUGIN` producing acceptance evidence | `StepRecord.ac_id` | When the node is the acceptance-criterion anchor for the work, `event.data["ac_id"]` carries the same identifier the IR plan uses for that AC (typically `WorkflowNode.node_id` or a metadata-attached AC label). `ProjectionBuilder._extract_ac_id` lifts it onto the projected `StepRecord` without invention. |
| `NodeOwner.VERIFIER` | `VerdictRecord` | The verifier's `harness.verdict.recorded` / `evaluation.verdict.recorded` event sets `event.data["scope"] = "ac"` and `event.data["ac_id"] == WorkflowNode.node_id` for the AC the verifier judged. `_verdict_from_event` projects that into `VerdictRecord.ac_id`. Run-scope verdicts (`scope == "run"`) project against the run, not a node. |
| `NodeOwner.HUMAN_GATE` | _(not projected in v1)_ | HITL WAIT/RESUME authority lives under #960 and is explicitly deferred by `projection-v1-scope.md`. The mapping leaves these node ids dangling on purpose; the projection has no record kind for them today. |
| `NodeKind.TERMINAL` | `RunRecord` end | Reaching a terminal node corresponds to a terminal `WorkflowLifecycleEventType.RUN_COMPLETED` / `RUN_FAILED` / `RUN_CANCELLED` event, which the run-level `VerdictRecord` (scope `"run"`) projects. The terminal node id itself is not projected as a separate record. |

### `WorkflowEdge.edge_id` → projection event-pair linkage

`WorkflowEdge` instances are **not** projected as a dedicated projection
record kind in v1. Their observability surface is the source-event pair on
either side of the transition:

- `WorkflowLifecycleEventType.EDGE_TRAVERSED` rows carry the `edge_id` and
  the attempt number. They are stored as journal events, not as projection
  rows.
- The projection's `StepRecord.source_event_ids` tuple on the predecessor
  step's `*.returned` event and the successor step's `*.started` event is
  the read-model evidence that the edge was traversed.
- A consumer that wants edge-grained read state can join the journal
  (`edge_id` field on lifecycle events) against the projection's
  `source_event_ids` without the projection needing a new `EdgeRecord`
  kind. **This is intentional.** v1 does not add one.

### Run / stage anchors

- `WorkflowSpec.spec_id` is the lifecycle `workflow_id`. It is **not** a
  projection identifier; the projection keys runs off `seed_id` plus an
  execution / session anchor (see `_derive_projection_source_key`).
- A single `WorkflowSpec` execution maps to exactly one `RunRecord` and at
  least one `StageRecord` (default kind `StageKind.EXECUTE`). Richer stage
  detection is additive follow-up work explicitly deferred by
  `projection-v1-scope.md`.

## Lifecycle event → projection mapping

The Workflow IR's lifecycle vocabulary is bounded
(`WorkflowLifecycleEventType` in
`src/ouroboros/orchestrator/workflow_lifecycle.py`). Each lifecycle event
type is observable through the existing projection vocabulary as follows.

| `WorkflowLifecycleEventType` | Projection effect | Linked via |
| --- | --- | --- |
| `workflow.run.created` | Opens a `RunRecord`. `started_at` is anchored to the earliest projected event timestamp. | `RunRecord.metadata` is the only place a consumer may attach a `workflow_id` provenance label; the record is not extended in v1. |
| `workflow.node.scheduled` | Reserves a future `StepRecord` slot. No `StepRecord` is emitted until a paired `tool.call.*` or `llm.call.*` event exists in the journal. | `StepRecord.source_event_ids` will reference the `*.started` and `*.returned` rows; the scheduled lifecycle row is *not* embedded. |
| `workflow.node.started` | Emits the `*.started` half of the projected `StepRecord`. The dangling step has `ended_at=None` until the matching returned event arrives. | `StepRecord.source_event_ids = (started_event.id,)` until pairing completes. |
| `workflow.node.completed` | Pairs the `StepRecord` with `ended_at` and `ok` derived from the returned event. | `StepRecord.source_event_ids = (started_event.id, returned_event.id)`. |
| `workflow.node.failed` | Same as `completed`, with `StepRecord.ok = False`. The node's `reason_code` lives on the lifecycle event and is **not** copied into the projection. | `StepRecord.source_event_ids`. |
| `workflow.node.retried` | Re-opens the node slot. The previous `StepRecord` retains its `step_id`; the next attempt produces a new `StepRecord` keyed on the same `node_id` (via `call_id`) plus a new `attempt` number. | `StepRecord.source_event_ids` for each attempt. |
| `workflow.edge.traversed` | _Not projected as a record._ Observable via `EDGE_TRAVERSED` lifecycle rows in the journal. | Predecessor `StepRecord` `source_event_ids` cover the read-model evidence. |
| `workflow.checkpoint.saved` | _Not projected in v1._ Checkpoint refs are `RunSnapshotRecord` material in a later projection slice; the mapping doc lists this row deliberately so future PRs know where it lands. | Deferred per `projection-v1-scope.md`. |
| `workflow.run.completed` | Closes the `RunRecord` (`ended_at` is the terminal lifecycle row timestamp). If the runtime also emitted a run-scope verdict event, `RunRecord.verdict_id` points at the projected `VerdictRecord`. | `VerdictRecord.evidence_event_ids` for the run verdict. |
| `workflow.run.failed` | Same as `completed`; the projected run-scope verdict (if any) has `outcome=FAIL`. | `VerdictRecord.evidence_event_ids`. |
| `workflow.run.cancelled` | Same as `completed`; the projected run-scope verdict (if any) has `outcome=CANCELLED`. | `VerdictRecord.evidence_event_ids`. |

The mapping table is **not exhaustive in either direction**. Projection
event families that have no lifecycle equivalent (for example,
`harness.artifact.recorded`) are governed by `projection-v1-scope.md`
alone; lifecycle events that have no projection equivalent
(`workflow.checkpoint.saved`, `workflow.edge.traversed`) are governed by
`workflow-ir-v1.md` alone. This document only locks the **intersection**.

## Anti-actions

This mapping doc explicitly **does not** introduce or imply:

1. **No schema change** to either `src/ouroboros/orchestrator/workflow_ir.py`
   or `src/ouroboros/harness/projection.py`. Both surfaces stay at their
   currently published `*_SCHEMA_VERSION`.
2. **No new field or flag** on any projection record. `legacy_inferred`,
   `source_event_ids`, `ac_id`, and `metadata` are the only surfaces a
   consistency test may rely on. New flags (`workflow_node_id`,
   `edge_id`, etc.) are out of scope.
3. **No live dispatch.** Workflow IR fixtures used to prove this mapping
   stay local and deterministic per the locked boundary paragraph in
   `workflow-ir-v1.md`. No `parallel_executor` call, no agent spawn, no
   plugin command execution.
4. **No projection-record embedding inside the IR.** `WorkflowNode` and
   `WorkflowEdge` continue to carry only their planning vocabulary; they
   do not reference `step_id`, `run_id`, or `verdict_id`.
5. **No IR embedding inside projection records.** `StepRecord.metadata`
   may carry `workflow_node_id` only when the journal event already
   carries it; the projection does not invent or backfill IR identifiers.
6. **No persistence write.** This contract is observed through the
   existing EventStore + projection builder. The mapping doc and its
   tests must not create migrations, caches, or new tables.
7. **No new event family.** The lifecycle event vocabulary
   (`WorkflowLifecycleEventType`) and the projection event-family set
   (`_TOOL_STARTED`, `_TOOL_RETURNED`, `_LLM_REQUESTED`, `_LLM_RETURNED`,
   `_ARTIFACT_RECORDED_TYPES`, `_VERDICT_RECORDED_TYPES`) are both
   closed sets at v1. Adding to either is governed by its own canonical
   issue, not this mapping doc.
8. **No HITL / plugin / evidence schema authority.** The boundary tables
   in `workflow-ir-v1.md` and `projection-v1-scope.md` allocate those to
   #960, #939, and #830/#978 respectively. This document defers to them.

## Verification

The mapping is exercised by
`tests/integration/test_ir_projection_consistency.py`, which builds a
small validated `WorkflowSpec` (fan-out + terminal), emits synthetic
`EventStore` rows that obey the rules above, and asserts that the
projection's identifiers line up with the IR's planned identifiers
exactly. A negative test pins the documented behavior when synthetic
lifecycle events reference a node id that is not in the spec: the
projection still builds without error (because the projection builder is
spec-agnostic by design), and the mismatch is surfaced by the IR side's
existing `validate_workflow_lifecycle_conformance` helper, which emits an
`unknown_node_id` conformance issue. No new flag is added to either side.
