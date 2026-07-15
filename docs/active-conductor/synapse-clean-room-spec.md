# Ouroboros Synapse Clean-Room Specification

> Generated: 2026-07-12
> Status: Normative input for a stacked delivery. The contract layer implements
> only the core/event items identified below; runtime and host availability
> requires the later stack layers.
> Source boundary: this document and Ouroboros repository contracts only

## Purpose

Ouroboros Synapse delivers one bounded intent signal from the main conductor to
one exact AC runtime attempt. It records whether the signal was requested,
accepted, queued, claimed for delivery, applied, rejected, completed, or left
uncertain across a provider acknowledgement boundary.

Synapse complements the observer path:

```text
AC runtime ── durable events ──> observer ──> main conductor
AC runtime <── SessionSignal ─── Synapse <── main conductor
```

## Clean-room rules

Implementation uses only this behavior specification and existing Ouroboros
abstractions. It must not copy or translate another framework's source code,
protocol layout, type names, event names, prompts, registry, or UI transcript
format.

The following are original Ouroboros terms:

- subsystem: `Synapse`
- directed unit: `SessionSignal`
- command: `ouroboros_session_signal`
- delivery modes: `inform`, `after_turn`, `redirect`, `replace`
- runtime capability: `SessionSignalCapabilities`

## Scope

Implemented in the contract layer:

- validated immutable `SessionSignal` contract;
- exact execution/scope/attempt target identity;
- bounded message, reason, and identifier fields;
- expiry and approval-receipt validation;
- requested/effective mode resolution against runtime capabilities;
- durable lifecycle event factories;
- deterministic lifecycle projection;
- idempotency identity and legal state-transition checks;
- capability fields that default to unsupported;

Implemented by the later runtime and host stack layers, and therefore not
claimed as available from the contract layer alone:

- exact-attempt EventStore target resolution and durable queue admission;
- public discovery/delivery MCP schemas, validation, and composition-root registration;
- in-process exact-attempt hub and tested leader-driven plus persisted
  `codex_cli` delivery;
- same-session `inform` replies with an explicit `tools=[]` request, plus
  additive `after_turn` delivery; runtimes surface degradation when they cannot
  enforce an empty tool catalog natively;
- durable `delivering` state before provider handoff;
- user > conductor > worker pending-signal priority;
- expiry checks at admission, replay, and consumption;
- queued replay after restart and terminal uncertainty for a previously claimed
  signal;
- bounded, secret-filtered replies in completed events.

Capability-disabled until runtime proof exists:

- checkpoint hooks inside an active provider turn;
- owned process abort and replacement resume.

## Vocabulary

### Modes

| Mode | Semantics | May be fallback |
|---|---|---|
| `inform` | Deliver contextual information without claiming execution redirection. | no |
| `after_turn` | Apply after the current worker turn finishes and before the next turn begins. | yes |
| `redirect` | Apply at a runtime-declared checkpoint before the next model/tool decision. | no |
| `replace` | Abort owned activity and start a replacement turn or session. | no |

### Sources

Priority is deterministic:

1. `user`
2. `conductor`
3. `worker`

A lower-priority signal cannot supersede an unapplied higher-priority signal for
the same session attempt.

### Lifecycle states

```text
requested
  ├─> rejected
  └─> accepted ─> queued
                  ├─> delivering ─> applied ─> completed
                  │               ├─> rejected
                  │               └─> delivery_uncertain
                  ├─> applied ─> completed
                  ├─> rejected
                  └─> delivery_uncertain
```

Terminal states are `rejected`, `completed`, and `delivery_uncertain`.
`applied` is not terminal because an optional acknowledgement or bounded reply
may still complete the signal.

## SessionSignal contract

Required fields:

- `signal_id`
- `target_session_scope_id`
- `target_session_attempt_id`
- `expected_execution_id`
- `mode`
- `message`
- `source`
- `reason`
- `idempotency_key`

Optional fields:

- `fallback_mode` — only `after_turn`, and only when requested mode is `redirect`;
- `expires_at` — timezone-aware UTC timestamp;
- `user_approval_event_id` — required for `replace`;
- `expected_contract_version` — required when a future signal references a
  versioned shared execution contract.

## Bounds

| Field | Maximum |
|---|---:|
| `signal_id` | 160 UTF-8 bytes |
| scope/attempt/execution/idempotency identifiers | 256 UTF-8 bytes each |
| `message` | 8,192 UTF-8 bytes |
| `reason` | 1,000 UTF-8 bytes |
| approval event ID | 256 UTF-8 bytes |

Values are stripped before validation. Empty strings, control-only text, and
oversized values fail closed. The persisted event payload stores bounded text
and a SHA-256 message digest. It never stores raw transcripts or tool output.

## Identity invariants

