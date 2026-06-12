# RFC — The spend actuator: a reasoning-effort dial, not model switching

> Status: **Draft**
> Relates to [discussion #1384](https://github.com/Q00/ouroboros/discussions/1384)
> (complexity→investment mechanism). This is the **actuator / wiring half** of the
> split the owner requested; the **estimator half** is
> [spend estimator](https://github.com/Q00/ouroboros/pull/1404). Frugality stance:
> [#1377](https://github.com/Q00/ouroboros/discussions/1377) /
> [frugality control loop](https://github.com/Q00/ouroboros/pull/1403).

## Summary

Given the per-unit difficulty + stakes estimate from the
[estimator RFC](https://github.com/Q00/ouroboros/pull/1404), *something* must act on it during execution.
Today nothing does — and the machinery that was meant to (`PALRouter` /
`ModelRouter` and a 1×/10×/30× tier-cost table) has **no live call site**. The
owner reshaped the actuator decisively: **the lever is reasoning effort, not model
switching.** This RFC specifies that actuator — wiring an **effort-first investment
decision** into the live executor, behind a **capability matrix** with an explicit
**advise-fallback**, and **removing** the unwired tier machinery.

## Context

### Why effort, not model tier

Modern backends expose a thinking/effort dial (Anthropic thinking budget,
OpenAI-style reasoning effort, Codex `model_reasoning_effort`). Routing on that dial
instead of across model families has three structural advantages that map directly
onto the estimator's safety properties:

1. **Monotonic and cliff-free by construction** (estimator property 8) — effort is a
   scalar knob, not a discrete family jump.
2. **Escalation is cheap**, so **fail-safe-on-uncertainty becomes practical**
   (property 3) — when the estimate is unsure, raise effort rather than gamble on a
   capability-class change.
3. **A mis-estimate no longer changes the executor's capability class** — which
   shrinks cold-start risk, because the estimator can be wrong *cheaply* while
   calibration data accumulates.

Model-tier switching survives only as the **coarse outer rung** of an escalation
ladder, and as the **fallback where a backend exposes no effort dial** (GLM's
thinking toggle is nearly binary, for instance).

### The live executor, and what to delete

The live executor is `orchestrator/parallel_executor.py` (the
`execution/double_diamond.py` path is off the live path — see the
[decomposition reliability RFC](https://github.com/Q00/ouroboros/pull/1406)). The unwired
`routing/router.py`, `routing/tiers.py`, and `plugin/orchestration/router.py` encode
the **wrong actuator** and are removed, not wired — fixing a router nothing calls
would be motion, not progress.

## Proposal

### 1. Effort-first investment decision in the live path

Wire a decision that maps the estimator's `(difficulty, stakes, confidence)` to a
**reasoning-effort level** per unit, applied in `orchestrator/parallel_executor.py`.
The decision is **load-bearing where the backend permits** (see the capability
matrix) and emits its driving axis + confidence as an event.

### 2. The escalation ladder

```
effort low  →  effort high  →  bigger model
```

Escalation is the response to low confidence and to a unit that under-performs at
its current level. Model-tier switching is only the top rung. Fail-safe default:
**escalate, never cheapen, under uncertainty.**

### 3. Capability matrix + advise-fallback (estimator property 11)

A per-backend capability matrix (extending `backends/capabilities.py`) records
whether a backend exposes a per-call effort dial:

- **Routable backend** (effort dial available) → the decision **sets** the effort
  level directly.
- **CLI-runtime backend** (no per-call control: hermes, codex, …) → the decision can
  only **advise** — the chosen effort is handed down as a guardrail in the spec /
  prompt. A design that assumes universal routability is vacuous on exactly the
  backends the #1377 incident came from.

### 4. Remove the tier machinery

Delete `routing/router.py`, `routing/tiers.py`, and `plugin/orchestration/router.py`
(and the always-Opus meta-decision defaults in `config/models.py`), with a test
asserting no non-test importer remains before removal.

## Out of scope (deliberately)

- **How the estimate is produced** — the [estimator RFC](https://github.com/Q00/ouroboros/pull/1404).
- **Cross-run calibration of the ladder boundaries** — v2.
- **A current, cross-provider cost model** — the actuator needs only *relative*
  ordering (more difficult/stakes → more effort); billing-accurate costing is
  separate. The user-held assurance dial that bounds economize-vs-assure lives in
  the [frugality control loop RFC](https://github.com/Q00/ouroboros/pull/1403).

## Acceptance criteria

1. A live (non-test) call site applies the **effort decision** on an effort-capable
   backend; a high-stakes short unit gets higher effort; an uncertain estimate
   escalates effort.
2. On a CLI runtime with no effort dial, the decision falls back to **advise**
   (guardrail in the spec) and records that it did so — asserted by a test.
3. The escalation ladder raises effort before switching model family, and never maps
   a higher-difficulty/higher-stakes unit to lower effort (monotonic).
4. `routing/router.py`, `routing/tiers.py`, and `plugin/orchestration/router.py` are
   removed with no `src/` regression.
