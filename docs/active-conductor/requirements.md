# Active Conductor Requirements

> Generated: 2026-07-12
> Status: Clarified for planning

## Original request

> "Review the Active Conductor RFC; I want to implement it."
>
> "Revise the RFC and prepare the plan before implementation."
>
> "Use an original name instead of IRC and implement it from a clean-room specification."

## Goal

Revise the Active Conductor RFC so every promised trigger, relay event, host
decision, and corrective action is implementable against an explicit durable
contract. Produce an implementation plan before changing runtime behavior.

## Scope

Included:

- authoritative recovery, model escalation, deliver-verdict, Seed-QA, frugality,
  and stagnation sensor contracts;
- `ouroboros_job_wait` attention classification and observer wake behavior;
- structured host action menus;
- `run`, `auto`, and `ralph` conductor playbooks;
- conductor decision logging;
- bounded successor executions or generations after engine recovery closes;
- start-time efficiency choice for `run` and `auto`, described in user-facing
  outcome language;
- persisted frugality assurance preference across Auto handoff and resume;
- proactive start, Discover, execution-plan, routing, level, and AC assurance
  briefings from English canonical host guidance;
- on-demand per-AC assurance inspection without duplicate polling;
- Ouroboros Synapse: capability-aware SessionSignal delivery and intent redirect for the
  provider-native AC sessions Ouroboros already owns;
- durable requested/accepted/queued/delivering/applied/rejected/uncertain/
  completed delivery audit and main-session assurance;
- root and packaged skill consistency, including Codex instructions.

Excluded:

- direct in-flight artifact mutation or concurrent targeted redispatch;
- live model-tier pinning of an active worker;
- MCP server push or a long-lived conductor/notification daemon; short-lived
  per-job worker ownership is allowed where the accepting stdio MCP process
  cannot survive the run it starts;
- OpenCode plugin-mode changes;
- automatic relaxation of user-approved goals or acceptance criteria;
- provider-independent hard preemption or a general-purpose chat service.

## Constraints

- The observer remains the exclusive `job_wait` cursor owner.
- The conductor never duplicates engine-owned retry, effort/model escalation, or
  alternate-harness redispatch.
- Recovery, artifact, routing, and specification mutations require authoritative
  `engine_ownership.state="closed"`; bounded SessionSignals use the separate
  capability and authority contract.
- VERIFY is performed by one short-lived read-only host subagent.
- Hosts without a verifier primitive must not ACT.
- Dynamic corrective inputs must be explicit in the action schema.
- Decision and evidence payloads are bounded and auditable.
- Strict frugality proof may add cost and therefore requires explicit consent.
- Progress UX must describe meaningful user-level state, not raw tool activity.
- Runtime resumability must not be treated as proof of checkpoint redirect support.
- SessionSignals require execution and attempt guards, bounded payloads,
  idempotency, and an applied acknowledgement before adoption is claimed.
- User intent outranks conductor/worker messages; spec-changing messages and
  hard replacement require explicit user authority.
- Host guidance is authored once in English. The host phrases it naturally in
  the user's current conversation language; persisted event values stay stable.
- A non-plugin Start* receipt is not accepted until a cross-turn worker owns the
  durable job identity; the stdio MCP process is never the lifetime guarantee.

## Success criteria

- The revised RFC has no references to nonexistent events or tools without also
  defining the slice that creates them.
- All attention triggers have durable, machine-readable evidence.
- The relay can wake on attention without waking on every raw event.
- Host instructions distinguish observer, verifier, conductor, and worker roles.
- Recovery/artifact mutation happens only as a bounded successor after recovery
  closure; active-session control changes future decisions only at proven safe
  boundaries.
- Every conductor decision, including refusal or failure, is logged.
- Implementation can be delivered as independently testable S0-S4 slices.
- The main session discovers live AC content, selects the affected session from
  the human's natural-language intent without exposing internal IDs, and
  truthfully distinguishes queued, safely applied, deferred, rejected, and
  explicitly aborted delivery.
- Unsupported redirect degrades only through a declared fallback and never
  silently claims that an interrupt occurred.
- Ending the accepting main turn does not interrupt run/auto/evaluate/evolve/
  Ralph work. For live Codex relays, however, a confirmed observer keeps the
  parent turn open in an interruptible `wait_agent` loop because child mailbox
  messages cannot revive an ended parent turn. Without an observer, the host
  says so and catches up from durable events on the next parent turn.

## Decisions

| Question | Decision |
|---|---|
| Can the conductor directly mutate an active AC's artifacts or routing? | No; only a successor after engine ownership closes. |
| Is model tier changed in flight? | No; it is a successor-run tier override. |
| How is retry exhaustion detected? | A dedicated `execution.ac.recovery_exhausted` event. |
| How are rejected verdicts grouped? | `(lineage_id or root_job_id, semantic_ac_key)`. |
| Is VERIFY an MCP tool action? | No; the action menu discriminates host-native verification from MCP calls. |
| Can Auto/Ralph act autonomously? | Yes, only for bounded non-relaxing successors. |
| What happens without host subagents? | Surface attention and stop before ACT. |
| How does efficiency mode map to frugality? | `adaptive` defaults to lightweight `observe`; `quality_first` defaults to `off`. |
| Does efficiency mode enable shadow replay? | No; strict baseline proof is a separate explicit opt-in. |
| How is the choice presented? | From English canonical guidance, phrased naturally in the user's current conversation language before start when no preference is known. |
| When does the main session first speak? | Immediately after start acceptance, then after configuration and plan resolution. |
| How are model/harness details phrased? | As current state with changes announced, because retries may progressively escalate or switch harnesses. |
| What does Discover reveal? | A bounded summary of targets and purpose, not raw commands, file reads, or reasoning. |
| What plan detail is guaranteed? | Total ACs/levels, parallelizable groups, dependencies, and first scheduled ACs. |
| What is this subsystem called? | **Ouroboros Synapse**; each directed unit is a `SessionSignal`. |
| What is the clean-room boundary? | Implement only this RFC's behavior; do not copy external source, protocols, names, prompts, or registries. |
| What is the default intent-change behavior? | `redirect` at a runtime-declared checkpoint, with explicit `after_turn` fallback when requested. |
| Can the conductor hard-stop an AC on its own? | No. `replace` requires explicit user approval and runtime support. |
| How is a target protected from stale delivery? | Scope ID + unique attempt ID + expected execution ID + idempotency key. |
| What if the runtime cannot redirect? | Report the limitation and apply only the declared fallback or reject the signal. |
| Can a changed Seed/AC be redirected into one worker? | No. It requires an approved shared successor/replacement contract for all affected ACs. |
