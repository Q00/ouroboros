# RFC — Token frugality as one control loop (attribution + advisory guardrails)

> Status: **Draft**
> Relates to [discussion #1377](https://github.com/Q00/ouroboros/discussions/1377)
> (token frugality). Composes with the spend-decision mechanism RFCs —
> [spend estimator](./spend-estimator.md) and
> [spend actuator (effort dial)](./spend-actuator-effort-dial.md) (#1384) — and the
> [decomposition reliability](./decomposition-reliability.md) RFC (#1385). Sibling
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
| **Controller's decision** | How much capability per unit | [spend estimator](./spend-estimator.md) + [actuator](./spend-actuator-effort-dial.md) |

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
display-only in the TUI (`tui/cost_tracker.py`, `tui/token_tracker.py`).
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

## Out of scope (deliberately)

- **The spend decision itself** (how much capability per unit) — that is the
  [estimator](./spend-estimator.md) and [actuator](./spend-actuator-effort-dial.md)
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
