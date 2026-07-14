# Active Conductor Implementation Plan

> Generated: 2026-07-12
> Status: Stacked implementation plan. Availability is established per slice;
> the contract foundation lands before the runtime and host layers.

## Delivery strategy

Implement as five reviewable slices. Do not combine host autonomy with the first
sensor/relay change.

## S0 — sensor closure

### Changes

- Add `execution.ac.recovery_exhausted` and emit it exactly once after retry and
  alternate-harness paths close.
- Add `efficiency_mode` (`adaptive`, `quality_first`) and
  `frugality_assurance` (`off`, `observe`, `strict`) to execute and Auto start
  contracts.
- Persist resolved preferences in the execution contract and `AutoPipelineState`;
  forward them through Auto RUN/Ralph handoffs and restore them on resume.
- Emit `execution.run.configuration_resolved` before AC dispatch.
- Emit `execution.plan.created` after dependency analysis with total levels,
  bounded AC summaries, dependency stages, and the first scheduled level.
- Emit bounded `execution.ac.phase_changed` and
  `execution.ac.discovery.updated` events with material-change deduplication.
- Add optional `semantic_ac_key` to `AcceptanceCriterionSpec`, including additive
  serialization, deterministic legacy materialization, and successor propagation.
- Add `semantic_ac_key`, `base_model_tier`, `escalation_retry_threshold`, and
  `model_escalated` to relevant execution events.
- Add `semantic_ac_key` to deliver-verdict events.
- Emit `auto.seed_qa.blocked` for exhausted non-passing QA gates.
- Define bounded event schemas/constants and event documentation.

### Tests

- configured retry exhaustion;
- repeated-failure early stop below the numeric retry cap;
- alternate-harness success/failure;
- model escalation at and below threshold;
- semantic key creation and propagation through retry/decomposition/evolution;
- replacement criteria receive a new key while preserved criteria retain theirs;
- Seed-QA pass, repair, transient error, and exhausted block.
- efficiency default mapping, explicit override, persistence, forwarding, and
  resume immutability;
- strict assurance never arms shadow replay without explicit authorization and
  required safety attestations.
- run configuration and plan events precede level 1 dispatch;
- discovery events are bounded, phase-honest, and suppress raw tool activity;
- #1601 progressive route events and #1602 token/model projections remain the
  authoritative source for main-session summaries.

### Exit gate

No host behavior changes. Producers alone prove all attention preconditions.

## S1 — attention classification and observer relay

### Changes

- Add `attention_or_ac_change` to `ouroboros_job_wait`.
- Add `stream="linked"` to the observer wait template.
- Add pure classifier helpers over raw `BaseEvent` data.
- Add bounded `meta.relay_events` and discriminated action menus.
- Add proactive briefing subtypes for run configuration, discovery, execution
  plan, level, AC routing, harness changes, and AC verification.
- Perform a terminal full-history scan before final observer completion.
- Preserve cursor page boundaries and legacy wait modes.

### Tests

- one table row per trigger;
- no action menu on phase/progress;
- unrelated linked event does not wake;
- attention event wakes immediately;
- malformed evidence fails closed;
- terminal scan finds late frugality evidence;
- proactive event cadence and material deduplication;
- plan briefing contains total levels and first scheduled ACs;
- route/harness messages emit on initial/current change only;
- bounded pages do not skip or duplicate events;
- legacy observer contract remains compatible.

### Exit gate

Observer can reliably relay attention, but menus advertise only read-only actions
and user escalation.

## S2 — host conductor playbook

### Changes

- Update `skills/run`, `skills/auto`, and `skills/ralph`.
- Update packaged/plugin copies used by supported hosts.
- Update `src/ouroboros/codex/ouroboros.md` and runtime guides.
- Specify one exclusive observer and at most one short-lived verifier.
- Implement VERIFY → DECIDE behavior and current-turn schema reload.
- Add English canonical pre-start efficiency guidance when neither an explicit
  argument nor a remembered preference exists; describe user outcomes rather
  than MCP internals and let the host phrase them naturally in the active
  conversation language.
