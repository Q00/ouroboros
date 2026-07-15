# RFC — Active conductor: judgment on the event-driven main session

> Status: Proposed — Synapse contract foundation implemented (2026-07-12)
> Scope: `ooo run`, `ooo auto`, `ooo ralph` host skills; MCP attention
> classification; conductor decision audit; post-recovery successor dispatch;
> capability-aware Ouroboros Synapse control
> Builds on: [delegated-job-observer](delegated-job-observer.md),
> [frugality-proof-producers](frugality-proof-producers.md)

## Problem

#1599 made background-job observation event-driven: one read-only child owns the
`job_wait` cursor and relays sparse progress, attention, and terminal messages to
the main session. That split correctly moved polling out of the expensive main
context, but it left semantic judgment unowned.

The engine already owns deterministic recovery: configured AC retries,
reasoning-effort escalation, one-notch model escalation, and alternate-harness
redispatch. Once that recovery is exhausted, however, no component verifies the
failure against ground truth, decides whether the specification or approach is
wrong, and starts a bounded corrective successor.

The first RFC draft assumed that the existing event and MCP surfaces were already
sufficient. The implementation audit found six contract gaps:

1. The observer waits with `wait_for="ac_change"` and does not request the linked
   event stream, so deliver verdicts and frugality events cannot wake it.
2. `execution.ac.outcome_finalized` is emitted after intermediate attempts as well
   as final attempts. It does not prove that retry and alternate-harness ownership
   is closed.
3. Model-routing events do not explicitly record whether an escalation occurred,
   and deliver-verdict events have no lineage-stable semantic AC identity.
4. Seed-QA blocking mutates Auto state but emits no dedicated gate-block event.
5. No public MCP surface exists for conductor decision logging, corrective
   directives, targeted in-flight tier pinning, or spec amendment.
6. Ouroboros can observe and resume provider-native worker sessions, but it has
   no durable, capability-aware mailbox for applying updated user intent to a
   specific live session. The existing `ControlBus` is explicitly in-process
   only and cannot by itself redirect a worker owned by another runtime process.

The active conductor must close these gaps without racing the recovery already
owned by the engine, and without claiming that every runtime can interrupt an
in-flight turn.

## Decision

Make the main session an **active conductor** while preserving exclusive observer
cursor ownership:

```text
engine/workers  -> durable sensor events
                      |
job observer    -> classified relay event
                      |
main conductor  -> VERIFY -> DECIDE -> LOG -> ACT
                                             |
                              bounded successor execution/generation
```

The observer owns polling. The main session owns judgment. The engine continues
to own every retry and redispatch inside the active job.

The conductor MUST NOT directly mutate artifacts, change routing, or redispatch
an active AC. A mutating recovery action is valid only when
`engine_ownership.state == "closed"`. Corrective work is a successor execution or
successor evolution generation, never a concurrent second driver of the current
AC.

Updated user intent is a separate control-plane input. It may be queued for a
specific active session and applied only through a runtime-declared safe boundary.
This does not authorize concurrent redispatch or silent specification changes.

## Layer 0 — authoritative sensor contract

Attention classification must consume durable facts, not infer retry state from
counter values or prose.

### Recovery exhaustion

Emit exactly one `execution.ac.recovery_exhausted` event for each root AC that
remains failed after every engine-owned path has closed.

Required data:

```json
{
  "execution_id": "exec_...",
  "session_id": "orch_...",
  "root_ac_index": 2,
  "semantic_ac_key": "ac_...",
  "retry_attempt": 2,
  "configured_retry_attempts": 2,
  "retry_termination_reason": "budget_exhausted",
  "alternate_redispatch_status": "failed",
  "last_failure_class": "verify_command_failed",
  "success": false
}
```

`retry_termination_reason` is one of:

- `budget_exhausted`
- `repeated_failure_early_stop`
- `alternate_harness_exhausted`
- `not_retryable`

`alternate_redispatch_status` is one of:

- `not_eligible`
- `not_attempted`
- `succeeded`
- `failed`

This event is the authoritative proof that the conductor may consider a
successor. `execution.ac.outcome_finalized` remains attempt-level telemetry and
MUST NOT independently trigger a mutating conductor action.

### Model escalation evidence

Extend `execution.ac.model_routed` with:

- `semantic_ac_key`
- `base_model_tier`
- `escalation_retry_threshold`
- `model_escalated: bool`

An escalation-failed trigger requires a matching
`execution.ac.recovery_exhausted` event and the latest routed event with
`model_escalated=true`. Reaching `frontier` alone is not evidence of exhaustion.

