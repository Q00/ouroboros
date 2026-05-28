# ControlJournal Delivery & Outbox Semantics

## 1. Status

**Locked as Option A — 2026-05-28.** Closes
[#575](https://github.com/Q00/ouroboros/issues/575).

This document records the *as-implemented* delivery contract between
`ControlContract` events appended to the `EventStore` and in-process
subscribers wired through `ControlBus`. The semantics are not new — they
have been the runtime behavior since #476's first migration site landed
— but they were never written down as the answer to #575. This doc
fixes that so #575 can close without losing the decision record.

The document is reference-only: it does not introduce new types,
events, or wiring.

## 2. Decision

> The **EventStore append is the source of truth.** `ControlBus`
> delivery is best-effort and recoverable; subscribers that miss a live
> publish replay from the journal cursor. No subscriber needs the bus
> for correctness.

This is Option A from RFC #575 ("Journal-backed outbox"), narrowed to
what the code actually does today:

1. The decision site constructs a `ControlContract` and appends a
   `control.directive.emitted` event to `EventStore` **before** any
   `ControlBus.publish(...)` call. The append is the commit point.
2. `ControlBus.publish(...)` is a best-effort, in-process fan-out. If
   it raises, the journal is still authoritative.
3. Subscribers that need durability (cross-process, post-restart,
   late-attaching) read from `EventStore.get_events_after(...)` via a
   monotonically advancing `last_row_id` cursor.
4. Replay is idempotent because every consumer is expected to be
   idempotent on `(event_type, event_id)` — see § 5.

## 3. Code surfaces

| Concept | Module / function | Notes |
|---|---|---|
| Durable append | `EventStore.append` in `src/ouroboros/persistence/event_store.py` (l. 276) | SQLite WAL mode, `synchronous=NORMAL`, `busy_timeout=30000ms`. |
| Cursor-based replay | `EventStore.get_events_after(last_row_id)` in `event_store.py` (l. 468) | Uses SQLite implicit `rowid` for monotone ordering; returns events plus the max rowid for the next call. |
| Control payload | `ControlContract` in `src/ouroboros/core/control_contract.py` | Rejects non-`Directive` values at construction. |
| In-process delivery | `ControlBus.publish/subscribe` in `src/ouroboros/orchestrator/control_bus.py` | Best-effort; never holds the EventStore lock. |
| Projection | `ControlDirectiveEmission` in `src/ouroboros/core/lineage.py` | Frozen read-model accumulated by `OntologyLineage.with_directive_emission`. |
| Cursor pattern in practice | `src/ouroboros/auto/listeners.py:319` | Demonstrates `(events, cursor) = await event_store.get_events_after("job", job_id, last_row_id=cursor)`. |

## 4. Required decisions — answered

These are the seven open questions from RFC #575, with the answer that
the code already enforces.

| # | Question | Answer |
|---|----------|--------|
| 1 | Is `ControlBus.publish()` best-effort or guaranteed-after-append? | **Best-effort.** Guarantees come from the journal. |
| 2 | Does EventStore store delivery status? | **No.** Delivery is projection-only (per-subscriber cursor). |
| 3 | Idempotency key for repeated delivery? | `(event_type, event_id)`. `event_id` is assigned at append. |
| 4 | Are subscribers required to be idempotent? | **Yes**, by contract. |
| 5 | Can a subscriber request replay from cursor `N`? | **Yes.** `EventStore.get_events_after(...)` is the canonical replay path. |
| 6 | How does this map to future MCP Mesh polling/result events? | Mesh transports reuse the same cursor + idempotency-key contract. No new contract is required. |
| 7 | What happens if a subscriber raises? | The bus catches and continues. The journal is unaffected; replay covers the missed event. |

Two implied invariants:

- **Append-fail → publish does not happen.** If `EventStore.append`
  raises, the decision site re-raises and no `ControlBus.publish` is
  issued. The caller may retry the append; idempotency must be
  preserved by the caller (typically via `(decision_site, lineage_id,
  step_id)`).
- **Append-succeed, publish-fail → journal wins.** The subscriber will
  see the event on its next cursor advance. The bus failure is logged
  but does not roll back the append.

## 5. Idempotency contract for subscribers

A `ControlBus` subscriber must satisfy:

1. **Idempotent on event_id** — applying the same `event_id` twice has
   the same effect as applying it once.
2. **Monotone cursor** — the subscriber advances its `last_row_id`
   only after successfully handling the event. A crash mid-handle
   means the event is replayed on the next cursor advance.
3. **No side effects ahead of the cursor** — a subscriber must not
   commit external state for events past `last_row_id`. If it does,
   replay can double-commit.

## 6. Anti-actions

- Do not introduce a `ControlBus` mode that "guarantees" delivery. The
  journal already guarantees it; adding a second guarantee surface
  contradicts the elegance bar from #476.
- Do not write delivery status into the EventStore (e.g., a
  `delivered_at` column on the event row). Delivery state is per
  subscriber and lives in the subscriber's cursor or its own projection.
- Do not collapse the `control.directive.emitted` append and the
  `ControlBus.publish` call into a single SQL transaction. The bus must
  not hold the EventStore lock.
- Do not bypass `ControlContract` validation by emitting raw
  `control.directive.emitted` payloads. The construction-time
  `Directive` check is the only guard against directive vocabulary
  rot.

## 7. Future surfaces (out of scope)

- **MCP Mesh** (#511): will reuse the cursor + idempotency contract
  defined here for cross-process delivery. No change required at the
  contract layer when Mesh lands.
- **Cross-runtime replay** (#1157 L0+): replay from journal cursor is
  already the canonical pattern. New runtimes only need to honor the
  cursor.
- **Plugin observability hooks** (#939 PR H, deferred): if and when
  `on_event` is promoted out of `ExcludedHookKind`, plugin subscribers
  inherit the same idempotency contract.

## 8. Closure

#575 may close now. New questions about control event delivery should
land as comments on the canonical surface they actually touch — usually
the consumer in `auto/listeners.py`, the bus subscriber in question, or
the EventStore — rather than re-opening #575.