- Add English canonical host guidance for start accepted, Discover targets, plan
  ready, level start/completion, current model/harness, escalation, AC assurance,
  and terminal summary.
- Tell the user at start that they can ask about any AC or continue other work.
- Keep ACT non-mutating until S3 tools are present.

### Tests

- skill artifact contract across every packaged copy;
- no action without a menu;
- no ACT without verifier support;
- no duplicate polling owner;
- `run` user escalation vs Auto/Ralph deterministic policy;
- OpenCode plugin behavior unchanged;
- language-neutral persisted enums and artifact tests for the English canonical
  host contract;
- on-demand AC assurance view never advances the observer cursor;
- main-session model/tier/token output agrees with #1602 TUI/Web reducers;
- #1601 progressive escalation and existing alt-harness events produce friendly
  one-time change notices.

### Exit gate

The main session owns judgment and can verify/surface actionable attention without
changing execution state.

## S3 — audited successor recovery

### Changes

- Add `ouroboros_record_conductor_decision` with idempotent selected/outcome events.
- Add optional persisted `conductor_directive` support to evolution and Ralph.
- Wire run-mode successor execution with explicit approval for spec changes.
- Wire one bounded Auto/Ralph successor for non-relaxing directives.
- Add conductor successor budgets and user escalation on exhaustion.
- Expand action menus only when the corresponding tools are registered.

### Tests

- selected/completed/failed/declined decision audit;
- idempotent repeated decision submission;
- rejected-reason directive propagation;
- preserved goal/AC/non-goal invariants;
- closed-ownership enforcement;
- successor tier override creates a new execution;
- autonomous budget exhaustion;
- action failure is logged and not silently retried.

### Exit gate

All revised RFC acceptance criteria pass, including bounded Auto/Ralph successor
recovery and run-mode approval.

## S4 — Ouroboros Synapse

The clean-room contract, lifecycle events, replay projection, exact-attempt live
hub, durable queue admission, target-discovery and delivery MCP handlers,
observer relay, same-session `inform`, leader-driven `after_turn`, priority,
expiry, and restart recovery are implemented. Checkpoint `redirect` and hard
`replace` remain capability-disabled because no runtime has supplied the needed
live-boundary proof.

### Changes

- Add optional runtime signal capabilities distinct from `targeted_resume`:
  inform delivery, background reply, after-turn delivery, checkpoint redirect,
  owned-turn abort, and
  resumable replacement.
- Add a live session-attempt projection keyed by `execution_id`,
  `session_scope_id`, and `session_attempt_id`; keep native session IDs internal
  to the resolved runtime handle.
- Add bounded event factories and a durable mailbox for:
  `control.session.signal.requested`, `accepted`, `queued`, `delivering`,
  `applied`, `rejected`, `delivery_uncertain`, and `completed`.
- Add `ouroboros_session_signal` with `inform`, `after_turn`, `redirect`, and
  `replace`, plus an explicit `fallback_mode`, expiry, reason, source,
  generation guards, and idempotency key.
- Implement same-session `inform` replies with an explicit `tools=[]` request and
  `after_turn` on transports that can safely resume after the current turn.
  Surface parameter degradation where an empty catalog is not natively
  enforceable. Do not advertise checkpoint redirect for that behavior.
- Extend supported live transports with checkpoint redirect only where an
  application acknowledgement can be produced.
- Wire `replace` only where Ouroboros owns cancellation and can prove
  terminalization before resume/restart; require explicit user approval.
- Route changed goals, ACs, constraints, and non-goals through an approval-bound
  shared successor/replacement contract rather than redirecting one live worker.
- Link delivery lifecycle events to the owning job so the existing observer
  relays requested/effective mode and queued/delivering/applied/rejected state.
- Update `run`/`auto` host playbooks to map user refinements to affected ACs,
  explain the proposed target/mode, preserve unaffected work, and render delivery
  assurance in the conversation language.

### Tests

- exact target, stale attempt, wrong execution, terminal session, and expiry;
- at-most-once request and replay-safe mailbox consumption;
- restart replay of still-queued signals and terminal `delivery_uncertain` for a
  signal that was claimed before process loss;