### Deliver-verdict lineage

Add an optional `semantic_ac_key` to `AcceptanceCriterionSpec`, and extend
`execution.ac.deliver_verdict` with the resolved key. The key is assigned when an
acceptance criterion is created and is preserved through retries, decomposition,
and evolution successors. A genuinely new or semantically replaced criterion
receives a new key. Legacy criteria without a key receive a deterministic key at
Seed materialization and persist it in the next structured Seed representation.

A rejected streak is counted by `(judgment_scope_id, semantic_ac_key)`, where
`judgment_scope_id` is the evolution lineage when one exists and otherwise the
root background job. Runtime session ID and list position are not valid lineage
identities.

The event keeps the full `rejected_reasons`; classification and verification must
not depend on the truncated human-readable event detail.

### Seed-QA gate

Emit `auto.seed_qa.blocked` whenever the Seed-QA repair budget closes without a
passing verdict.

Required data:

- `auto_session_id`
- `seed_id`
- `attempts`
- `verdict`
- `score`
- `differences`
- `suggestions`
- `reason`

Transient evaluator errors and timeouts remain ordinary Auto blockers and use a
different trigger code from a genuine non-passing Seed-QA verdict.

### Frugality and stagnation

Reuse the existing events:

- `execution.frugality_proof.evaluated`
- `lineage.stagnated`

The terminal job response performs a final full-history classification scan so a
frugality event emitted during runner teardown is not lost when the observer
fetches the terminal result.

## Layer 1 — job-wait attention relay

### Wake contract

The observer wait template uses:

```json
{
  "tool": "ouroboros_job_wait",
  "arguments": {
    "job_id": "job_...",
    "cursor": 0,
    "timeout_seconds": 180,
    "view": "summary",
    "stream": "linked",
    "wait_for": "attention_or_ac_change"
  }
}
```

`attention_or_ac_change` wakes on:

- meaningful AC/Sub-AC/phase progress;
- a newly classified attention event;
- terminal job state;
- timeout.

It does not wake on unrelated raw linked events or unchanged heartbeats.

### Relay envelope

`ouroboros_job_wait` adds `meta.relay_events`, ordered by durable event position.
Each item uses this discriminated envelope:

```json
{
  "id": "attention_...",
  "kind": "attention_required",
  "trigger": "ac_recovery_exhausted",
  "scope": {
    "job_id": "job_...",
    "execution_id": "exec_...",
    "session_id": "orch_...",
    "lineage_id": null,
    "semantic_ac_key": "ac_..."
  },
  "engine_ownership": {
    "state": "closed",
    "evidence_event_ids": ["event_..."]
  },
  "evidence": {
    "reason": "verify_command_failed",
    "rejected_reasons": []
  },
  "recommended_host_actions": [
    {
      "kind": "host_verify",
      "action": "spawn_read_only_verifier",
      "arguments": {
        "evidence_event_ids": ["event_..."],
        "workspace_policy": "read_only"
      },
      "effect": "read_only",
      "rationale": "Confirm the failure against durable evidence and disk state."
    }
  ]
}
```

Valid `kind` values remain:

- `phase_changed`
- `progress_advanced`
- `attention_required`
- `terminal`

Only `attention_required` carries `engine_ownership`, `evidence`, and
`recommended_host_actions`. Progress and phase events MUST NOT carry action
menus.

### Attention triggers

| Trigger code | Durable evidence | Mutation eligibility |
|---|---|---|
| `ac_recovery_exhausted` | `execution.ac.recovery_exhausted` | ownership closed |
| `model_escalation_failed` | recovery exhausted + latest `model_escalated=true` | ownership closed |
| `deliver_verdict_rejected_streak` | at least two rejected verdicts for one `(judgment_scope_id, semantic_ac_key)` | verify immediately; mutate only after ownership closes |
| `frugality_grounding_regression` | proof status `fail_grounding_regression` | successor only |
| `frugality_no_savings` | proof status `fail_no_frugality` | successor only |
| `seed_qa_blocked` | `auto.seed_qa.blocked` | successor/resume only |
| `lineage_stagnated` | `lineage.stagnated` | successor generation only |

If a rejected streak is observed while engine ownership is still active, the
menu contains only read-only verification and defer/escalation actions. No
redispatch template is emitted until ownership is closed.

### Action menu schema

VERIFY is a host-native operation, while ACT may be an MCP tool call. The menu
therefore uses a discriminator instead of pretending every action is an
`ouroboros_*` tool:

