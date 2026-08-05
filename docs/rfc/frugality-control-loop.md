# RFC — Token frugality as one control loop (attribution + advisory guardrails)

> Status: **Draft**
> Relates to [discussion #1377](https://github.com/Q00/ouroboros/discussions/1377)
> (token frugality). Composes with the spend-decision mechanism RFCs —
> [spend estimator](https://github.com/Q00/ouroboros/pull/1404) and
> [spend actuator (effort dial)](https://github.com/Q00/ouroboros/pull/1405) (#1384) — and the
> [decomposition reliability](https://github.com/Q00/ouroboros/pull/1406) RFC (#1385). Sibling
> usability thread: [#1376](https://github.com/Q00/ouroboros/discussions/1376).

## Summary

Frugality in Ouroboros is **waste-only and goal-subordinate**: it minimizes only
tokens that did **not** advance a *verified* acceptance criterion — never the
comprehensiveness the user is paying for, and never by halting a run mid-way. The
owner accepted this stance verbatim and reframed the four #1377 themes as **one
control loop**, not four features — the way TCP regulates a network whose capacity
it cannot observe:

| Control-loop role | #1377 theme | Slice |
|---|---|---|
| **Sensor** | Spend attribution (per-stage cost) | **This RFC, first** |
| **Learning layer** | Reflective guardrail loop (advisory v1) | **This RFC** |
| **Policy input** | User-held assurance dial | This RFC (records the lever) |
| **Controller's initial estimate** | Completion-feasibility pre-flight | reliability slice (concurrency-aware)¹ |
| **Controller's decision** | How much capability per unit | [spend estimator](https://github.com/Q00/ouroboros/pull/1404) + [actuator](https://github.com/Q00/ouroboros/pull/1405) |

This RFC scopes the **first slice the owner greenlit: spend attribution (the
sensor) + the advisory guardrail loop (the learning layer)** — both low-risk, and
both unlock the rest.

¹ Feasibility is concurrency-aware, not quota-only — see the reliability slice;
the owner confirmed the original incident was a GLM *concurrency-cap* rejection.

## Context

### The two hard invariants

The destination (working product, all ACs met, verified) is fixed; only the *path*
cost varies. Every frugality mechanism must:

1. **Never reduce the achieved outcome.**
2. **Never increase rework risk.**

These are the acceptance test for any mechanism here. A prospective budget cap that
halts a run mid-way violates both and is an explicit non-goal — waste is often
indistinguishable from genuine exploration *in advance*, so guardrails are emitted
**retrospectively**, only once spend is clearly non-advancing.

### Layered ownership (with one sharpening)

- **Ouroboros (methodology)** owns *how much* work and at *what fidelity* it
  commissions, plus **spec crispness** (a sharper seed prevents the priciest waste —
  rework).
- **The agent runtime + LLM backend** own *how cheaply* a commissioned unit
  executes (re-reads, retries, regeneration); core can only **advise** (a guardrail
  in the spec) or **route** there.
- **Sharpening (owner):** **fan-out discipline is core's job, not the runtime's** —
  and core already shipped the first response. `orchestrator/backend_limits.py`
  serializes delivery to **1 AC at a time** for any backend whose limits Ouroboros
  can't know (every CLI runtime, hermes included), raised only via explicit
  `OUROBOROS_MAX_CONCURRENCY`; [#1372](https://github.com/Q00/ouroboros/pull/1372)
  added configurable rate-budget pacing for non-Claude delivery. The 14-AC stampede
  from the #1377 incident cannot recur in that form.

### Signals exist but are not aggregated into waste

Cost/token signals are already event-sourced (`orchestrator/events.py`
`estimated_cost_usd`; `orchestrator/workflow_state.py` `estimated_tokens`; persisted
in `session.py`) — but nothing aggregates them into a *waste* view (tokens on ACs
that later failed/were re-done, dead escalations, stagnation cycles). Cost is
display-only in the TUI (`tui/widgets/cost_tracker.py`,
`tui/components/token_tracker.py`).
`observability/retrospective.py` already produces per-run retrospectives and
`resilience/stagnation.py` detects wasted-motion — but nothing carries a lesson
forward between runs.

## Proposal

### 1. Spend attribution (the sensor) — ships first

A frugality aggregator joins the event-sourced cost/token signals with AC outcomes,
tier/effort history, and stagnation events to compute, per run:

- `total_cost` and an itemized **avoidable** portion (`rework`, `dead_escalation`,
  `stagnation`), in tokens and estimated USD;
- **per-stage attribution**: interview / execute / consensus.

Surfaced as a non-judgmental line in the run summary (CLI + the journey progress
block) and a TUI panel — e.g. *"~$0.40 of ~$1.10 went to re-work; biggest
contributor: AC-7 escalated twice without progress."* Emitted via
`observability/retrospective.py`; everything labeled **estimated**, never false
precision.

**Floor-preserving:** the aggregator only flags motion that failed to advance a
*verified* AC. A long, first-try-successful AC is never flagged.

### 2. Reflective guardrail loop (the learning layer) — advisory v1

After each unit (AC / phase / generation / session), a short, conservative
efficiency retrospective. Where spend was clearly non-advancing, emit a
*generalizable* guardrail into a frugality policy set, each tagged by owner:

- **Methodology-level** (Ouroboros acts): prune assurance on low-risk ACs, cap
  decomposition depth, fewer generations, tighten the spec it hands down, commit
  lower reasoning effort to trivial work.
- **Execution-level** (runtime owns): Ouroboros can only **advise** (pass the
  guardrail down in the spec/prompt) or **route** to a cheaper backend.

**v1 is advisory only** — it proposes one guardrail per session and records it;
auto-enforcement of high-confidence guardrails is a later bet. Guardrails are
project-scoped (`.ouroboros/`, to avoid cross-project contamination), auditable, and
reversible. A guardrail may only remove motion that did not advance a verified AC.

### 3. The user-held assurance dial (the policy input)

The *one* legitimate cost/assurance trade — consensus on every AC vs. only risky
ones; 1 generation vs. 3 — surfaced as a single explained dial the **user** sets,
never an automatic cut. This RFC records the lever and wires attribution to it;
codified guardrails may *propose* a default position but never override the user.

### 4. Focused-evolution proof receipts

Focused evolution removes already-verified AC nodes from Wonder, Reflect, and
execution. Its exact `active_ac_indices` / `frozen_ac_indices` trace proves a
smaller logical working set, but node counts alone do not prove resource savings.
AC identity is stable across that shrinking frontier: legacy shorter/reordered
lists and patch/description disagreement are rejected. A criterion carrying a
verify command, artifact/output assertion, or investment authority cannot change
description until Reflect has a schema for proposing the complete replacement
contract; otherwise old mechanical evidence could be rebound to new semantics.
Gen 2+ therefore emits a lineage observation containing runtime-attributed tokens
for the complete generation call universe (Wonder, every Reflect attempt,
validation repair, assertion extraction, semantic/consensus evaluation, AC
execution, dependency analysis, decomposition policy/attestation/repair,
coordinator review, and any experiment-only shadow replay), wall time,
calls/retries, the final evaluation, per-AC verdicts, regressions, and TraceGuard
grounding results. A Wonder-only early stop still emits a durable
`insufficient_data` receipt rather than disappearing from the evidence history.

The deterministic comparison is deliberately paired and fail-closed:

- **control**: `focused_evolution=false` and `scoped_reexecution=false`;
- **treatment**: `focused_evolution=true` with at least one frozen node;
- both arms must start from the same clean Git commit in distinct worktrees and
  have the same canonical full-Seed/previous-evaluation input fingerprint. The
  fingerprint excludes only Seed identity/provenance fields (`seed_id`,
  `created_at`, `interview_id`, and `parent_seed_id`); task type, brownfield
  context, structured AC contracts, evaluation principles, exit conditions,
  semantic metadata, and plugin extras remain authoritative;
- every primary runtime attempt must have one unique runtime-token receipt and
  one unique TraceGuard deliver verdict bound to the same `ac_id`, retry,
  `session_attempt_id`, primary `ac_dispatch_id`, and root AC index. The set of
  dispatched root indices must exactly equal the arm's active working set;
  zero or non-finite runtime usage is incomplete evidence;
- a session-signal follow-up makes the arm incomplete until follow-up turns have
  a stable cross-arm semantic chain identity; their aggregate spend is never
  silently treated as configuration-equivalent primary work;
- every non-executor generation provider call must likewise carry positive,
  runtime-reported usage. Missing, zero, duplicate, malformed, opaque, or
  unrelated call evidence makes that arm incomplete; completion prompt/output/
  total counters must reconcile exactly and no partial subtotal is used;
- executor-internal auxiliary provider calls use the same generation-scoped
  accounting boundary. An arbitrary executor is opaque unless it explicitly
  attests that all of its provider effects use the tracked boundary;
- normalized primary-runtime and auxiliary-provider configurations (backend,
  model/tier/mode, effort level/mode, permission mode, and applicable request
  settings) must be present, non-unknown, and match across
  control and treatment. Equality preserves assignment, not merely the unordered
  set of values: each treatment root AC must match the same control root, and each
  realized auxiliary phase role must match the same control role. Per-root retry
  and per-role call sequences preserve order and multiplicity. A root or role
  present in both arms must have an exactly equal sequence; only an entire
  control-only root/role may be removed. Partial removal inside a shared unit is
  `insufficient_data` until a stable cross-arm semantic call identity exists,
  because value-only subsequences cannot distinguish deletion from configuration
  reallocation. Missing, blank, or unknown phase identity is incomplete evidence. The
  focused/scoped actuator flags are the only intended experimental difference;
  configuration mismatch or reassignment is `insufficient_data`;
- a `full_graph` control must execute every Seed root with no frozen partition;
  an arm label alone is not coverage evidence. Every treatment active/frozen
  set must be a disjoint, complete partition of the same Seed;
- auxiliary request fingerprints bind tools, system-prompt identity, model,
  effort/service/permission dials, extra request kwargs, and fresh/scoped session
  mode. Completion adapters must resolve the task profile once, seal that exact
  config for dispatch, and satisfy a centrally registered, exact-key schema for
  the effective model/profile/sampling/effort request and instance envelope.
  Endpoint/provider routing, timeout, retry policy, and all other provider-facing
  state are bound; credential authority is represented only by a deterministic
  HMAC, never the credential. Proof-eligible adapters must carry the prepared
  endpoint and credential authority in memory through the actual provider-call
  boundary; dependency-global routing or re-resolving mutable environment state
  after attestation is ineligible. The registered retry field must mechanically prove
  one measured attempt—an adapter's self-asserted completeness flag has no
  authority. Empty, partial, extra, secret-bearing, cyclic, over-deep, oversized,
  non-finite, or otherwise malformed attestations are `insufficient_data` and
  must not fail evolution itself. Resumed transcript state has no stable
  cross-arm semantic identity and is likewise `insufficient_data`. The final provider-resolved model is
  revalidated after completion responses and agent streams; padded or
  case-varied `unknown`, conflicting effective models, cyclic/deep opaque
  configuration, or unreconciled counters are incomplete;
- each arm's final evaluation must cover every current Seed AC exactly once,
  in range and bound to the structured AC semantic identity, and the complete
  final semantic Seed fingerprint must be identical across the pair;
- PASS requires at least 10% fewer runtime-attributed **total generation** tokens
  and zero final-approval, evaluation-score, evaluation-stage, drift,
  reward-hacking-risk, per-AC verdict/score, lineage-regression, or grounding
  degradation. A quality metric present in only one arm is incomparable and
  returns `insufficient_data`;
- missing control, isolation, token telemetry, final-gate evidence, or TraceGuard
  evidence returns `insufficient_data`; partial evidence is never summed into a
  savings claim. Savings below 10% return
  `fail_no_savings`; any quality loss returns `fail_quality_regression`.

Proof-relevant event reads and generation-local provider calls use fixed
materialization caps. Reaching either cap records only bounded evidence plus an
overflow marker and cannot PASS. Individual, per-axis aggregate, and combined
primary-plus-auxiliary token totals must all remain finite, so long lineages and oversized telemetry cannot turn receipt
collection into an unbounded memory sink or erase a durable non-PASS result.

Production evolve records the treatment receipt but never auto-runs the control.
The paired arm is an experiment/benchmark operation; doubling every production
generation would spend the savings the controller is intended to preserve.
The stricter completeness contract is receipt schema v2; legacy v1 observations
and proof events are readable history but are not eligible for a current PASS.

## Out of scope (deliberately)

- **The spend decision itself** (how much capability per unit) — that is the
  [estimator](https://github.com/Q00/ouroboros/pull/1404) and [actuator](https://github.com/Q00/ouroboros/pull/1405)
  RFCs (#1384).
- **Adaptive concurrency** — evolving `backend_limits` from a static cap to a
  signal-driven controller is the named **second slice** of the frugality
  workstream; this RFC ships the sensor it would read from.
- **Cross-run calibration / auto-enforced guardrails** — v2, once enough labeled
  outcomes accumulate (cold-start).
- **Hard budget caps** — explicitly rejected (floor-preserving only).

## Acceptance criteria

1. Every run ends with a waste retrospective: `total_cost`, an itemized
   `avoidable_cost` (rework / dead-escalation / stagnation), and per-stage
   attribution — all labeled estimated.
2. A run with a known re-done AC reports non-zero `rework`; a clean run reports ~0
   avoidable; a long first-try-successful AC is **not** flagged (floor-preserving).
3. The advisory loop emits at most one generalizable, owner-tagged, reversible
   guardrail per session into project-scoped `.ouroboros/`, and never one that would
   reduce a verified outcome.
4. Moving the assurance dial visibly changes assurance behavior and is never applied
   silently.