- provider-acknowledgement crash windows terminate as `delivery_uncertain`
  without automatic resend;
- user > conductor > worker priority with no lower-priority supersession;
- bounded text, secret filtering, digest audit, and no raw transcript storage;
- queued versus applied acknowledgement and bounded background reply;
- explicit `redirect -> after_turn` fallback with effective-mode disclosure;
- unsupported redirect with no fallback fails closed;
- replace approval validation plus capability-disabled rejection until an owned
  cancellation, terminalization, and replacement-resume proof exists;
- runtime capability matrices for Codex, Claude SDK/workers, OpenCode, Goose,
  Pi, and unsupported harnesses;
- no duplicate observer cursor and English canonical delivery-state guidance;
- unaffected parallel AC sessions continue while one exact target receives an
  `inform` or `after_turn` signal.

### Exit gate

The main session can deliver updated user intent to an exact AC session with
auditable, capability-truthful semantics. It never reports a safe interrupt,
application, or hard replacement that the runtime did not acknowledge.

### Final validation

- Complete automated suite: `12,263 passed, 5 skipped`.
- Ruff lint/format: clean; Mypy: clean across 444 source files.
- Live same-native-session `inform`: Codex CLI, Claude Agent SDK, persisted
  Claude worker, OpenCode, Goose, and Pi.
- Explicit `redirect -> after_turn` fallback: Pi.
- Truthful unsupported path: Hermes requested → rejected with every
  SessionSignal capability disabled.
- Deterministic manual persistence harness: consumption expiry, source-priority
  supersession, queued restart replay, and delivering restart uncertainty.

## Review checkpoints

After each slice:

1. run targeted unit tests;
2. run formatting and type checks for changed modules;
3. review event backward compatibility and payload bounds;
4. verify no unrelated skill/package copy drift;
5. update the RFC status only for the completed slice.

## Primary risks

| Risk | Mitigation |
|---|---|
| Duplicate recovery-exhausted events | Deterministic event ID/idempotency key per execution + semantic AC key. |
| Cursor skips from linked streams | Reuse the existing bounded global-rowid page boundary. |
| Conductor races active recovery | Server validates authoritative ownership closure before mutation. |
| Dynamic prompt payload growth | Strict reason/count/length bounds and argument digests in audit events. |
| Skill behavior differs by host | Test root and packaged copies from one canonical contract. |
| Autonomous spec weakening | Persisted directive is additive and invariant-checked before successor start. |
| “Efficiency proof” costs more than it saves | Keep strict assurance/shadow replay as a separate explicit opt-in. |
| Auto loses the user's start preference | Persist in Auto state and assert forwarding/resume contracts. |
| Main/TUI/Web disagree about current model | Reuse #1602 event semantics and shared projection helpers. |
| Discover becomes a noisy activity feed | Emit bounded semantic summaries only on material change. |
| Plan briefing arrives after work starts | Persist `execution.plan.created` before level 1 dispatch and test event order. |
| Resume is mistaken for checkpoint redirect | Model the capabilities separately and require an `applied` acknowledgement. |
| Intent reaches a newer retry or successor | Require execution + scope + attempt guards and reject stale targets. |
| Lower-priority automation overrides the user | Persist source priority and serialize unapplied messages per target. |
| Hard abort loses completed work | Require explicit approval, terminalization proof, and a replacement receipt. |
| Main session overstates delivery | Host UX is driven by durable queued/delivering/applied/rejected events, not MCP call success prose. |
| One AC receives a new spec while peers use the old Seed | Reject live redirect and create one approval-bound shared successor contract. |

## Merge prerequisites

- #1601 and #1602 are present on main. Keep progressive escalation as the only
  routing semantics supported.
- Reuse #1602 related-event delivery and projection patterns; do not
  duplicate token aggregation or latest-route selection.
- Keep the RFC/plan changes isolated from the open PR code until both merge or
  explicitly coordinate a stacked branch.

## Proposed implementation order

1. S0 sensor closure
2. S1 attention relay
3. S2 host playbook
4. S3 audited successor recovery
5. S4 Ouroboros Synapse

The remaining gate is full regression plus repeated live runtime proof against
the completed implementation.