```json
{
  "kind": "host_verify",
  "action": "spawn_read_only_verifier",
  "arguments": {
    "evidence_event_ids": ["event_..."],
    "workspace_policy": "read_only"
  },
  "effect": "read_only",
  "rationale": "Confirm the reported failure against durable evidence and disk state."
}
```

```json
{
  "kind": "mcp_tool",
  "tool": "ouroboros_lateral_think",
  "arguments": {
    "problem_context": "...",
    "current_approach": "..."
  },
  "required_host_inputs": [],
  "effect": "read_only",
  "rationale": "Generate a bounded alternative before changing the specification."
}
```

```json
{
  "kind": "user_escalation",
  "action": "request_spec_change_approval",
  "arguments": {
    "decision_scope": "acceptance_criteria"
  },
  "effect": "spec_changing",
  "rationale": "The proposed successor changes the user-approved contract."
}
```

Menus are ordered by minimum intervention:

1. host verification;
2. read-only MCP analysis;
3. non-spec-changing successor action;
4. spec-changing successor action or user escalation.

An MCP menu entry MUST name a registered tool and contain all server-known
arguments. Values that require conductor judgment are declared in
`required_host_inputs`; the host fills only those named fields after VERIFY.
This replaces the unimplementable requirement that every dynamic corrective
argument be executable verbatim before verification has happened.

The initial observer handoff retains the singular
`recommended_host_action="spawn_observer_session"` for backward compatibility.
Per-event menus use the plural `recommended_host_actions` field.

## Layer 2 — host conductor playbook

`run`, `auto`, and `ralph` execute the following playbook only for a relayed
`attention_required` event with a non-empty action menu.

1. **VERIFY** — spawn exactly one additional, short-lived, cheap, read-only host
   subagent. It checks the named durable evidence and actual files. It does not
   own the observer cursor and does not edit the workspace.
2. **DECIDE** — choose the earliest menu entry supported by verification. Reject
   an action whose preconditions or required inputs cannot be established.
3. **LOG INTENT** — record the selected action before mutation.
4. **ACT** — reload the selected MCP tool schema in the current turn and call it.
   Mutating actions require `engine_ownership.state="closed"`.
5. **LOG OUTCOME** — record completed, failed, or declined status with the action
   receipt or user decision.

Non-attention events consume no conductor reasoning. If the host cannot spawn a
read-only verifier, it surfaces the attention event and does not ACT.

If the decision-log/control tools are not registered yet, the host completes
VERIFY and DECIDE, surfaces the verified recommendation, and stops. It does not
simulate LOG or ACT from prose.

The observer remains the exclusive polling owner throughout this playbook.

## Layer 3 — decision log and successor recovery

### Decision audit surface

Add `ouroboros_record_conductor_decision`, which appends versioned events keyed by
an idempotent `decision_id`:

- `conductor.decision.selected`
- `conductor.decision.completed`
- `conductor.decision.failed`
- `conductor.decision.declined`

The record includes attention event ID, evidence event IDs, verification summary,
selected action/effect, arguments digest, actor mode (`run`, `auto`, `ralph`), and
result receipt where applicable. It never stores secrets or unbounded raw model
output.

### Corrective directive

Extend the evolution/Ralph start surfaces with an optional
`conductor_directive` object:

```json
{
  "source_attention_event_id": "attention_...",
  "instruction": "Support the rejected claims with repository evidence or remove them.",
  "rejected_reasons": ["..."],
  "preserve_goal": true,
  "preserve_acceptance_criteria": true
}
```

The directive is persisted before successor dispatch and becomes an additive,
non-relaxing constraint. It cannot silently weaken the goal, delete an acceptance
criterion, or rewrite user-owned non-goals.

### Mode policy

- `ooo run` is interactive. It may automatically perform read-only analysis and
  non-spec-changing successor parameter changes. Any amended Seed or acceptance
  contract requires explicit user approval.
- `ooo auto` may autonomously start one bounded successor when the verified
  directive is non-relaxing and the menu marks the action deterministic.
- `ooo ralph` may autonomously start a successor generation with a non-relaxing
  directive after its current Ralph job is terminal. It does not inject into a
  generation already running.

A model-tier change is a **successor-run tier override**, not an in-flight pin.
The action menu must use that wording and must identify the new execution it will
start.

Autonomous conductor actions have a separate bounded budget. Default: one
mutating successor per attention event and no more than two conductor-created
successors per root job. Exhaustion becomes user escalation.

