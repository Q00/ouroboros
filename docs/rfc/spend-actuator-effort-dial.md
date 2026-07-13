# RFC - The spend actuator: assessment-controlled effort and trust-controlled models

> Status: **Implemented (assessment/effort v1; fail-closed model trust gate)**
> Relates to [issue #1398](https://github.com/Q00/ouroboros/issues/1398) and
> [discussion #1384](https://github.com/Q00/ouroboros/discussions/1384). The input
> authority contract is the [investment assessment RFC](spend-estimator.md).

## Summary

Execution uses two separate controls that must not be conflated:

- AC investment assessment acts on **reasoning effort**.
- Verified decomposition trust acts on **model tier**.

Difficulty/stakes metadata cannot authorize a cheaper model. Likewise, being a
decomposed child cannot lower reasoning effort. Uncertainty preserves or raises
investment; it never cheapens execution.

## Current live path

The live path is `orchestrator/parallel_executor.py`, with direct and resume calls
routed by `orchestrator/runner.py`. The old PAL/complexity router and off-path tier
machinery were already removed. The current model-tier router is live and remains
part of the runtime/proof surface.

The executor emits three related events:

- `execution.ac.investment_assessed` records exact assessment inputs and policy
  authority.
- `execution.ac.effort_routed` records the resulting enforced or advised effort
  and embeds the exact assessment used.
- `execution.ac.model_routed` records model resolution plus decomposition-trust
  authorization.

## Assessment-controlled effort

Only complete low/low, high-confidence, declared or measured inputs may lower the
configured base effort by one notch. Missing, inferred, or low-confidence inputs
preserve the base. Any high difficulty/stakes axis imposes at least high effort.
Retry escalation is applied afterward.

The policy is dormant when no base effort is configured. Assessment telemetry is
still emitted so the absence of an actuator is visible rather than inferred.

## Trust-controlled model tier

`is_decomposed_child` is classification, not lowering authority. A child drops one
model tier only when `decomposition_trustworthy` is exactly `True`. The default is
`False` for all existing callers.

Current upstream `main` has no live trusted decomposition issuer. Therefore live
children remain at the base tier while the pure routing API and telemetry contract
are ready for a future verified-MECE producer under issue #1466.

Retry escalation remains progressive and is applied after any authorized child
discount. Explicit model pins and `OUROBOROS_MODEL_TIER_ROUTING=off` remain higher
precedence controls.

## Policy precedence

For reasoning effort:

```text
configured base -> assessment floor/discount -> retry raise -> capability enforcement
```

For model tier:

```text
configured base/pin -> explicit decomposition trust -> retry escalation -> capability enforcement
```

The controls do not cross-authorize each other. Assessment cannot lower model
tier, and decomposition trust cannot lower effort.

## Capability and fallback behavior

Each runtime declares whether it can enforce per-call effort and model overrides.
An enforced decision is passed to `execute_task`. An advised decision is recorded
but no unsupported keyword argument is passed. A dormant decision emits no routed
event for that actuator.

This distinction is load-bearing: telemetry must describe what the runtime can
actually honor, not what the orchestrator wished it would honor.

## Recursive and alternate execution

The parent AC's investment metadata follows recursive children and alternate
harness reruns because those units jointly discharge the same investment risk.
The parent success contract does not follow child prompts; children receive a
success contract only when a future decomposition producer creates one explicitly.

Alternate reruns preserve the same investment metadata and fail-closed trust
state. Retry/failure signals may raise later attempts but do not rewrite the
first-attempt assessment.

## Out of scope

- Automatic natural-language complexity scoring.
- Count-based lowering proxies.
- A live decomposition trust issuer or verified-MECE partition protocol.
- Cross-run calibration.
- Shadow replay or proof-admission changes.
- New backend capability support.

## Acceptance criteria

1. A live call site applies investment assessment to reasoning effort and records
   the exact assessment used.
2. Missing or weak authority cannot lower effort; a high axis cannot resolve below
   the configured high floor.
3. Child model lowering requires explicit decomposition trust; child status alone
   keeps the base tier.
4. Retry escalation may raise effort and model tier but never lowers a later
   attempt.
5. Direct, parallel, recursive, retry, and alternate-rerun paths preserve the same
   authority and fail-closed defaults.
6. Native enforcement and advised fallback are recorded truthfully.
