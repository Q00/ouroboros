# RFC — Atomicity & decomposition reliability (re-grounded on the live executor)

> Status: **Draft**
> Relates to [discussion #1385](https://github.com/Q00/ouroboros/discussions/1385)
> (atomicity & AC-decomposition reliability). Sibling mechanism:
> [spend estimator](./spend-estimator.md) (#1384) — both pre-execution judgments,
> both shared the fabricated-output failure pattern. Frugality stance:
> [#1377](https://github.com/Q00/ouroboros/discussions/1377) /
> [frugality control loop](./frugality-control-loop.md).

## Summary

Before fanning out, Ouroboros decides *is this unit atomic (execute it) or not
(decompose it first)?* — the gate in front of fan-out, where a wrong call is
asymmetric (under-decompose → rework; over-decompose → wasted motion) and compounds
recursively. The owner's verification of #1385 produced a finding that
**re-grounds the entire effort**:

> The module the original analysis dissected — `execution/atomicity.py` +
> `execution/decomposition.py`, via `DoubleDiamondExecutor` — has **no non-test
> caller in `src/`**. Across recent runs: 115 `execution.ac.completed` events,
> **zero** atomicity-check events. **The live executor is
> `orchestrator/parallel_executor.py`.**

So this RFC does two things: **delete the dead modules** (repairing fabricated
fields in code nothing calls is motion, not progress), and **re-aim the
decomposition discipline at `parallel_executor.py`'s actual split path**.

## Context

### The dead path (to be deleted)

Every file-level finding from #1385 holds, but against an off-path module:

- The LLM path discards its analysis and hardcodes the numeric fields
  (`complexity_score = 0.3 if is_atomic else 0.8`, `tool_count = 1|4`,
  `estimated_duration = 60|600`) — only the boolean is real.
- `MAX_DEPTH = 2` contradicts the docstring's "5 levels (NFR10)" and the caller's
  own `max_depth`.
- A single low-temperature sample with a keyword fallback (`and`, `then`,
  `database` …) that is *anti-correlated* with reality ("add a button and a label"
  → non-atomic; "re-architect the persistence layer" → atomic).
- On failure the loop silently defaults to "atomic" — the *costlier* error
  direction, chosen by accident.
- Decomposition is asserted MECE but only validated for count / non-empty /
  parent-cycle; depth defaults to Opus.

### The live path (to be hardened)

`orchestrator/parallel_executor.py` has better bones — profile-based split
(`axis: testable_unit`, an explicit `min_unit`, an `atomic_verifier_verdict`,
recorded depth-cap warnings) — but suffers the **same disease at larger scale**: a
~6,300-line module whose load-bearing judgments ride on text-surface heuristics
(e.g. AC classification via `re.finditer(r"\b(?:and|then|while|plus)\b|[,;:]", …)`,
the exact anti-correlated shape called out above), much of it accreted per-failure
regex patches.

### Error bias: attempt-then-bounce is already how the live system works

The live system runs ACs at seed granularity and lets **evaluation bounce**
failures — 115 ACs executed with *zero* pre-execution atomicity judgments. So the
work is not *choosing* the error bias (it is empirically attempt-then-bounce); it is
adding **discipline** to the loop that already exists.

### Lineage

`DoubleDiamond` was Ouroboros's methodology-first executor — understand
(Discover/Define) *before* judging atomicity, decompose recursively with discipline.
Its philosophy wasn't wrong; its implementation abandoned it (the hardcoded scores
are the moment it did). The properties below largely **re-derive that philosophy as
testable criteria** — so deleting the module is not discarding the idea, it is
transplanting it onto the executor that actually runs.

## Proposal

### 1. Delete the off-path modules

Remove `execution/atomicity.py` + `execution/decomposition.py` (and the dead
`DoubleDiamondExecutor` atomicity/decomposition wiring and their always-Opus
defaults), with a test asserting they have no non-test importer before removal. The
off-path `ac_atomicity_checked` event goes with them — it is removed, not fixed.

### 2. A verified decomposition step in `parallel_executor.py`

After a split: check the children are **collectively exhaustive** (no dropped
scope), **mutually exclusive** (no overlap), and **each measurably closer to atomic**
than the parent. A failed check triggers **one repair retry**, then escalates — never
silent acceptance. Depth caps must be internally consistent, and any path that forces
a known-non-atomic unit to run as atomic must **record that compromise** as an event.

### 3. Discipline on the existing attempt-then-bounce loop

- **Bounded attempts** at seed granularity (the current behavior, made explicit).
- **Bounce-cause classification** when evaluation rejects: *too-big* vs. *bad-spec*
  vs. *environment* — only *too-big* should drive a decomposition.
- **Bounce-trace as decomposition input**: split from *what was actually attempted
  and what remains*, not from the AC text. This beats splitting from a text-surface
  guess.

### The acceptance properties (restructured per owner)

**v1 invariants:** executor-relative judgment (relative to the effort/tools/context
that will run it, per the [actuator RFC](./spend-actuator-effort-dial.md)); graded
confidence, not a bare boolean; outputs that are **real measurements**; a *reasoned*
asymmetric error bias (attempt-then-bounce, argued); decomposition **verified**, not
asserted; guaranteed termination with **recorded compromises**; **safe degradation**
without an LLM (a fallback must beat a trivial baseline or report "uncertain →
decompose conservatively / defer" — never a confident keyword guess); faithful,
auditable rationale.

**v2 maturity goal:** closed-loop recalibration from execution/evaluation outcomes —
collides with cold-start; v1 records honestly so v2 can close the loop.

**Budget-aware:** boundary robustness (self-consistency / multiple samples) fires
**only in the mid-confidence band**, per the frugality tension with #1377.

**New property (owner-added) — live-path verification first:** any failure analysis
or fix must establish *what actually executes* before investing. This RFC's whole
re-grounding is that lesson applied.

## Out of scope (deliberately)

- **The how-much-to-invest decision** — the [estimator](./spend-estimator.md) /
  [actuator](./spend-actuator-effort-dial.md) RFCs.
- **A formal convergence proof** — satisfied by consistent caps + recorded
  compromises + a monotone-progress check, not a theorem.
- **Cross-run recalibration** — v2.

## Acceptance criteria

1. `execution/atomicity.py` + `execution/decomposition.py` are removed with **no
   `src/` regression** (a test asserts no non-test importer before removal).
2. An overlapping or scope-dropping split **in `parallel_executor.py`** is caught and
   repaired (one retry), or escalated — never silently accepted.
3. Every forced-atomic compromise appears in the event stream; the live executor's
   split/verdict fields carry **real** inputs (no hardcoded stand-ins).
4. Evaluation bounces are classified (too-big / bad-spec / environment) and only
   *too-big* drives a decomposition, seeded from the bounce trace.
5. With no LLM available, the fallback reports "uncertain → decompose conservatively"
   rather than a confident keyword verdict.