## Start-time efficiency and frugality preference

`ooo run` and `ooo auto` expose a friendly start-time efficiency choice. The
prompt is rendered in the user's conversation language and describes the outcome,
not internal MCP or environment-variable names.

If the user has not supplied an explicit argument or saved preference, the host
asks once:

> Use efficiency mode for this run? It starts suitable ACs on lower-cost models
> and automatically raises capability when recovery requires it.

The public execution preference is:

- `efficiency_mode="adaptive"` — efficiency ON; use tier routing, child lowering,
  and retry escalation;
- `efficiency_mode="quality_first"` — efficiency OFF; do not lower decomposed
  children for cost and do not generate frugality attention.

Efficiency actuation and proof strength are separate internally because proving
savings can itself spend additional tokens:

- `frugality_assurance="off"` — no frugality verdict or conductor attention;
- `frugality_assurance="observe"` — lightweight routing/token/grounding telemetry
  and best-effort proof; this is the default when efficiency is adaptive;
- `frugality_assurance="strict"` — explicitly authorized baseline comparison.
  It may arm shadow replay only when isolation and decomposition attestations are
  satisfied. It MUST NOT be enabled implicitly because it can increase cost.

Default mapping:

| Efficiency choice | Frugality assurance | Shadow replay |
|---|---|---|
| `adaptive` | `observe` | off |
| `quality_first` | `off` | off |

An explicit advanced `frugality_assurance` value overrides the mapping. An
explicit `model_tier` chooses the starting tier but does not silently enable
strict proof.

The resolved preference is persisted in the execution contract. Auto also stores
it in `AutoPipelineState` and forwards it through RUN and Ralph successors.
Resume restores the original preference; changing it starts a new successor
contract rather than mutating an active or historical run.

When assurance is `off`, frugality events may remain as internal low-cost
telemetry where already unavoidable, but the classifier suppresses frugality
attention and the user-facing result makes no savings claim. In `observe` mode,
`insufficient_data` is reported only in the final assurance summary, not as an
attention blocker.

## Proactive run briefing UX

The active conductor is responsible for the user's understanding from the moment
`run` or `auto` starts, not only when something fails. MCP calls, cursors, raw
events, and internal handler names remain implementation details.

All host guidance is canonical English. The host phrases those facts naturally
in the user's current conversation language. Persisted event codes and enum
values remain language-neutral.

### Progressive disclosure

The main session proactively communicates these milestones:

1. **Start accepted** — goal/Seed summary, efficiency choice, planned primary
   harness/runtime, model-routing policy, and what happens next.
2. **Discovering** — which bounded repository areas, artifacts, or contracts are
   currently being inspected and why.
3. **Execution plan ready** — total AC count, total serial/parallel levels,
   dependency shape, and the ACs scheduled first.
4. **AC dispatch** — the current model, tier, enforcement mode, and harness for
   each newly active AC.
5. **Meaningful change** — model escalation, harness redispatch, level advance,
   AC verification, or attention-required judgment.
6. **Terminal assurance** — per-AC outcome, verification evidence, model/harness
   history, token summary when enabled, and remaining risk.

The start message explicitly tells the user that the main conversation remains
available: they may ask about any AC, request more detail, or continue unrelated
work while the observer watches execution.

### Briefing sensor contract

Add `execution.run.configuration_resolved` after runtime and economic preferences
are resolved but before AC dispatch.

Required data:

- `execution_id`, `session_id`
- `efficiency_mode`, `frugality_assurance`
- `primary_runtime_backend`
- `primary_harness_label`
- `model_routing_enabled`
- `requested_model_tier`
- `starting_model_tier` and `starting_model` when already resolvable
- `progressive_escalation_enabled`
- `alternate_harness_enabled`

Add `execution.plan.created` after dependency analysis and before level 1 starts.

Required data:

```json
{
  "execution_id": "exec_...",
  "total_acs": 5,
  "total_levels": 3,
  "parallelizable": true,
  "levels": [
    {
      "level": 1,
      "ac_indices": [0, 2],
      "ac_summaries": ["...", "..."],
      "depends_on_levels": []
    }
  ],
  "first_level": 1,
  "first_ac_indices": [0, 2]
}
```

Reuse `execution.decomposition.level_started` and
`execution.decomposition.level_completed` for live level transitions. The plan
event describes the whole schedule once; level events describe what is happening
now.

### Discover visibility

The current workflow progress projection defaults to broad phase labels and does
not reliably describe what a worker is inspecting. Add bounded semantic events:

- `execution.ac.phase_changed`
- `execution.ac.discovery.updated`

`execution.ac.phase_changed` uses the stable phase vocabulary:

- `discover`
- `plan`
- `implement`
- `verify`
- `deliver`

`execution.ac.discovery.updated` contains:

- `semantic_ac_key`, `ac_index`
- `targets`: at most five bounded paths, symbols, tests, or document labels
- `purpose`: one short sentence
- `source`: `structured_worker` or `deterministic_tool_classifier`

The event is emitted only when the target set or purpose materially changes. It
must not relay every file read, search query, command, thinking fragment, or raw
tool output.

If the runtime cannot provide structured phases, a deterministic classifier may
derive them from tool categories: read-only repository inspection → `discover`,
write/edit → `implement`, test/check commands → `verify`, and final evidence
assembly → `deliver`. Ambiguous activity remains `unknown` rather than being
presented as a confident claim.

### Current model and harness

After #1601, model escalation may climb progressively on later retries. The main
session therefore says **currently running with**, never **this run uses one fixed
model**.

After #1602, reuse the same authoritative events used by TUI/Web:

- `execution.ac.model_routed` for current model/tier/mode;
- `execution.ac.token_attribution.reported` for bounded spend summaries;
- `execution.frugality_proof.evaluated` for the terminal assurance verdict.

Reuse `execution.ac.alt_harness_redispatched` to announce a harness change with
its from/to backend and reason. The main session posts the initial route once and
then only meaningful changes: a stronger tier/model, a different harness, or a
new retry attempt. It does not repeat unchanged routing events.

### Relay subtypes and message policy

Proactive briefings use existing relay `kind` values with structured `subtype`:

- `progress_advanced / run_configuration`
- `phase_changed / ac_phase`
- `progress_advanced / discovery_summary`
- `progress_advanced / execution_plan`
- `progress_advanced / level_started`
- `progress_advanced / ac_routing`
- `progress_advanced / harness_changed`
- `progress_advanced / ac_verified`

Default cadence:

- start configuration: once;
- plan summary: once;
- discovery: only on material target change;
- level start/completion: once per level;
- model/harness: initial dispatch and changes only;
- AC completion: once with compact evidence;
- raw logs and unchanged heartbeats: never.

The user can request an on-demand AC assurance view at any time. That view shows
the selected AC's purpose, dependencies, current phase, current model/harness,
retry history, verified evidence, and blocker without taking observer cursor
ownership.

## Ouroboros Synapse — inter-session intent control

### Clean-room boundary

Synapse is an Ouroboros-native control plane designed from this RFC's behavioral
requirements. Implementations MUST NOT copy another agent framework's source,
wire protocol, type names, event names, prompts, or registry design. Comparative
research is treated only as evidence that asynchronous messaging and execution
redirection are distinct problems.

The subsystem name is **Ouroboros Synapse**. A single directed unit is a
`SessionSignal`. Synapse carries intent from the main conductor to one exact AC
session attempt and reports the proven delivery state back through durable
events. It is not a chat network.

### Control modes

Inter-session signaling and execution redirection use one audited surface with
distinct modes:

| Mode | Meaning | Default authority |
|---|---|---|
| `inform` | Deliver bounded context or advice; an optional no-tools/background reply may be returned when supported. | user or conductor |
| `after_turn` | Queue the signal for the next worker turn after the current turn completes. | user or conductor |
| `redirect` | Apply updated intent before the next model/tool decision at a runtime-declared checkpoint. | user; conductor only for non-relaxing directives |
| `replace` | Abort owned runtime activity, persist the interruption, then resume or restart with replacement intent. | explicit user approval only |

`redirect` is the preferred interrupt behavior because it preserves completed tool
work and avoids pretending that a provider token stream can be spliced
arbitrarily. If the target runtime cannot redirect, the signal may fall back to
`after_turn` only when that fallback is explicit in the command. Otherwise it is
rejected. `replace` is never an implicit fallback.

### Addressing and capability contract

The logical target is an Ouroboros runtime session, not merely a provider-native
session ID. Every request carries all of:

- `target_session_scope_id` — stable AC/session identity;
- `target_session_attempt_id` — unique implementation attempt;
- `expected_execution_id` — generation guard against delivery to a later run;
- `idempotency_key` — at-most-once command identity.

The server resolves the current runtime handle internally. Native session IDs
are audit metadata, not user-supplied routing authority. A stale, terminal, or
mismatched target fails closed.

