# RFC — Frugality-proof producers: wiring the token, grounding, and baseline axes

> Status: **Draft**
> Epic: [#1465](https://github.com/Q00/ouroboros/issues/1465) ("Frugality you can prove") · Task [#1470](https://github.com/Q00/ouroboros/issues/1470)
> Depends on: the effort contract + deterministic proof gate (PR stack #1473–#1478).

## Why

The deterministic frugality-proof machine (`orchestrator/frugality_proof.py`) is
delivered by the proof-gate PR (#1478) this RFC depends on — it is **not yet on
`main`**; it lands with the #1473–#1478 stack below. There, `assemble_triads()` joins
per-AC events into a `FrugalityTriadRow`, and `evaluate_proof()` computes the seed's
PASS/FAIL gate — **grounding regression is a per-AC veto**, then sample sufficiency
(≥20 triads / ≥3 runs), then aggregate token reduction (≥10%). The **effort axis** is
produced by the same stack (`execution.ac.effort_routed`, `mode=enforced` on
Claude/Codex/Copilot).

But a row only `counts_in_proof` when it carries **all** axes, and three are not yet
emitted. Until they are, the gate honestly returns `INSUFFICIENT_DATA`. This RFC
specifies the three remaining producers against the event-type contract that gate
reads — so they have a precise, testable target and the gate stays unchanged.

## The fixed event contract (consumed by the #1478 gate)

The gate (`frugality_proof.py`, shipped in #1478) reads these event types and fields.
Producers must emit them keyed by the same `ac_id` the effort event uses, and must
carry the **run anchor** (`seed_run_id`, or `execution_id`) on every event: the proof
spans runs and the same logical `ac_id` recurs each run, so `assemble_triads()` keys
rows by `(run, ac_id)`. An axis event without the run anchor cannot be attributed to
the right run's row.

| Event type | Producer | Required fields | Seed AC |
|---|---|---|---|
| `execution.ac.effort_routed` | **done** | `effort_level`, `effort_mode`, `is_decomposed_child`, `ac_id`, `seed_run_id` | (effort contract) |
| `execution.ac.token_attribution.reported` | **#1 below** | `ac_id`, `seed_run_id`, `token_spend` | AC2 |
| `execution.ac.deliver_verdict` | **#2 below** | `ac_id`, `seed_run_id`, `traceguard_verdict`, `unsupported_claim_rate`, `grounding_regression` | AC4 |
| `execution.ac.shadow_replay` | **#3 below** | `ac_id`, `seed_run_id`, `baseline_token_spend`, `baseline_mode`, `decomposition_trustworthy` | AC5 |

## Producer #1 — Per-AC token attribution (AC2)

Emit `execution.ac.token_attribution.reported` carrying the **real** token count an
AC consumed, from the runtime's usage signals (not estimated from text length). On
runtimes that surface no usage counters, emit `token_spend: null` honestly rather
than fabricating — such rows simply will not count toward the proof.

Acceptance: a re-done AC reports a higher spend than a clean one; a clean first-try
AC is never inflated; a test asserts no value is a hardcoded placeholder.

## Producer #2 — TraceGuard deliver verdict (AC4)

For each AC, run the deliver claim through the deterministic TraceGuard validator
(`harness/deliver_gate.py`, #978) against the evidence manifest, and emit
`execution.ac.deliver_verdict` with the accepted/rejected `traceguard_verdict`, the
`unsupported_claim_rate`, and `grounding_regression` — **true** iff the lowered-effort
run produced any newly-rejected claim versus its parent-effort baseline.
`fat_harness` ON is the grounding precondition; under OFF, no verdict is emitted (so
those rows do not count). This is the axis the per-AC grounding veto reads.

Acceptance: identical inputs yield identical verdicts (deterministic, no noise band);
a newly-rejected claim at lower effort surfaces `grounding_regression: true`.

## Producer #3 — Shadow-replay paired baseline (AC5)

In an **experiment-harness path only** (never production steady-state), re-execute
each enforced decomposed-child AC once at its **parent** effort and emit
`execution.ac.shadow_replay` with `baseline_token_spend`, `baseline_mode:
"shadow_replay"`, and `decomposition_trustworthy` (false for forced-atomic /
MECE-repair-failed units, which are quarantined out of the proof). This is the
paired baseline the frugality bar measures reduction against.

Acceptance: cost bounded to the experiment set (~2× on those rows only); forced-atomic
units are recorded but excluded; the triad pairs each child's lowered-effort run with
its parent-effort baseline.

## Out of scope

- Any change to `evaluate_proof()` / the gate — the thresholds and order are fixed.
- The effort contract (shipped in the #1473–#1478 stack).

## Done = the gate stops returning INSUFFICIENT_DATA

Once all three producers emit their events for an enforced run, `assemble_triads()`
yields fully-measured rows and `evaluate_proof()` returns a real PASS/FAIL —
the complete enforced-triad proof the seed set out to produce.
