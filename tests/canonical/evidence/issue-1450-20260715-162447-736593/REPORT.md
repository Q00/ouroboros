# Issue #1450 live paired quality experiment

## Run identity

- Timestamp: 2026-07-15 16:24:47 UTC
- Source tree: `fbf81aec9f68e1a2359f15b0dc0ea4e9412fe617`
- Interview provider/model: `codex` / `default`
- Proposal generator: `LLMAnswerRefiner` with `CodexCliLLMAdapter`
- Sample: 3 paired orders, 6 independent arms (`x/y`, `y/x`, `x/y`)
- Wall time: 393.23 seconds
- Evidence files: 46 committed files after excluding empty lease lock files
- Cost: unavailable. The Codex adapter and experiment evidence did not emit token or billing data, so an exact cost cannot be reported without fabrication.

## Reproduction

```sh
PATH="$PWD/.venv/bin:$PATH" \
OUROBOROS_RUN_AUTO_FILL_QUALITY=1 \
OUROBOROS_AUTO_FILL_QUALITY_EVIDENCE_DIR=/absolute/path/to/issue-1450-evidence \
uv run pytest tests/canonical/test_issue_1450_quality.py \
  -k live_paired_quality_experiment -v -s
```

The preflight passed with no source, fixture, executable, provider, or model configuration errors. The hermetic suite also passed with `12 passed, 1 skipped` before the live opt-in run.

## Inputs and raw outputs

- `experiment-input.json` contains the frozen fixture, both transformed ledgers, hashes, arm mapping, and full proposal manifest.
- `proposal-manifest.json` contains the seven actual Codex-generated treatment proposals.
- `pair-*/arm-*/input.json` contains each arm's exact pre-repair Seed and ledger.
- `pair-*/arm-*/result.json` contains the final persisted state, MCP envelope, smoke record, and evidence classification.
- Baseline trace directories contain raw decisions, questions, flags, lateral output, terminal outcome, and summaries.
- Treatment workdirs preserve the generated `bin/habit`, `package.json`, and product tests when the detached run produced them.

## Result

Verdict: `inconclusive`

All three pair outcomes were `invalid`; no arm reached a complete, state-consistent product oracle with the required trace evidence.

| Pair | Baseline `x` | Treatment `y` |
|---|---|---|
| 1 | `blocked`, Seed QA revise 0.72, missing evidence | `detached`, product not verified complete, missing evidence |
| 2 | `blocked`, Seed QA revise 0.72, missing evidence | `detached`, product not verified complete, missing evidence |
| 3 | `blocked`, Seed QA revise 0.68, missing evidence | `detached`, product not verified complete, missing evidence |

The baseline repeatedly failed Seed QA because its safe-default acceptance criteria did not specify the CLI and persistence contract concretely enough. The treatment produced concrete product files, but its detached execution did not reach a verified terminal state; background logs repeatedly reported `EvolutionaryLoop not configured`, and the required outcome trace was absent.

## Production decision

Do not wire the treatment into production based on this run. The experiment observed neither improvement nor regression because both strategies failed different mandatory gates before a valid paired product comparison existed.

The next experiment should run only after the canonical runner supplies the required EvolutionaryLoop/Ralph dependency and proves terminal trace persistence for detached treatment arms. The baseline Seed QA gap must also be resolved or explicitly classified as the behavior under comparison. This conclusion is limited to the frozen `cli-todo` fixture and must not be generalized into strategy-level equivalence.

## Tracking gap

The experiment contract asks for cost, but no adapter usage or billing fields were emitted anywhere in the evidence bundle. A follow-up should add bounded provider/model usage metadata to the proposal and arm result records before a future cost comparison is claimed.