The human never supplies these logical IDs either. The main session obtains the
current `execution_id` from the run/auto start or observer contract, calls
`ouroboros_session_signal_targets`, and semantically matches the human's wording
against each live target's AC content, display path, and current activity. One
candidate may be selected directly. With multiple candidates, the conductor
selects only when one is materially more relevant and asks a short question in
the active conversation language only for a genuine tie. Exact scope and attempt IDs are copied from the
selected discovery result, never invented or requested from the human.

Each runtime advertises an explicit control capability matrix:

- background reply support;
- inform delivery support;
- after-turn delivery support;
- checkpoint redirect support;
- owned-turn abort support;
- resumable replacement support.

Capabilities describe what the active transport can enforce now. Session
resumability alone does not imply checkpoint redirect, and the in-process `ControlBus`
does not imply cross-runtime delivery.

### MCP command and durable lifecycle

Add the read-only discovery command `ouroboros_session_signal_targets`:

```json
{
  "execution_id": "exec_abc"
}
```

It returns only currently registered attempts for that execution, including AC
content and logical scope/attempt guards. It never exposes provider-native
session IDs and does not choose a target on behalf of the main model.

Add the delivery command `ouroboros_session_signal`:

```json
{
  "target_session_scope_id": "exec_abc_ac_2",
  "target_session_attempt_id": "exec_abc_ac_2_attempt_1",
  "expected_execution_id": "exec_abc",
  "mode": "redirect",
  "fallback_mode": "after_turn",
  "message": "Apply the refined requirement while preserving the approved ACs.",
  "source": "user",
  "reason": "The user refined the desired interaction.",
  "user_approval_event_id": null,
  "idempotency_key": "msg_...",
  "expires_at": "2026-07-12T15:00:00Z"
}
```

The command appends a durable state machine:

- `control.session.signal.requested`
- `control.session.signal.accepted`
- `control.session.signal.queued`
- `control.session.signal.delivering`
- `control.session.signal.applied`
- `control.session.signal.rejected`
- `control.session.signal.delivery_uncertain`
- `control.session.signal.completed`

`accepted` records the validated target, requested/effective mode, capabilities,
and any explicit fallback. `queued` proves durable ownership of pending delivery.
`delivering` records that the owning runtime claimed the signal immediately
before provider handoff; it does not prove application.
`applied` requires a runtime or checkpoint acknowledgement that the instruction
entered the target context. `completed` may include a bounded acknowledgement or
side-channel reply, but never an unbounded transcript. A tool success response
that only means "queued" MUST NOT be presented as "already applied".
If a process fails after handing the message to a provider but before recording
acknowledgement, `delivery_uncertain` is terminal for automatic delivery. The
system does not blindly retry a possibly applied instruction.

On restart, still-queued signals are reconstructed into the exact live target's
queue. A signal whose last durable state is `delivering` is never replayed; it is
closed as `delivery_uncertain`. Expiry is checked again when the runtime consumes
the signal, not only when it was admitted.

The event stream is linked to the owning job and execution so the exclusive job
observer can relay delivery status without giving the main session another
cursor. The direct MCP call does not read or advance the observer cursor.

### Ordering, authority, and safety

- User-authored intent outranks conductor and worker messages. A lower-priority
  message cannot supersede an unapplied user message for the same target. Source
  attribution is resolved from the command path and approval receipt; it is not
  inferred from prose inside `message`.
- Messages are bounded, secret-filtered, and contain no raw transcript or tool
  output. The persisted audit stores a digest plus bounded user-visible text.
- A signal that changes the approved goal, ACs, constraints, or non-goals is not
  accepted as `inform`, `after_turn`, or `redirect`. It requires an explicit user
  approval receipt and an approval-bound successor or `replace`
  contract so every affected AC shares one new specification version.
- Delivery is at most once per idempotency key. Runtime acknowledgement and
  mailbox consumption are replay-safe.
- Expired messages and messages targeting a replaced attempt are rejected, not
  silently redirected.
- If safe application cannot be proven, the conductor reports `queued`,
  `deferred`, or `rejected`; it never claims the AC changed course.

### User assurance

Canonical host guidance is written in English and the host phrases it naturally
in the user's current conversation language. The facts must preserve actual
delivery semantics. Example canonical messages:

- "The request is durably queued for AC 3 and has not been applied yet."
- "This runtime cannot redirect at a live checkpoint, so the explicit
  after-turn fallback will apply after the current turn."
- "AC 3 was replaced by another attempt, so the request was rejected."
- "Delivery crossed the provider boundary without acknowledgement; Synapse will
  not resend it automatically."
