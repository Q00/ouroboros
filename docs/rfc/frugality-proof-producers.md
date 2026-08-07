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
can attest complete local and external side-effect isolation. The live
`bounce_only` decomposition path now has a Verified-MECE decision: an independent
runtime attests coverage, sibling non-overlap, and simpler units after at most one
repair. Only that finalized decision carries `trustworthy=true`; forced-atomic,
parse-degraded, repair-exhausted, and other unverified children remain quarantined.

## The fixed event contract (consumed by the #1478 gate)

The gate (`frugality_proof.py`, shipped in #1478) reads these event types and fields.
Producers must emit them keyed by the same `ac_id` the model event uses, and must
carry the **run anchor** (`seed_run_id`, or `execution_id`) plus the orchestration
`session_id` on every event: the proof spans runs and the same logical `ac_id` recurs
each run, while multiple sessions may share one execution id. `assemble_triads()`
therefore keys rows by `(run, session, ac_id)`. An axis event without the run anchor
or session identity is legacy evidence and can be attached only when the matching
run/root has one unambiguous session; otherwise it remains uncounted.

| Event type | Producer | Required fields | Seed AC |
|---|---|---|---|
| `execution.ac.model_routed` | **done** | `model_tier`, `model`, `model_mode`, `is_decomposed_child`, `root_ac_index`, `retry_attempt`, `ac_id`, run anchor, `session_id` | routing contract |
| `execution.ac.token_attribution.reported` | **implemented on this branch** | `ac_id`, run anchor, `session_id`, `session_attempt_id`, primary `ac_dispatch_id`, `root_ac_index`, `retry_attempt`, `token_spend` | AC2 |
| `execution.ac.deliver_verdict` | **implemented on this branch** | `ac_id`, run anchor, `session_id`, `session_attempt_id`, primary `ac_dispatch_id`, `root_ac_index`, `retry_attempt`, `traceguard_verdict`, `unsupported_claim_rate`, `grounding_regression` | AC4 |
| `execution.ac.shadow_replay` | **implemented, fail-closed without an isolation-attested runtime** | `ac_id`, run anchor, `session_id`, `root_ac_index`, `retry_attempt`, `baseline_token_spend`, `baseline_mode`, `baseline_tier`, `baseline_model`, `decomposition_trustworthy` | AC5 |
| `execution.ac.attempt_judged` | **implemented in the outer verify/retry layer** | run anchor, `session_id`, `root_ac_index`, `retry_attempt`, `attempt_number`, `success`, `outcome`, `is_decomposed` | provisional attempt telemetry |
| `execution.ac.acceptance_finalized` | **implemented by the terminal Final Gate** | run anchor, `session_id`, `acceptance_generation_id`, `root_ac_index`, `final_retry_attempt`, `accepted`, `disposition`, `outcome`, `terminal_status` | final admission |

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
`decomposition_trustworthy`. A live `bounce_only` decision may now set the trust
field true after Verified-MECE attestation; every other decomposition remains
untrusted and quarantined. Trust alone does not authorize replay: the runtime must
separately attest side-effect isolation. This is the paired baseline the frugality
bar measures reduction against.

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

Likewise, an LLM-produced string array is not a Verified-MECE proof. The live
`bounce_only` path now requires an exact structured proposal with bounded,
non-empty, unique child scope claims and verification hints. A fresh independent
runtime session must attest collective coverage, sibling non-overlap, and simpler
units; one verifier-guided repair is allowed, after which the decision escalates
and records its compromise. Only that finalized durable decision may carry
`trustworthy=true`. Parsing and replay are deterministic and fail closed; the
semantic attestation does not claim that generated child wording must equal text
predicted by the Seed.

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

## Focused-evolution paired receipt completeness

The evolve-level paired receipt is narrower than the population triad above, but
uses the same fail-closed evidence posture. `execution.ac.attempt.dispatched`
with `dispatch_kind=primary` defines the independently known executor attempt set
for an arm. Its full identity is `(ac_id, retry_attempt, session_attempt_id,
ac_dispatch_id, root_ac_index)`. Each identity must occur exactly once and have
exactly one `execution.ac.token_attribution.reported` event sourced from
`runtime_usage` and exactly one internally consistent
`execution.ac.deliver_verdict`, both bound back to that primary dispatch.
The producer emits one aggregate attribution for a primary attempt and any
session-signal follow-up turns. Until those follow-ups expose a stable cross-arm
semantic chain identity, the presence of any follow-up makes the paired receipt
`INSUFFICIENT_DATA`; silently folding arm-specific follow-up work into a comparable
primary subtotal could fabricate savings. The dispatched root set must exactly
cover `active_ac_indices`.

Executor telemetry is only one component of the generation total. A task-local
collector also records the runtime usage of Wonder, every Reflect parse retry,
validation repair, assertion extraction, semantic/consensus evaluation,
dependency analysis, decomposition classification/proposal/attestation/repair,
coordinator review, and shadow replay. Primary leaf execution remains joined
through its exact durable attempt receipt. An opaque call site or any provider
result without positive runtime usage invalidates the whole arm. Missing usage or
claim surfaces remain honest `INSUFFICIENT_DATA`; duplicate or malformed evidence
is never dropped and the remaining subset is never summed.

The paired generation key hashes the complete canonical Seed and previous
`EvaluationSummary`, plus the shared Git commit. Only volatile Seed identity and
provenance (`seed_id`, `created_at`, `interview_id`, `parent_seed_id`) are removed.
Final quality evidence is independently checked against each arm's current Seed:
every AC index must appear once, in range, with matching content and structured
semantic identity. Final approval, overall score, highest evaluation stage,
drift, reward-hacking risk, per-AC scores, lineage regressions, and TraceGuard
grounding are compared with lower-is-better direction where applicable. A
metric present in only one arm is incomparable. The two arms must additionally
have the same complete semantic fingerprint of the Seed actually evaluated;
equal cardinality is not enough.
These completeness facts and bounded digests of the executor and provider call
identities are persisted in the observation before a PASS can be emitted.
Separate normalized configuration fingerprints require and bind backend,
model/tier/mode, effort level/mode, permission mode, and applicable request
settings. Completion surfaces resolve the task profile once and dispatch the
resulting sealed config. They require a versioned adapter-instance attestation
validated against a centrally registered exact-key schema. The receipt binds the
effective model, profile, sampling/output/effort fields, endpoint/routing,
timeout, and retry field, while credential authority is persisted only as a
deterministic HMAC. The prepared endpoint and credential remain secret-safe,
in-memory dispatch authority and must be consumed unchanged at the actual
provider-call boundary; dependency-global or post-attestation mutable routing is
ineligible. The schema must mechanically establish one measured attempt;
a self-declared retry-completeness boolean is insufficient. Unregistered,
unattested, empty, partial, extra, secret-bearing, cyclic, over-deep, oversized,
non-finite, retry-opaque, blank, or unknown configuration invalidates only the
proof arm and never the evolution call.
Assignment digests additionally preserve
the root-AC-to-configuration and auxiliary-role-to-configuration call sequences
across the pair, including order and multiplicity. A root or role realized in both
arms must have an exactly equal sequence. The proof may remove an entire
control-only root/role, but fails closed on partial removal within a shared unit
until producers expose stable cross-arm semantic call identities. Missing, blank,
or unknown phase roles are incomplete evidence. Equality of an unordered value
set—or an identity-free subsequence—is insufficient because swapping or
concentrating a cheap model could otherwise masquerade as a focused-evolution
saving.
Agent-runtime request envelopes also bind the tool set, system-prompt digest,
known request kwargs, and whether the call is fresh or uses a scoped fresh
handle. A true resumed transcript/session is ineligible until it exposes a
stable semantic cross-arm context identity. Provider response model identity is
normalized and revalidated for both completion and agent-stream surfaces, so a
requested known model cannot be replaced by an opaque `unknown` or a different
effective model without changing the assignment digest. Completion usage
subtotals must reconcile exactly with the reported total.
Proof-event reads use bounded keyset pages and provider-call capture has its own
fixed cap. Either overflow fails incomplete while retaining only bounded metadata.
Individual receipts, primary/provider subtotals, and their combined generation
total must remain finite. A Wonder-only
early exit persists a non-PASS receipt.

## Done = safe, fully measured runs stop returning INSUFFICIENT_DATA

The producer wiring is complete on this branch. A real PASS/FAIL additionally
requires both a deterministic decomposition-trust validator, a runtime that
satisfies the full replay-isolation contract, and enough fully measured runs. Until
then, the correct production verdict remains `INSUFFICIENT_DATA`; tests can exercise
the complete triad with an explicitly attested, side-effect-free runtime double.
