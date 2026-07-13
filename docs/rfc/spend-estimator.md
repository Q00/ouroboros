# RFC - AC investment assessment: difficulty + stakes with explicit authority

> Status: **Implemented (v1 policy, 2026-07-13)**
> Relates to [issue #1398](https://github.com/Q00/ouroboros/issues/1398) and
> [discussion #1384](https://github.com/Q00/ouroboros/discussions/1384).
> The execution-side policy is documented in the
> [spend actuator RFC](spend-actuator-effort-dial.md).

## Summary

Ouroboros does not infer that work is cheap from acceptance-criterion text,
token length, tool count, artifact count, verification-command shape, or keyword
scoring. Those signals do not establish difficulty or cost-of-being-wrong and
cannot safely authorize lower investment.

The v1 contract is an optional AC-level `investment` declaration. It records two
axes, provenance, and confidence. The orchestrator normalizes that declaration to
an auditable `InvestmentAssessment`; missing or weak authority fails closed and
cannot make execution cheaper.

## Current-main disposition

- The legacy PAL/complexity estimator and its off-path router were removed.
- Reasoning-effort capability negotiation and per-call enforcement are live.
- Model-tier routing is a separate live actuator used by the frugality proof.
- Child model lowering is governed by decomposition trust, not by investment
  metadata or child status alone.
- Cross-run calibration remains deferred until qualified evidence exists.

## Input contract

`AcceptanceCriterionSpec` accepts this additive field:

```yaml
acceptance_criteria:
  - description: Rotate production payment signing keys
    investment:
      difficulty: medium
      stakes: high
      provenance: declared
      confidence: high
```

The v1 vocabulary is intentionally bounded:

| Field | Values | Meaning |
| --- | --- | --- |
| `difficulty` | `low`, `medium`, `high`, or omitted | Work complexity independent of statement length |
| `stakes` | `low`, `medium`, `high`, or omitted | Cost of being wrong, including reversibility and blast radius |
| `provenance` | `declared`, `measured`, `inferred`, `absent` | Where the assessment authority came from |
| `confidence` | `low`, `medium`, `high` | Confidence in the supplied assessment; defaults to `low` |

Legacy string ACs and structured ACs without `investment` remain valid and keep
their persisted representation unchanged.

`declared` means an authorized Seed/profile producer chose the value. `measured`
means an upstream producer asserts that the value came from concrete repository
or runtime evidence; that producer must retain the evidence reference and must
not relabel inference as measurement. In v1, both are explicit authority.
`inferred` and `absent` are never lowering authority.

## Normalized assessment

Every execution leaf receives an `InvestmentAssessment` containing:

- normalized `difficulty` and `stakes` (`unknown` when absent);
- `provenance` and `confidence`;
- the signals used and the signals missing;
- `can_cheapen`;
- any minimum required effort;
- a deterministic rationale derived from those exact inputs.

The assessment is emitted as `execution.ac.investment_assessed`. When effort is
routed, the same assessment payload is embedded in
`execution.ac.effort_routed`, preventing telemetry from drifting away from the
values that made the decision.

## Policy invariants

The base effort is the per-run `reasoning_effort` configured by the runner. The
shared effort ladder is `minimal -> low -> medium -> high -> xhigh`; the one-notch
discount has a `low` floor, while an explicitly configured `minimal` base remains
unchanged. Policy order is: configured base, assessment floor/discount, then
retry escalation. A runtime that cannot enforce the resulting level records it
as advised and receives no unsupported effort parameter.

1. Missing difficulty or stakes becomes `unknown` and cannot authorize lower
   effort.
2. `inferred` and `absent` provenance are observe-only or raise-only. They never
   authorize cheapening.
3. Only complete `low` difficulty + `low` stakes with `declared` or `measured`
   provenance and `high` confidence may lower configured effort, by exactly one
   notch.
4. Any `high` axis imposes a minimum effort of `high` when a base effort is
   configured.
5. Retry escalation is applied after the assessment. Runtime failures may raise
   later attempts, never retroactively justify a cheaper first attempt.
6. When no base effort is configured, effort routing remains dormant; the
   assessment is still recorded for observability.
7. Decomposed children inherit the parent AC's investment metadata. They do not
   inherit the parent's success contract unless a future decomposition protocol
   produces a child-specific contract.

## Separation from model-tier trust

Investment assessment does not authorize a cheaper model. A decomposed child may
drop one model tier only when `decomposition_trustworthy=True` is supplied by an
explicit deterministic trust issuer. Current upstream `main` has no live issuer,
so live decomposed children remain at the base tier. Retry escalation, explicit
model pins, and the routing kill switch retain precedence. Trust production and
verified-MECE decomposition remain tracked by issue #1466.

## Out of scope

- Natural-language difficulty or stakes scoring.
- Token, tool, artifact, command, file, or keyword counts as lowering authority.
- Cross-run learning or calibration.
- Shadow-replay enablement or frugality-proof redesign.
- New runtime/provider support.

## Acceptance criteria

1. Legacy AC serialization is stable and `investment` is additive.
2. Missing, inferred, or low-confidence inputs cannot lower effort.
3. A high-stakes short AC cannot run below the configured safety floor.
4. An authorized low/low assessment lowers effort by at most one notch.
5. Retry escalation composes after assessment and only raises later attempts.
6. Direct, parallel, recursive-child, and resume paths use the same fail-safe
   default.
7. Events contain the exact inputs and rationale used by policy resolution.