- "Stopping and replacing the current execution requires explicit approval."

When the user refines intent in the main conversation, the conductor identifies
the affected AC session through discovery, explains the selected AC and
effective mode, then issues only the authorized message. The user is never asked
for internal IDs. Unaffected ACs continue normally.

## Division of labor

Engine-owned:

- same-runtime retries;
- effort and model escalation;
- alternate-harness redispatch;
- active job cancellation and terminalization;
- runtime-specific safe-boundary acknowledgement and owned-turn abort;

Conductor-owned:

- verification of reported claims against ground truth;
- semantic interpretation of repeated failure;
- non-relaxing corrective directives;
- successor execution/generation selection;
- spec-change approval and user escalation;
- decision audit;
- mapping updated user intent to affected AC sessions;
- capability-aware message mode selection and truthful delivery assurance.

`frontier` tier is neither required nor sufficient for conductor engagement.
Only authoritative recovery closure permits mutation.

## Frugality coupling

The main conductor wakes only for classified attention. Phase changes, progress,
raw events, and heartbeats remain in the observer context. Verification is
delegated to one cheap read-only child, and action menus carry bounded evidence
instead of requiring the conductor to replay the complete event history.

The classifier may query complete history server-side, but each relay envelope is
bounded:

- at most 10 evidence event IDs;
- at most 10 rejected reasons, each length-limited;
- no raw tool output;
- no unbounded file content or model transcript.

## Non-goals

- No MCP server push; the observer still long-polls.
- No long-lived conductor or notification daemon; the conductor is the existing
  main session. A detached per-job worker may own execution because an stdio MCP
  process is scoped to one client turn and cannot be the durability boundary.
- No duplicate polling or cursor ownership in the main session.
- No direct in-flight artifact mutation, targeted concurrent redispatch, or live
  tier pin. Checkpoint intent redirection is the only active-session control
  introduced here.
- No OpenCode plugin-mode conductor in this RFC.
- No verifier writes to the active workspace.
- No automatic weakening of user-approved goals, ACs, constraints, or non-goals.
- No provider-independent promise of arbitrary token-stream preemption.
- No general-purpose peer chat network, channel membership, or transcript sync.

## Implementation slices

### S0 — sensor closure

- emit `execution.ac.recovery_exhausted`;
- add model escalation fields and `semantic_ac_key`;
- emit `auto.seed_qa.blocked`;
- add and persist resolved efficiency/frugality execution preferences;
- emit run configuration, execution plan, AC phase, and bounded discovery events;
- guarantee terminal full-history visibility.

S0 changes no host behavior.

### S1 — attention relay

- add `attention_or_ac_change`;
- request `stream="linked"` in observer contracts;
- classify bounded `meta.relay_events`;
- add ordered, discriminated action menus;
- retain observer protocol backward compatibility.

S1 may initially advertise only read-only actions and user escalation.

### S2 — active host playbook

- update root and packaged `run`/`auto`/`ralph` skills;
- update Codex host instructions;
- preserve exactly one observer plus at most one short-lived verifier;
- implement VERIFY → DECIDE → LOG → ACT behavior;
- keep OpenCode plugin mode unchanged.

S2 remains non-mutating until S3 tools are registered.

### S3 — audited successor recovery

- add `ouroboros_record_conductor_decision`;
- add persisted `conductor_directive` support to evolution/Ralph successors;
- add bounded autonomous successor budgets for Auto/Ralph;
- implement run-mode user approval for spec-changing successors.

### S4 — Ouroboros Synapse

- add runtime control capabilities and a live session-attempt projection;
- add the durable SessionSignal state machine and idempotent mailbox;
- add `ouroboros_session_signal` with explicit fallback semantics;
- implement `inform`/`after_turn` first, then runtime-specific checkpoint
  `redirect` and explicitly approved `replace` where enforceable;
- relay requested/effective mode and applied/rejected status through the existing
  observer;
- update English canonical `run`/`auto` guidance so the main session targets affected ACs and
  guarantees only the delivery state actually proven.

Current `after_turn` capability matrix:

| Runtime | State | Evidence boundary |
|---|---|---|
| persisted Codex CLI | enabled | same persisted thread |
| Claude Agent SDK | enabled | same native SDK session |
| persisted Claude MCP worker | enabled | same persisted worker session |
| OpenCode CLI | enabled | same OpenCode session ID |
| Goose CLI | enabled | same stable session name with explicit resume |
| Pi CLI | enabled | same exact project session ID |
| Hermes CLI | disabled | emitted ID was not resumable in the installed live probe |

