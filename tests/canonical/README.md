# Canonical acceptance scenarios

Minimal manual test harness for `ooo auto` per the L0 design slice of
[#1157](https://github.com/Q00/ouroboros/issues/1157) and
[#1170](https://github.com/Q00/ouroboros/issues/1170).

## What this is

A directory of self-contained scenarios that the maintainer runs
**manually** when assessing whether `ooo auto`'s SSOT acceptance gate
holds. There is intentionally **no CI obligation**, no replay layer,
and no scheduled execution.

## What it is NOT

- Not a continuous regression engine.
- Not a nightly CI workflow.
- Not a recorded-replay system.
- Not a cost-budgeted live runner.

If any of those becomes valuable later (evidence-driven follow-up
issue required), it gets added then — not pre-built. See #1170
*Self-audit note* for the rationale.

## How to use

### Quick shape-check (always runs in CI, no LLM cost)

```sh
uv run pytest tests/canonical/ -v
```

This validates that every scenario directory has the required
fixture files in the right shape. It does **not** invoke
`ouroboros_auto`. Use this to catch fixture rot.

### Full live run (manual, costs LLM tokens)

```sh
OUROBOROS_RUN_CANONICAL=1 uv run pytest tests/canonical/ -v
```

Once the live wiring lands in L0-b, this command will invoke the
`ouroboros_auto` MCP tool against each scenario and assert the
documented terminal state — **use sparingly**, each scenario will
consume real LLM tokens (cli-todo ≈ \$1, kart-racer ≈ \$5 with
Sonnet-class models). **At L0-a (this PR) the opt-in still
`pytest.skip`s with a typed reason** so the harness contract is
observable without burning tokens; the shape-check tests still run.

### Run a single scenario

All canonical tests live in `tests/canonical/test_canonical.py` and
are parametrized per discovered scenario directory. Filter by slug
with `-k`:

```sh
uv run pytest tests/canonical/ -v -k cli-todo
```

Add `OUROBOROS_RUN_CANONICAL=1` once L0-b lands to opt into the live
invocation for that scenario.

## Scenario directory shape

Each `tests/canonical/<slug>/` directory contains:

| File | Purpose |
|---|---|
| `goal.txt` | One-line goal string fed to `ooo auto`. No leading/trailing whitespace beyond a final newline. |
| `expected.yaml` | Frozen metadata: `domain_class`, `completion_mode`, `runtime_probe_kinds`, optional `wall_clock_budget_seconds`. |
| `env/` *(optional)* | Fixture files seeded into the temp workdir before `ouroboros_auto` is invoked. Often empty for greenfield scenarios. |

`expected.yaml` schema (validated by `conftest.py`):

```yaml
# required
domain_class: cli                    # one of the L1 TaskClass values
completion_mode: product_complete    # CODE_COMPLETE | PRODUCT_COMPLETE

# optional
runtime_probe_kinds:                 # placeholder until L3 lands
  - headless_run
  - stdout_golden
wall_clock_budget_seconds: 600       # default: 7200
```

## When to extend

When a fifth scenario class (e.g. `desktop-app`) emerges as worth
canonicalizing, add a new `<slug>/` directory + populate
`expected.yaml`. No infrastructure change required. The runner
auto-discovers.

## Adding the live-run path

The hermetic shape-check is in place from L0-a. The live-run path
(`OUROBOROS_RUN_CANONICAL=1`) is wired but the actual
`ouroboros_auto` invocation lands in L0-b once the maintainer has
confirmed they want it; until then the live-run path skips with a
typed reason so the harness contract stays observable.
