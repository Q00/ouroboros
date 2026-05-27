<!-- Thanks for contributing to Ouroboros! Fill in the sections below. -->

## Summary

<!-- 1-3 sentences. What does this PR do and why? -->

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