- `target_session_attempt_id` identifies one implementation attempt, not merely
  a resumable provider session.
- `expected_execution_id` prevents a signal from crossing into a successor run.
- The server resolves provider-native session IDs; callers cannot supply them as
  routing authority.
- An expired, terminal, replaced, or mismatched target is rejected.
- `idempotency_key` identifies one effective signal within the exact target
  attempt.

## Capability resolution

`SessionSignalCapabilities` contains independent booleans:

- `background_reply`
- `inform_delivery`
- `after_turn_delivery`
- `checkpoint_redirect`
- `owned_turn_abort`
- `replacement_resume`

All fields default to `False`. Existing `targeted_resume=True` does not imply any
Synapse capability. A runtime opts in only after its exact resume transport is
tested.

The currently proven same-session `inform_delivery`, `background_reply`, and
`after_turn_delivery` transports are:

| Runtime | Resume proof | Synapse status |
|---|---|---|
| `codex_cli` | persisted Codex thread ID | enabled |
| `claude` | Claude Agent SDK native session ID | enabled |
| `claude_mcp` | persisted worker session only | enabled only when persistence is configured |
| `opencode` | `opencode run --session <id>` | enabled |
| `goose` | stable generated session name plus `--resume` | enabled |
| `pi` | exact project session ID via `--session` | enabled |
| `hermes_cli` | CLI advertises `--resume`, but the installed live probe did not persist the emitted session into its resumable store | disabled |

Provider credentials and a valid model remain deployment prerequisites. A
configured transport failure becomes an ordinary runtime error; capability
declaration means the adapter preserves and targets the native session when the
provider itself is operational.

The human never supplies scope or attempt IDs. The main session calls
`ouroboros_session_signal_targets` with the observed execution ID, semantically
matches natural-language intent to live AC content, and copies the selected
logical guards into the delivery command. It asks only for genuine semantic
ties and never exposes provider-native session IDs.

Resolution rules:

1. `inform` requires `inform_delivery`; a returned reply requires
   `background_reply` and is always bounded.
2. `after_turn` requires `after_turn_delivery`.
3. `redirect` requires `checkpoint_redirect`.
4. A `redirect` request may resolve to effective mode `after_turn` only when
   `fallback_mode=after_turn` and the runtime supports it.
5. `replace` requires both `owned_turn_abort` and `replacement_resume`, plus a
   non-empty user approval event ID.
6. Unsupported modes are rejected; capability resolution never silently
   upgrades or downgrades.

## Lifecycle event contract

Event namespace:

- `control.session.signal.requested`
- `control.session.signal.accepted`
- `control.session.signal.queued`
- `control.session.signal.delivering`
- `control.session.signal.applied`
- `control.session.signal.rejected`
- `control.session.signal.delivery_uncertain`
- `control.session.signal.completed`

All events:

- aggregate by `("session_signal", signal_id)`;
- carry schema version, exact target identity, source, requested mode,
  idempotency key, message digest, and bounded reason;
- include `effective_mode` after acceptance;
- may include `job_id`, `session_id`, and runtime backend as bounded correlation
  metadata;
- never include provider payloads, raw prompts, transcripts, or tool output.

## Delivery acknowledgement

`queued` proves only that Ouroboros durably owns pending delivery.
`delivering` proves only that the owning runtime claimed the signal before the
provider boundary; it still does not prove application.
`applied` requires a runtime or checkpoint acknowledgement that the signal
entered the target context.

If Ouroboros may have handed the signal to a provider but cannot prove whether
the provider accepted it, the state becomes `delivery_uncertain`. That state is
terminal for automatic delivery; operators may inspect the target, but Synapse
does not resend automatically.

## Specification-changing intent

Live signals are additive implementation guidance or read-only information.
A change to the approved goal, ACs, constraints, or non-goals cannot be delivered
as `inform`, `after_turn`, or `redirect`.

Such a change requires:

1. an explicit user approval receipt;
2. a new shared contract version;
3. one successor or replacement plan covering every affected AC.

## Acceptance criteria

1. Every invalid or oversized contract fails before an event is created.
2. Capability resolution is deterministic and table-tested.
3. `redirect` falls back only to an explicitly requested `after_turn` mode.
4. `replace` requires both runtime capabilities and user approval.
5. Legal lifecycle transitions are deterministic; illegal transitions fail.
6. Event payloads are bounded, digest-bearing, and free of raw provider data.
7. Idempotency identity includes exact execution, scope, attempt, and key.
8. Existing runtimes remain behaviorally unchanged because all Synapse
   capabilities default to unsupported.
9. A queued signal is replayed after restart, while a previously delivering
   signal becomes terminal `delivery_uncertain` and is never blindly resent.
10. `inform` runs without tools in the same native session and persists only a
    bounded, secret-filtered reply.
