# ControlJournal Delivery & Outbox Semantics

## 1. Status

**Direction locked as Option A — 2026-05-28.** Closes
[#575](https://github.com/Q00/ouroboros/issues/575).

This document records the delivery contract between `ControlContract`
events appended to the `EventStore` and any consumers that read those
events (replay, projections, future in-process `ControlBus`
subscribers, and future cross-process MCP Mesh transports).

The contract is deliberately narrowed to **what current HEAD actually
implements** plus the **forward semantics that any future producer or
subscriber must honor**. It does not retroactively claim a publish
pipeline that does not exist yet; it locks the direction so that the
publish pipeline, when it lands, has only one shape it can legally
take.

The document is reference-only: it does not introduce new types,
events, or wiring.

## 2. What current HEAD implements

| Surface | Status | Code |
|---|---|---|
| Durable append of `control.directive.emitted` | **Implemented (observational-first)** | `AgentProcessRuntime._make_emitter` in `src/ouroboros/orchestrator/agent_process.py` is the only producer wired today. It only appends when an `EventStore` is configured; append failures are caught and logged ("the journal stays out of the way" per #476). |
| `EventStore.append` durability | **Implemented** | `src/ouroboros/persistence/event_store.py:276`. SQLite WAL mode, `synchronous=NORMAL`, `busy_timeout=30000ms`. |
| `ControlContract` validation | **Implemented** | `src/ouroboros/core/control_contract.py`. Rejects non-`Directive` values at construction. |
| Aggregate-scoped replay | **Implemented** | `EventStore.get_events_after(aggregate_type, aggregate_id, last_row_id=0)` at `event_store.py:468`. SQLite implicit `rowid` provides monotone ordering *within* a single `(aggregate_type, aggregate_id)` pair. There is no global cursor. |
| Aggregate scoping rule | **Locked** | `src/ouroboros/events/control.py:13-30` — every `control.directive.emitted` row is aggregated by `(target_type, target_id)` of the decision target, not by a neutral `"control"` bucket. Replay therefore happens per target. |
| `ControlBus` plumbing (subscribe / publish API) | **Implemented but unused** | `src/ouroboros/orchestrator/control_bus.py`. A `ControlBus` instance is constructed in `src/ouroboros/mcp/server/adapter.py:1872`, but no production callsite invokes `ControlBus.publish(...)` yet. The bus is intentionally in place ahead of subscribers so the wiring stays stable. |
| Projection | **Implemented** | `ControlDirectiveEmission` in `src/ouroboros/core/lineage.py`; accumulated by `OntologyLineage.with_directive_emission`. |
| Cursor pattern in practice | **Implemented (non-control example)** | `src/ouroboros/auto/listeners.py:319` — `(events, cursor) = await event_store.get_events_after("job", job_id, last_row_id=cursor)` shows the canonical per-aggregate cursor advance. |

## 3. Decision (Option A, narrowed)

> The **EventStore append is the source of truth.** Any future
> `ControlBus.publish(...)` or cross-process delivery is best-effort
> and recoverable; subscribers that miss a live publish recover by
> replaying the journal **per `(target_type, target_id)`** from a
> per-aggregate cursor. No subscriber needs the bus for correctness.

Concretely, the rules that any new producer or subscriber must honor:

1. **Append-before-publish.** Any future decision site that publishes
   on `ControlBus` must first append the `control.directive.emitted`
   event via the journal-backed producer (today: `_make_emitter`). The
   append is the commit point.
2. **Best-effort publish.** `ControlBus.publish(...)` is in-process,
   fire-and-forget fan-out. Subscriber exceptions do not roll back the
   append. The bus implementation already catches and logs handler
   failures (`control_bus.handler_raised` at
   `src/ouroboros/orchestrator/control_bus.py:180`).
3. **Aggregate-scoped replay.** Subscribers that need durability
   (cross-process, post-restart, late-attaching) read from
   `EventStore.get_events_after(aggregate_type, aggregate_id,
   last_row_id=...)` for each `(target_type, target_id)` they care
   about. There is no global "all directives after cursor N" replay
   path, and the journal contract does not promise one.
4. **Decision-level idempotency via `effective_idempotency_key`.**
   When a `ControlContract` carries an `idempotency_key`, the
   projection-level dedupe identity is
   `(target_type, target_id, directive, idempotency_key)` as exposed
   by `ControlContract.effective_idempotency_key`
   (`src/ouroboros/core/control_contract.py:108-123`). Raw-row
   identity is the event UUID (`BaseEvent.id`, assigned at event
   construction in `src/ouroboros/events/base.py:90`) and is only
   adequate for de-duplicating literal redelivery of the same row,
   not for de-duplicating two appends of the same logical decision.

## 4. Required decisions — answered

These are the seven open questions from RFC #575 with the answer that
current HEAD enforces or that the contract locks forward.

| # | Question | Answer |
|---|----------|--------|
| 1 | Is `ControlBus.publish()` best-effort or guaranteed-after-append? | **Best-effort.** Guarantees come from the journal. No production publish callsite exists today, but when one lands it must follow this rule. |
| 2 | Does EventStore store delivery status? | **No.** Delivery is projection-only (per-subscriber cursor or projection). The event table never gains a `delivered_at` column. |
| 3 | Idempotency key for repeated delivery? | **Two layers.** Raw row: `BaseEvent.id` (UUID assigned at construction). Effective decision: `ControlContract.effective_idempotency_key` returning `(target_type, target_id, directive, idempotency_key)` when an `idempotency_key` is supplied. Use the effective key for replay/backfill/Mesh dedupe; use the raw `id` for in-flight publish dedupe. |
| 4 | Are subscribers required to be idempotent? | **Yes**, by contract. |
| 5 | Can a subscriber request replay from cursor `N`? | **Yes, per aggregate.** `EventStore.get_events_after(aggregate_type, aggregate_id, last_row_id=...)` is the canonical replay path. There is no global cross-aggregate cursor; subscribers maintain one cursor per `(target_type, target_id)` they follow. |
| 6 | How does this map to future MCP Mesh polling/result events? | Mesh transports reuse the per-aggregate cursor contract and the `effective_idempotency_key` for decision dedupe. No new contract surface is required at this layer. |
| 7 | What happens if a subscriber raises? | The bus catches and continues (`control_bus.handler_raised`). The journal is unaffected; replay covers the missed event when the subscriber later advances its cursor. |

Two implied invariants that today's producer already exhibits:

- **Append-fail is observational-only.** `AgentProcessRuntime._make_emitter`
  catches append failures (timeout or otherwise), logs
  `agent_process.directive_emit_failed`, and lets the lifecycle
  transition complete. This is the #476 "journal stays out of the way"
  rule. Callers that need strict durability must wrap the producer
  themselves; the default emitter does not raise.
- **Append-succeed, publish-fail must leave the journal authoritative.**
  When a future producer pairs append with publish, a publish failure
  must not roll back the append. The subscriber will see the event on
  its next cursor advance.

## 5. Idempotency contract for subscribers

A subscriber (on `ControlBus` today, on Mesh tomorrow) must satisfy:

1. **Idempotent on the appropriate key.** Use `BaseEvent.id` to drop
   literal row redelivery. Use
   `ControlContract.effective_idempotency_key` to drop logical
   redelivery of the same decision across replay/backfill.
2. **Monotone per-aggregate cursor.** The subscriber advances its
   `last_row_id` for a given `(aggregate_type, aggregate_id)` only
   after successfully handling that event. A crash mid-handle means
   the event is replayed on the next cursor advance for that
   aggregate.
3. **No side effects ahead of the cursor.** A subscriber must not
   commit external state for events past its persisted `last_row_id`.
   If it does, replay can double-commit.

## 6. Anti-actions

- Do not introduce a `ControlBus` mode that "guarantees" delivery. The
  journal already guarantees what needs guaranteeing; adding a second
  guarantee surface contradicts the elegance bar from #476.
- Do not write delivery status into the EventStore (e.g. a
  `delivered_at` column on the event row). Delivery state is per
  subscriber and lives in the subscriber's cursor or its own
  projection.
- Do not collapse the `control.directive.emitted` append and a future
  `ControlBus.publish` call into a single SQL transaction. The bus
  must not hold the EventStore lock.
- Do not bypass `ControlContract` validation by emitting raw
  `control.directive.emitted` payloads. The construction-time
  `Directive` check is the only guard against directive vocabulary
  rot.
- Do not introduce a neutral `"control"` aggregate bucket to enable a
  global cursor. The target-scoped aggregation is deliberate (per the
  `events/control.py` module docstring); a neutral bucket would
  silently break per-aggregate projectors.

## 7. Future surfaces (out of scope)

- **First production publish callsite.** When any decision site adds
  `ControlBus.publish(...)` after the existing append, that PR is the
  first place this contract gets exercised end-to-end. Until then,
  durability is provided solely by `_make_emitter` and projections
  read directly from the journal.
- **MCP Mesh** (#511) will reuse the per-aggregate cursor contract
  and `effective_idempotency_key` for cross-process delivery. No
  change is required at this contract layer when Mesh lands.
- **Cross-runtime replay** (#1157 L0+): replay from per-aggregate
  cursor is already the canonical pattern. New runtimes only need to
  honor the cursor.
- **Plugin observability hooks** (#939 PR H, deferred): if and when
  `on_event` is promoted out of `ExcludedHookKind`, plugin subscribers
  inherit the same idempotency contract.

## 8. Closure

#575 may close now. The decision is recorded; the runtime surfaces
that already implement the durable half are cited; the forward
constraints on a future publish pipeline are locked. New questions
about control event delivery should land as comments on the canonical
surface they actually touch — usually the consumer in
`auto/listeners.py`, the producer in
`orchestrator/agent_process.py`, the bus in
`orchestrator/control_bus.py`, or the EventStore — rather than
re-opening #575.
