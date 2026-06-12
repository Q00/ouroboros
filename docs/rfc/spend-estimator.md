# RFC — The spend estimator: difficulty + stakes from measured inputs

> Status: **Draft**
> Relates to [discussion #1384](https://github.com/Q00/ouroboros/discussions/1384)
> (complexity→investment mechanism). This is the **estimator half** of the split the
> owner requested; the **actuator half** is the
> [spend actuator (effort dial) RFC (#1405)](https://github.com/Q00/ouroboros/pull/1405).
> Both serve the frugality stance: [#1377](https://github.com/Q00/ouroboros/discussions/1377) /
> [frugality control loop (#1403)](https://github.com/Q00/ouroboros/pull/1403).

## Summary

Ouroboros's "spend the cheap option on cheap work" lever depends on something that
reliably identifies cheap (and hard, and high-stakes) work. Today that estimate is
**unprincipled** — and, as the owner verified, the routing built on it has **no
live call site** at all. This RFC specifies the estimator: it produces, per unit of
work, a **difficulty** estimate and a **stakes** estimate from **measured inputs**,
with explicit confidence and faithful rationale. It does *not* decide what to do
with that estimate — that is the [actuator RFC](https://github.com/Q00/ouroboros/pull/1405).

The owner accepted #1384's acceptance properties as the right direction with one
restructure: the **safety properties are v1 invariants; calibration and cross-run
learning demote to v2 maturity goals** (they collide with cold-start); and a **new
property — runtime-scoped applicability** — is added.

## Context (grounded in current `main`)

The estimate is computed but its consumers are unprincipled or absent:

- **No live *routing* consumer.** The tier router — `PALRouter.route()` /
  `ModelRouter.route()` (`routing/router.py`, `plugin/orchestration/router.py`) — has
  **no non-test call site**, so the routing lever does not exist at runtime (the
  [actuator RFC (#1405)](https://github.com/Q00/ouroboros/pull/1405) removes that tier
  machinery). `estimate_complexity` itself *is* called once outside tests —
  `execution/atomicity.py:271` — but that module is off the live path and slated for
  deletion (see the [decomposition reliability RFC (#1406)](https://github.com/Q00/ouroboros/pull/1406)),
  so **no live-path consumer of the estimate remains**. This RFC keeps and re-grounds
  the *estimate*; its live consumer becomes the actuator.
- **Length treated as difficulty.** Complexity is a weighted sum of three length-ish
  scalars: tokens ×0.30 (÷4000), tools ×0.30 (÷5), depth ×0.40 (÷5). "Fix the race in
  the lock-free queue" scores ≈0 and would rate trivial — the hardest units are
  usually the shortest to state.
- **Fabricated inputs.** The tool-dependency factor (30% weight) is a count
  re-encoded as placeholder strings (`tool_dependencies=[f"tool_{i}" …]`) — no real
  signal.
- **Uncalibrated constants** (weights, thresholds, normalizers) with no empirical
  basis, and the only adaptive signal is escalate-after-2-failures *within one run*.

## Proposal

### Two axes, measured inputs only

The estimator returns, per unit: **`difficulty`**, **`stakes`**, and a
**`confidence`** band, plus the real inputs that drove them.

- **Difficulty** is decoupled from length — a short, conceptually hard unit must
  rate hard and a long, mechanical one easy. Token count may inform but must not
  dominate.
- **Stakes** = cost-of-being-wrong: reversibility, blast radius, sensitivity. A
  trivial-to-state but high-stakes unit (auth, a schema migration, a payment path)
  must be investable on stakes alone.
- **Every weighted factor derives from a real property of the unit.** A factor that
  cannot be measured cannot carry weight — no counts re-encoded as lists.

### The acceptance properties (restructured per owner)

**v1 invariants** (a design violating any one is unacceptable):

- **(2) Two axes:** difficulty *and* stakes.
- **(3) Fail-safe, safety-asymmetric:** under-powering a hard/high-stakes unit is far
  worse than over-powering a trivial one; the estimator represents its own
  uncertainty and, when uncertain, signals **escalate, not cheapen**.
- **(8) Monotonic and stable:** more difficult / higher-stakes never maps cheaper;
  small input changes produce small output changes — no threshold cliffs.
- **(10) Faithful, auditable rationale:** every estimate emits which axis drove it,
  the confidence, and the real inputs used — the recorded numbers are the ones
  actually used, never decorative placeholders.
- **(11, owner-added) Runtime-scoped applicability:** the estimate is consumed
  differently per backend — *routable* where Ouroboros calls the LLM directly,
  *advisory* where it delegates to a CLI runtime. The estimator records enough for
  both; see the actuator RFC's capability matrix.

**v2 maturity goals** (gated on cold-start — a single user's runs don't yield enough
labeled outcomes; v1 only *records* their inputs):

- **(6) Calibrated to observed outcomes, inspectably** — constants justified by
  recorded success/failure/rework/cost.
- **(7) Closed-loop across runs** — a unit that failed at a given investment level
  shifts how similar future units are estimated.

Property **10** is the enabler: by recording real inputs and real outcomes in v1,
the event stream becomes the data the v2 calibration loop reads.

## Out of scope (deliberately)

- **What to do with the estimate** — model/effort selection, the escalation ladder,
  and the capability matrix are the [actuator RFC](https://github.com/Q00/ouroboros/pull/1405).
- **Cross-run calibration loop** — v2 (this RFC lays its event-stream groundwork).
- **A perfect difficulty oracle** — "better than length, two-axis, and safe when
  unsure" is the v1 bar.

## Acceptance criteria

1. "Fix the race in the lock-free queue" rates **hard**; a long boilerplate unit
   rates **easy** (difficulty decoupled from length).
2. A short, high-stakes unit (auth / migration / payment) rates **high-stakes** and
   is investable on stakes alone.
3. Every emitted estimate carries `difficulty`, `stakes`, `confidence`, and the
   **real** inputs used — and a test asserts no field is a hardcoded placeholder.
4. The mapping is monotonic and cliff-free under small input perturbations.
5. Under low confidence, the estimate signals **escalate**, never cheapen.
