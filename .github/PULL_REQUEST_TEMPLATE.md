<!-- Thanks for contributing to Ouroboros! Fill in the sections below. -->

## Summary

<!-- 1-3 sentences. What does this PR do and why? -->

## Review boundary

<!-- Keep this boundary stable while responding to review. See CONTRIBUTING.md#review-boundary-contract and CONTRIBUTING.md#five-question-review-rubric. -->
- **User problem**: What one concrete user problem does this PR solve?
- **Inputs and execution conditions**: Which inputs, preconditions, environments, and execution paths are supported?
- **Promised contract**: What observable behavior and invariants does the PR guarantee?
- **Implementation boundary and owner**: Which existing subsystem/component changes? Which data or security boundaries are crossed? Who owns it?
- **Non-goals**: Which inputs, conditions, risks, or adjacent capabilities are intentionally excluded?
- **Evidence**: Which reproduction steps or tests prove the promised behavior?

<!-- Review findings are blockers only when they reproduce inside this boundary and violate the contract, or when they leave an immediate user-data/security risk. New subsystem/ownership needs go to a maintainer; safe out-of-boundary risks require a named follow-up owner. -->

## Test plan

<!-- Bulleted checklist of how this PR was verified. -->
- [ ] `uv run pytest tests/ -q` passes
- [ ] `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/` clean
- [ ] `uv run mypy src/` clean (or noted exception)

## R-run comparison (required for `src/ouroboros/auto/` changes)

<!--
RFC #1256 §I5 requires every PR that touches `src/ouroboros/auto/` to include
a per-round wall-clock comparison against the latest canonical baseline.
This guards against silent performance regressions like the ~3× per-round
slowdown observed in the PR-A/B/β/γ merge train (#1258).

If your PR does NOT touch `src/ouroboros/auto/`, delete this whole section.

If it does, fill in the table below. Every metric row must have ALL
three comparison cells (Baseline, This PR, Ratio) populated — partially
filled rows (e.g. only `Baseline=TBD` with blank PR/Ratio) are rejected
by the gate. For substrate-only PRs that genuinely have no per-round
comparison, fill every cell explicitly (`N/A | N/A | n/a`) so the
intent is auditable; one filled cell with two blanks is treated as
"author skipped the requirement".

A baseline R-run is at `~/.ooo-observability/` or in #1258 evidence;
capture a fresh run on your branch with:

    OUROBOROS_RUN_CANONICAL=1 uv run pytest tests/canonical/ -k cli-todo -v

The §I5 budget is 1.5× the latest baseline per-round cost. Greater
regressions require a separate performance budget RFC.
-->

| Metric                         | Baseline (sha)        | This PR (sha)         | Ratio    |
|--------------------------------|-----------------------|-----------------------|----------|
| Rounds completed in 600 s      |                       |                       |          |
| Per-round wall-clock (s/round) |                       |                       |          |
| Terminal reason                |                       |                       |          |
| EventStore event count         |                       |                       |          |

Budget compliance: [ ] within 1.5× / [ ] regression flagged with mitigation /
[ ] N/A (PR does not touch `src/ouroboros/auto/`)

## Related issues

<!-- Link issues this PR closes or references, e.g. "Fixes #1234", "Refs #1256". -->
