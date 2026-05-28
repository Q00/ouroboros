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

### 2.1 Production producers of `control.directive.emitted`

The journal already has four production producers. Two distinct
**failure stances** coexist; subscribers must not assume one stance
covers the whole stream.

| Producer | Code | Target aggregate | Append semantics |
|---|---|---|---|
| `AgentProcessRuntime._make_emitter` | `src/ouroboros/orchestrator/agent_process.py:994-1029` | `("agent_process", process_id)` | **Observational best-effort.** Returns `None` when no `EventStore` is configured. When wired, `event_store.append(...)` is wrapped in `asyncio.wait_for(...)` and **`except Exception` catches and logs `agent_process.directive_emit_failed`** (the #476 "journal stays out of the way" rule). Lifecycle transitions complete regardless of append outcome. |
| `EvolutionLoop._emit_step_directive` | `src/ouroboros/evolution/loop.py:307-339` | `("lineage", lineage_id)` | **Strict, single-event append.** `await self.event_store.append(...)` is uncaught — append failure propagates and aborts the step. |
| `EvolutionLoop._emit_watchdog_timeout_directive` | `src/ouroboros/evolution/loop.py:341-372` | `("lineage", lineage_id)` | **Strict, single-event append.** Same uncaught-`append` shape as the step directive. |
| `GenerationProgressWatchdog.persist_decision` | `src/ouroboros/evolution/watchdog.py:236-308` | `("lineage", lineage_id)` | **Strict, atomic batch append.** Emits the watchdog decision event and its paired directive event via `event_store.append_batch([decision_event, directive_event])`, so both rows commit together or neither does. The contract carries an `idempotency_key` so the projection-level dedupe identity is exposed (`watchdog_directive_idempotency_key`). |

Two implications subscribers must internalize:

1. **Failure stance is per-producer, not uniform.** The
   agent-process producer's catch-and-log behavior is the exception,
   not the rule. Lineage producers raise on append failure.
   Subscribers that infer "every decision the system took shows up in
   the journal" must remember that an `agent_process` emitter may have
   silently dropped its append; they cannot conclude the lifecycle
   transition didn't happen just because the row is absent.
2. **Atomicity stance is per-producer, not uniform.** Only the
   watchdog producer uses `append_batch` to pair a decision event with
   its directive. Single-row producers can be racing other producers
   appending against the same `("lineage", lineage_id)` aggregate, so
   the projection cannot assume "directive immediately follows its
   originating decision row" — only the watchdog producer guarantees
   that.

### 2.2 Other implemented surfaces

| Surface | Status | Code |
|---|---|---|
| `EventStore.append` durability | **Implemented** | `src/ouroboros/persistence/event_store.py:276`. SQLite WAL mode, `synchronous=NORMAL`, `busy_timeout=30000ms`. |
| `EventStore.append_batch` atomic durability | **Implemented** | Used by the watchdog producer; commits all events in the batch in a single transaction. |
| `ControlContract` validation | **Implemented** | `src/ouroboros/core/control_contract.py`. Rejects non-`Directive` values at construction. |
| Aggregate-scoped replay | **Implemented** | `EventStore.get_events_after(aggregate_type, aggregate_id, last_row_id=0)` at `event_store.py:468`. The pagination filter is `rowid > last_row_id`, but the returned batch is ordered by `(timestamp, id)` (`event_store.py:503-509`). The `last_row_id` cursor is therefore a *pagination* cursor: subscribers must advance it only after committing the whole batch they handled, and must not infer global ordering from `rowid`. There is no global cross-aggregate cursor. |
| Aggregate scoping rule | **Locked** | `src/ouroboros/events/control.py:13-30` — every `control.directive.emitted` row is aggregated by `(target_type, target_id)` of the decision target, not by a neutral `"control"` bucket. Replay therefore happens per target (currently `"agent_process"` or `"lineage"`). |
| `ControlBus` plumbing (subscribe / publish API) | **Implemented but unused** | `src/ouroboros/orchestrator/control_bus.py`. A `ControlBus` instance is constructed in `src/ouroboros/mcp/server/adapter.py:1872`, but no production callsite invokes `ControlBus.publish(...)` yet. The bus is intentionally in place ahead of subscribers so the wiring stays stable. |
| Projection | **Implemented** | `ControlDirectiveEmission` in `src/ouroboros/core/lineage.py`; accumulated by `OntologyLineage.with_directive_emission`. |
| Cursor pattern in practice | **Implemented (non-control example)** | `src/ouroboros/auto/listeners.py:319` — `(events, cursor) = await event_store.get_events_after("job", job_id, last_row_id=cursor)` shows the canonical per-aggregate cursor advance against a `"job"` aggregate. The same shape applies to control aggregates `"lineage"` and `"agent_process"`. |

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

Two implied invariants that today's producers exhibit, split by
failure stance:

- **Append-fail behavior is producer-specific.** The agent-process
  producer (`_make_emitter`) catches append failures and logs
  `agent_process.directive_emit_failed`, per the #476 "journal stays
  out of the way" rule. The lineage producers
  (`EvolutionLoop._emit_step_directive`,
  `EvolutionLoop._emit_watchdog_timeout_directive`) and the watchdog
  producer (`GenerationProgressWatchdog.persist_decision` via
  `append_batch`) **do not** catch — append failure propagates and
  aborts the step or watchdog decision. Subscribers that need a
  uniform durability stance must derive it per producer; the journal
  itself does not impose one.
- **Append-succeed, publish-fail must leave the journal authoritative.**
  When a future producer pairs append with publish, a publish failure
  must not roll back the append. The subscriber will see the event on
  its next per-aggregate cursor advance.

## 5. Idempotency contract for subscribers

A subscriber (on `ControlBus` today, on Mesh tomorrow) must satisfy:

1. **Idempotent on the appropriate key.** Use `BaseEvent.id` to drop
   literal row redelivery. Use
   `ControlContract.effective_idempotency_key` to drop logical
   redelivery of the same decision across replay/backfill.
2. **Monotone per-aggregate batch cursor.** `get_events_after`
   returns `(events, max_rowid)`, not a per-event rowid stream. The
   subscriber advances its `last_row_id` for a given
   `(aggregate_type, aggregate_id)` only after committing the batch
   it chose to handle. A crash mid-batch means the whole batch is
   replayed on the next cursor advance for that aggregate;
   subscribers must therefore be idempotent at the batch granularity,
   not the event granularity.
3. **No side effects ahead of the cursor.** A subscriber must not
   commit external state for batches past its persisted
   `last_row_id`. If it does, replay can double-commit.

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
  durability is provided by the producer set inventoried in
  §2.1 (the `agent_process` emitter, the two `EvolutionLoop`
  directive emitters, and the `GenerationProgressWatchdog` atomic
  batch producer) and projections read directly from the journal.
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
