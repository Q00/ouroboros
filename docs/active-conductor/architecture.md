# Active Conductor Architecture

> Generated: 2026-07-12
> Approach: staged, event-authoritative successor recovery

## Overview

The active conductor extends the delegated observer without taking its cursor.
The server converts authoritative durable events into bounded relay envelopes.
The observer relays those envelopes, and the main host session verifies and
selects a safe action. Recovery, artifact, routing, and specification mutation is
allowed only after engine recovery is closed and always creates a successor
rather than racing the current worker. Bounded user-intent messages use the
separate safe-boundary control path below.

## System diagram

```text
┌──────────────────── engine-owned ────────────────────┐
│ worker -> retry -> effort/model raise -> alt harness │
└──────────────────────────┬────────────────────────────┘
                           │ durable events
                           v
                  ┌───────────────────┐
                  │ attention         │
                  │ classifier        │
                  └─────────┬─────────┘
                            │ meta.relay_events
                            v
                  ┌───────────────────┐
                  │ read-only observer│ owns cursor
                  └─────────┬─────────┘
                            │ sparse relay
                            v
┌──────────────────────────────────────────────────────┐
│ main conductor                                       │
│ VERIFY child -> DECIDE -> LOG -> ACT successor       │
│ user intent -> TARGET -> MESSAGE -> ASSURE delivery  │
└──────────────────────────────────────────────────────┘
                            │ guarded session command
                            v
                  ┌───────────────────┐
                  │ durable mailbox   │
                  │ + capability gate │
                  └─────────┬─────────┘
                            │ runtime-specific control
                            v
                  ┌───────────────────┐
                  │ owned AC session  │
                  │ safe boundary     │
                  └───────────────────┘
```

## Components

| Component | Responsibility | Primary location |
|---|---|---|
| Efficiency preference | Resolve, persist, and forward actuation/assurance modes | execute/auto MCP handlers, execution contract, Auto state |
| Briefing producers | Emit resolved run configuration, staged plan, phase, and discovery summaries | runner, dependency planner, execution event emitter |
| Semantic AC identity | Assign and preserve stable criterion identity | `core/seed.py`, Seed generation/evolution paths |
| Recovery sensor | Prove all engine recovery is closed | `orchestrator/parallel_executor.py` |
| Routing/verdict identity | Supply escalation evidence and stable AC lineage | `orchestrator/execution_event_emitter.py` |
| Seed-QA sensor | Emit a typed exhausted-gate event | `auto/pipeline.py` |
| Attention classifier | Join event history and create bounded relay envelopes | `mcp/tools/job_handlers.py` |
| Observer contract | Request linked attention-aware waits | `mcp/tools/job_observer.py` |
| Decision recorder | Persist selected and terminal decision events | new MCP handler + event module |
| Successor directive | Persist non-relaxing corrective context | evolution/Ralph handlers and loop |
| Host playbook | VERIFY, DECIDE, LOG, ACT | root/package skills and Codex artifact |
| Session-attempt projection | Resolve live logical scope/attempt to its owned runtime handle without accepting native IDs as caller authority | `orchestrator/synapse.py` + runtime lifecycle events |
| Synapse command | Validate SessionSignal authority, target generation, capability, fallback, and idempotency | `mcp/tools/synapse_handler.py` + MCP composition root |
| Synapse mailbox | Persist requested/accepted/queued/delivering/applied/rejected/uncertain/completed delivery state | `core/session_signal*`, `events/session_signal.py`, `orchestrator/synapse.py` |
| Runtime signal adapter | Apply after-turn delivery, checkpoint redirect, or owned replacement only where the harness can enforce it | optional worker transport/session-control protocol |

## Data flow

1. The engine completes its retry and alternate-harness policy.
2. A producer emits authoritative sensor events.
3. `ouroboros_job_wait` reads linked streams with one global cursor.
4. The classifier performs any required full-history join and emits bounded
   `relay_events`.
5. The observer forwards only classified phase/progress/attention/terminal events.
6. The main conductor spawns one read-only verifier for attention evidence.
7. The conductor selects an ordered action and logs intent.
8. Read-only actions may run immediately. Mutation requires closed ownership.
9. A successful mutation starts a bounded successor and logs its receipt.

Before step 1, `run`/`auto` resolves the user-facing efficiency choice into a
persisted `efficiency_mode` plus `frugality_assurance` contract. Every successor
inherits that contract unless the user starts a new successor with an explicit
override.

The proactive UX uses the same flow without invoking conductor judgment:
configuration, plan, phase/discovery, level, model, and harness events are reduced
into sparse `phase_changed`/`progress_advanced` relay envelopes. Only
`attention_required` enters VERIFY/DECIDE.

Updated user intent follows a reverse, cursor-independent command path:

1. The main conductor calls `ouroboros_session_signal_targets` for the observed
   execution and maps the human's wording to the most relevant live AC content.
2. If candidates remain genuinely tied, the conductor asks one short question
   in the active conversation language; it never asks the human for internal IDs.
3. The conductor copies the selected exact scope/attempt guards into
   `ouroboros_session_signal`, which persists `requested` before validation.