This table is capability evidence, not provider availability. Missing
credentials, an invalid model, or an unhealthy upstream still fails as a runtime
error. `targeted_resume=True` without a successful same-session proof never opens
Synapse admission.

Each slice is independently shippable and has no hidden dependency on host prose.

## Acceptance criteria

1. Every trigger in the attention table has authoritative durable evidence; no
   trigger depends on parsing human-readable event detail.
2. `attention_or_ac_change` wakes for attention, meaningful progress, terminal,
   or timeout, but not unrelated raw events.
3. `meta.relay_events` carries bounded structured evidence. Only
   `attention_required` carries a non-empty action menu.
4. MCP action entries name registered tools. Dynamic host-supplied values are
   explicitly listed in `required_host_inputs`.
5. VERIFY uses one read-only host subagent before any ACT. Hosts without that
   primitive surface the event and do not mutate.
6. No recovery, artifact, routing, or specification mutation is offered or
   executed unless authoritative engine ownership is closed. Active-session
   messages follow the separate capability and authority rules in this RFC.
7. Intermediate `outcome_finalized` events, reaching `frontier`, or an unchanged
   polling window cannot independently authorize redispatch.
8. A repeated rejected verdict is grouped by
   `(judgment_scope_id, semantic_ac_key)` and produces a corrective directive
   containing the bounded `rejected_reasons`.
9. `ooo run` requests approval for spec-changing successors; `auto` and `ralph`
   may only auto-run bounded non-relaxing successors.
10. Every conductor decision has selected and terminal audit events, including
    declined and failed actions.
11. Observer cursor ownership remains exclusive and OpenCode plugin behavior is
    unchanged.
12. Root skills, packaged skill copies, Codex instructions, runtime guides, and
    artifact contract tests remain coherent.
13. `run` and `auto` accept and persist `efficiency_mode` and
    `frugality_assurance`; Auto forwards the resolved values to RUN/Ralph.
14. Start UX presents the choice in the user's conversation language and never
    exposes MCP polling, environment variables, or internal routing vocabulary as
    the primary explanation.
15. Strict assurance and shadow replay require explicit authorization. Efficiency
    mode alone never enables extra-cost baseline execution.
16. The main session proactively emits start, discovery, plan, routing,
    level, AC-verification, and terminal-assurance briefings without exposing MCP
    mechanics or raw logs. Guidance is canonical English and is phrased naturally
    in the active conversation language.
17. The plan briefing names total levels and the first scheduled ACs before level
    1 execution begins.
18. Model and harness wording is current-state based. Progressive escalation and
    alternate-harness changes are announced once when they occur.
19. Discovery summaries are bounded, materially deduplicated, and honest about
    unknown activity; they never forward raw tool calls or model reasoning.
20. On-demand AC assurance inspection does not acquire or advance the observer
    job cursor.
21. SessionSignal commands require stable scope, attempt, and execution guards;
    stale or terminal targets fail closed.
22. `inform`, `after_turn`, `redirect`, and `replace` have distinct
    capability and authority checks. No runtime is credited with redirect merely
    because it can resume a session.
23. A successful command distinguishes `queued` from `applied`; the main session
    never reports intent adoption without an application acknowledgement.
24. Unsupported redirect falls back only to the explicitly requested mode and
    reports the effective mode. Hard abort is never an implicit fallback.
25. User messages outrank conductor/worker messages, and specification-changing
    content requires explicit user confirmation.
26. SessionSignal delivery is bounded, secret-filtered, idempotent, replay-safe,
    and auditable from request through completion or rejection.
27. Session-control relay events use the existing exclusive observer cursor;
    direct message commands never create a second polling owner.
28. Runtime-specific tests prove safe-boundary behavior, abort ownership, and
    truthful degradation for every supported harness.
29. Ambiguous provider delivery becomes `delivery_uncertain` and is not
    automatically retried or reported as applied.
30. Specification-changing intent cannot be injected into only one live AC; it
    creates an approval-bound shared successor/replacement contract for every
    affected AC.
31. Every non-plugin Start* acceptance is owned by a process whose lifetime is
    independent of the accepting stdio MCP turn. Parent shutdown cannot create
    `mcp.job.interrupted`; a later controller can observe and cancel the owner
    through durable state.
32. When no real host observer is available, the main session does not claim
    proactive observation. The durable worker continues, and a later parent
    turn can catch up the same linked events and terminal result.
