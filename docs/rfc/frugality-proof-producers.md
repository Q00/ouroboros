# RFC — Frugality-proof producers: wiring the token, grounding, and baseline axes

> Status: **Draft**
> Epic: [#1465](https://github.com/Q00/ouroboros/issues/1465) ("Frugality you can prove") · Task [#1470](https://github.com/Q00/ouroboros/issues/1470)
> Depends on: the effort contract + deterministic proof gate (PR stack #1473–#1478).

## Why

The deterministic frugality-proof machine (`orchestrator/frugality_proof.py`) was
delivered by proof-gate PR #1478 and is already present on `main`.
`assemble_triads()` joins
per-AC events into a `FrugalityTriadRow`, and `evaluate_proof()` computes the seed's
PASS/FAIL gate — **grounding regression is a per-AC veto**, then sample sufficiency
(≥20 triads / ≥3 runs), then aggregate token reduction (≥10%). This branch makes
the live frugality actuator the model-tier router: a child counts only when
`execution.ac.model_routed` proves a native enforced tier strictly below its
shadow-replay baseline tier. Reasoning-effort telemetry remains useful audit
metadata but is not the admission gate because shipped runs may legitimately use
`reasoning_effort: null`.

But a row only `counts_in_proof` when it carries **all** axes. This branch implements
the three previously-missing producers plus authoritative outcome finalization.
Missing, unsafe, or malformed measurements still return `INSUFFICIENT_DATA`; in
particular, shadow replay is unavailable on bundled production runtimes until one
can attest complete local and external side-effect isolation. Runtime decomposition
currently validates only response shape/count, not semantic MECE coverage and
exclusivity, so live children also carry no decomposition-trust attestation and
cannot enter a proof row yet.

## The fixed event contract (consumed by the #1478 gate)

The gate (`frugality_proof.py`, shipped in #1478) reads these event types and fields.
Producers must emit them keyed by the same `ac_id` the model event uses, and must
carry the **run anchor** (`seed_run_id`, or `execution_id`) on every event: the proof
spans runs and the same logical `ac_id` recurs each run, so `assemble_triads()` keys
rows by `(run, ac_id)`. An axis event without the run anchor cannot be attributed to
the right run's row.

| Event type | Producer | Required fields | Seed AC |
|---|---|---|---|
| `execution.ac.model_routed` | **done** | `model_tier`, `model`, `model_mode`, `is_decomposed_child`, `root_ac_index`, `retry_attempt`, `ac_id`, run anchor | routing contract |
| `execution.ac.token_attribution.reported` | **implemented on this branch** | `ac_id`, run anchor, `root_ac_index`, `retry_attempt`, `token_spend` | AC2 |
| `execution.ac.deliver_verdict` | **implemented on this branch** | `ac_id`, run anchor, `root_ac_index`, `retry_attempt`, `traceguard_verdict`, `unsupported_claim_rate`, `grounding_regression` | AC4 |
| `execution.ac.shadow_replay` | **implemented, fail-closed without an isolation-attested runtime** | `ac_id`, run anchor, `root_ac_index`, `retry_attempt`, `baseline_token_spend`, `baseline_mode`, `baseline_tier`, `baseline_model`, `decomposition_trustworthy` | AC5 |
| `execution.ac.attempt_judged` | **implemented in the outer verify/retry layer** | run anchor, `root_ac_index`, `retry_attempt`, `attempt_number`, `success`, `outcome`, `is_decomposed` | provisional attempt telemetry |
| `execution.ac.acceptance_finalized` | **implemented by the terminal Final Gate** | run anchor, `acceptance_generation_id`, `root_ac_index`, `final_retry_attempt`, `accepted`, `disposition`, `outcome`, `terminal_status` | final admission |

`execution.ac.outcome_finalized` remains readable as a historical alias for
attempt telemetry. It is not a final-admission signal.

All retry attempts for a logical child are paired before aggregation. Token spend
and baseline spend are summed attempt-for-attempt, while grounding regression is an
OR veto. A token-bearing attempt missing any model/deliver/shadow partner excludes
the row rather than undercounting it. Leaf events are provisional until the latest
root attempt has exactly one successful, strictly decomposed outcome marker and the
child actually participated in that attempt. A later `verify_command`, expected-
artifact failure, atomic retry, duplicate/conflicting marker, or stale child cannot
contribute a proof row. Missing `retry_attempt` and duplicate per-axis events are
malformed telemetry and fail closed rather than defaulting to attempt zero or
inflating one side of the comparison.

## Producer #1 — Per-AC token attribution (AC2)

Emit `execution.ac.token_attribution.reported` carrying the **real** token count an
AC consumed, from the runtime's usage signals (not estimated from text length). On
runtimes that surface no usage counters, emit `token_spend: null` honestly rather
than fabricating — such rows simply will not count toward the proof.

Resolve usage per runtime message before summing messages and retry events. A valid
`total_tokens` is authoritative for its message; otherwise add `input_tokens`,
`output_tokens`, and Anthropic's additive `cache_creation_input_tokens` /
`cache_read_input_tokens`. Keep OpenAI's `cached_input_tokens` in the diagnostic
breakdown only: it is already a subset of `input_tokens`, so adding it again would
double-count. Token telemetry is all-or-nothing per leaf/attempt: a non-mapping
usage payload, or any present recognized counter that is non-numeric, negative,
non-finite, or overflowing, invalidates the whole attribution. An invalid present
`total_tokens` never falls back to smaller components. This fails closed against
undercounting rather than turning partial telemetry into a synthetic saving.

Acceptance: a re-done AC reports a higher spend than a clean one; a clean first-try
AC is never inflated; a test asserts no value is a hardcoded placeholder.

## Producer #2 — TraceGuard deliver verdict (AC4)

For each AC, run the deliver claim through the deterministic TraceGuard validator
(`harness/deliver_gate.py`, #978) against the evidence manifest, and emit
`execution.ac.deliver_verdict` with the accepted/rejected `traceguard_verdict`, the
`unsupported_claim_rate`, and `grounding_regression` — **true** iff the lower-tier
run produced any newly-rejected claim versus its parent-tier baseline.
`fat_harness` ON is the grounding precondition; under OFF, no verdict is emitted (so
those rows do not count). This is the axis the per-AC grounding veto reads.

The minimal shadow-replay harness does not record a second journal, so its exact
parent-tier deliver verdict is unavailable. Live runs therefore use a named,
fail-closed policy (`grounding_regression_mode=fail_closed_live_traceguard`): a
journal-grounded accepted child records `false`; any rejected child records `true`
because the system cannot prove the parent would also reject it. This may produce a
conservative FAIL, but can never manufacture a PASS. The live TraceGuard adapter also
binds canonical fact/chunk ids to journal-generated evidence handles and requires
structured `key=value` claim terms to match journal text; a claim-provided fact id is
diagnostic only and cannot populate its own evidence manifest.

For the default code profile, the live bridge derives claims from
`files_touched`, `commands_run`, and `tests_passed` before considering any
self-authored `observed_facts`. It admits `execution.tool.started` only for the
exact accepted retry/session attempt after the leaf and harness verifier pass.
An Edit/Write/NotebookEdit start additionally needs one unambiguous, correlated,
explicitly successful completion (or an explicit self-contained completion
status). Missing/failed results, malformed `is_error` data, duplicate starts,
duplicate or contradictory completions, and call-id mismatches are rejected.
Paths must be workspace-relative and contained; commands must match exactly;
`tests_passed` must also be an exact member of `commands_run`. Missing or multiple
matches are rejected, never guessed.
Test-pass evidence additionally requires a non-failed, correlated Bash completion
and runtime-produced proof text; assistant narration alone cannot name or bless a
test node-id.

An `accepted` TraceGuard verdict is internally consistent only when
`unsupported_claim_rate == 0` and `grounding_regression == false`. Contradictory
payloads are excluded instead of allowing a nominally accepted row to hide
unsupported claims.

Acceptance: identical inputs yield identical verdicts (deterministic, no noise band);
a newly-rejected claim at the lower tier surfaces `grounding_regression: true`.

At run end the consumer evaluates a bounded cohort of recent executions with the
same fail-closed experiment identity, resolved from `orchestrator.session.started`:
`seed_id`, executable-Seed fingerprint, canonical project/workspace, proof protocol
version, and the resolved routing fingerprint (including the runtime constructor
model pin). Legacy or malformed starts stay current-run-only. It never combines
unrelated workloads merely to satisfy the `>=3 runs` threshold; fewer attributable
runs remain `INSUFFICIENT_SAMPLE`.

## Producer #3 — Shadow-replay paired baseline (AC5)

In an **experiment-harness path only** (never production steady-state), a child
with deterministic decomposition-trust attestation and an isolation-attested
replay runtime is eligible for one re-execution at its **parent** model tier/effort.
That run emits `execution.ac.shadow_replay` with `baseline_token_spend`,
`baseline_mode: "shadow_replay"`, `baseline_tier`, `baseline_model`, and
`decomposition_trustworthy`. The field is currently false for every live
decomposition; only an explicitly attested test or future experiment producer may
set it true. Untrusted units are quarantined out of the proof. This is the paired
baseline the frugality bar measures reduction against.

The child and baseline must also resolve to different concrete model IDs. A sparse
tier configuration may label a child `frugal` while falling upward to the same
standard model used by the baseline; tier labels alone must not manufacture a
reduction that never happened.

Usage is accepted only after the throwaway runtime emits one unambiguous successful
terminal result, profile-valid typed evidence, and a transcript-verifier PASS bound
to the isolated snapshot cwd. Missing dependencies/Git metadata, semantic failure,
terminal errors, contradictory outcomes, unsupported evidence, or usage-less runs
emit no baseline. This prevents a failed troubleshooting replay from inflating the
denominator and manufacturing a PASS.

A copied cwd is not itself a security boundary. Before execution, the throwaway
runtime must explicitly attest both (1) strict read/write confinement to the
supplied snapshot and (2) disabled or isolated network, MCP, API, deployment,
messaging, DB, and other external side-effect paths. No bundled production runtime
currently makes both attestations, so Claude/Codex/Gemini/etc. skip replay and emit
no baseline today. This is intentional fail-closed behavior, not a claim that
copytree or a normal workspace-write sandbox is sufficient.

Likewise, an LLM-produced string array is not a deterministic MECE proof. The live
decomposer currently checks JSON type and child count only; until a validator can
attest non-empty uniqueness plus semantic coverage/exclusivity, production
decompositions are marked untrustworthy and shadow replay is skipped before any
extra model call. Test doubles may supply an explicit trusted attestation to verify
the downstream event/proof machinery, but that does not certify the live producer.

Host-side `verify_command` execution is also unsupported in shadow mode: such a
command could name an absolute live path or escape with `cd ../..` outside the
runtime sandbox. ACs carrying `verify_command` therefore emit no baseline until an
independently sandboxed verify runner exists. Expected-artifact-only checks remain
path-contained and may be evaluated without spawning a shell.

The replay resolves the parent model/effort with the same execution-profile tier
hint and retry index used by the live child. Otherwise a profile-pinned frugal
parent could be replayed at the router's standard default, inventing a lowering
that the live route never made.

Acceptance: no baseline model call occurs without both trust and isolation
attestations. Once an eligible experiment runtime exists, cost is bounded to the
experiment set (~2× on those rows only); untrusted units remain excluded, and the
triad pairs each child's lower-tier run with its parent-tier baseline.

## Out of scope

- The proof thresholds and verdict order remain fixed.
- Reasoning-effort routing remains an auxiliary, independently observable contract.

## Done = safe, fully measured runs stop returning INSUFFICIENT_DATA

The producer wiring is complete on this branch. A real PASS/FAIL additionally
requires both a deterministic decomposition-trust validator, a runtime that
satisfies the full replay-isolation contract, and enough fully measured runs. Until
then, the correct production verdict remains `INSUFFICIENT_DATA`; tests can exercise
the complete triad with an explicitly attested, side-effect-free runtime double.