4. The server validates execution/attempt guards, source authority, content
   bounds, and runtime control capabilities.
5. The mailbox records `accepted` and `queued`, including requested and effective
   mode when an explicit fallback is used.
6. The owning runtime claims the signal with `delivering`, applies it at its
   declared boundary, and emits `applied` plus `completed`, or emits `rejected`.
   A crash after claim but before provider acknowledgement emits terminal
   `delivery_uncertain` and suppresses automatic retry.
7. For `inform`, the same native session runs one reply turn with `tools=[]` and
   stores only a bounded, secret-filtered reply in `completed`. A runtime that
   cannot enforce the empty catalog surfaces that parameter degradation instead
   of claiming a native no-tools guarantee.
8. The existing observer relays the status; the main session explains the proven
   state naturally in the active conversation language.

The existing `ControlBus` may fan out a message inside one runtime process, but
it is not the durable or cross-runtime transport. EventStore-backed mailbox
state remains authoritative across process boundaries and restarts.

## Key decisions

| Decision | Rationale |
|---|---|
| Dedicated recovery-exhausted event | Retry counters cannot represent early stop and alternate-harness closure safely. |
| New attention wake mode | `raw` is noisy while `ac_change` misses semantic events. |
| Discriminated action menu | VERIFY and user escalation are not MCP tool calls. |
| Successor-only recovery/artifact mutation | Prevents token duplication and races with deterministic recovery. |
| Stable semantic AC identity | Runtime session IDs and AC list positions cannot form a streak across evolution. |
| Separate intent/outcome audit | Failed and declined conductor choices remain visible. |
| Non-relaxing corrective directive | Autonomy must not silently weaken the user's contract. |
| Separate actuation from assurance | Lower-cost routing can save tokens, while strict proof may spend extra tokens. |
| Reuse #1601/#1602 telemetry | The main session, TUI, and Web must agree on progressive routing, current model, and token totals. |
| Plan event plus live level events | Users need the whole schedule once and the current stage as it changes. |
| Bounded discovery summaries | Helpful visibility without leaking noisy raw tool streams or chain-of-thought. |
| Signal delivery separate from redirect | Background advice, after-turn delivery, checkpoint redirect, and replacement have different safety and capability semantics. |
| Logical attempt addressing | Native session IDs are transport metadata; execution + scope + attempt guards prevent stale cross-run delivery. |
| Acknowledged application | Queued delivery is useful but does not prove that the worker changed course. |
| Explicit hard replacement | Aborting owned work may discard tool progress and therefore requires user authority. |
| Shared contract for spec changes | A changed goal or AC cannot be injected into one worker while peers continue on an older Seed version. |

## Compatibility

- Preserve `recommended_host_action="spawn_observer_session"`.
- Add, rather than replace, `meta.relay_events` and plural per-event menus.
- Keep legacy `raw`, `ac_change`, `phase_change`, and `terminal` waits.
- Leave OpenCode plugin mode unchanged.
- Update both root `skills/` and packaged/plugin skill copies in the same slice.
- Render efficiency choices in the conversation language; keep persisted enum
  values language-neutral.
- Add session-control capabilities without changing existing runtime behavior;
  unsupported transports advertise false and use explicit follow-up/rejection.
- Preserve current resume handles. Checkpoint redirect is an optional capability, not a
  reinterpretation of `targeted_resume`.
- `ouroboros_session_signal` is registered because leader-driven resumable
  runtimes now implement tested `after_turn` delivery and application
  acknowledgement. Other modes remain capability-gated.

## Testing strategy

- Pure table-driven classifier tests for every trigger and malformed payload.
- Job-wait wake, cursor paging, timeout, and no-skip tests.
- Producer tests for exactly-once recovery exhaustion and stable AC identity.
- Negative tests proving intermediate retries never authorize mutation.
- Skill artifact tests for observer/verifier separation and mode policy.
- End-to-end tests for logged successor dispatch, idempotency, and budget exhaustion.
- Start/resume tests for efficiency mapping, Auto forwarding, English canonical
  host guidance, and strict-proof opt-in.
- Reducer agreement tests proving main-session routing/token summaries match the
  #1602 TUI/Web projections.
- Plan/level/discovery deduplication and on-demand AC assurance tests.
- Session-message state-machine, idempotency, stale-attempt, priority, expiry,
  uncertain-delivery, payload-bound, and secret-filter tests.
- Runtime contract tests distinguishing resume, after-turn delivery, checkpoint redirect,
  owned abort, and replacement resume.
- End-to-end assurance tests proving `queued` is never rendered as `applied` and
  explicit fallback names the effective mode.

## Integration with #1601 and #1602

- #1601 changes model escalation from one fixed notch to progressive routing.
  Active Conductor must consume the final event shape after that PR lands and
  announce each actual route change rather than calculate an expected tier in the
  host.
- #1602 exposes model/tier/token telemetry through TUI/Web related-event queries
  and shared reducers. Main-session briefing should reuse those event semantics
  and, where practical, shared projection helpers instead of building a separate
  token/model accounting path.
- Neither PR currently provides execution-plan or semantic Discover events; those
  remain explicit Active Conductor producer work.
