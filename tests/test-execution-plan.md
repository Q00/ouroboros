# Test Execution Plan

**Last updated:** 2026-04-16
**Collection basis:** `uv run pytest tests --collect-only -q` collected `4529` tests.

## Goal

This plan orders test execution to maximize signal per minute and minimize AI-agent context waste:

- fail fast on import, packaging, and foundation breakage
- validate low-level invariants before broad orchestration flows
- defer expensive happy-path confirmation until prerequisite unit layers are green
- avoid running suites whose likely failure cause is already known from an upstream phase

## Important Constraint

No suite in this repository strictly guarantees another suite.

- A passing lower-level suite does **not** prove a higher-level suite will pass.
- A passing higher-level suite usually proves only a narrow happy path and does **not** replace the lower-level regression suites.
- The value of this ordering is not mathematical proof. It is **failure localization** and **avoiding redundant downstream runs** after a prerequisite layer is already broken.

## Practical Dependency Chain

Use this as the mental model for deciding whether a downstream suite is worth running:

`config/core/persistence/routing/providers`
-> `execution/evaluation/plugin/mcp/observability/resilience`
-> `orchestrator/bigbang/cli`
-> `integration`
-> `e2e`

The farther right a suite is, the more modules it depends on, and the less useful it is after a failure on the left.

## Default Repo-Wide Order

Run the phases in order. Stop when a phase fails unless the failing phase is unrelated to the change under test.

### Phase 0: Collection Gate

**Command**

```bash
uv run pytest tests --collect-only -q
```

**Why first**

- catches import errors, broken fixtures, bad module moves, and collection regressions
- confirms the expected test surface before spending context on execution

**Stop rule**

- if collection fails, do not run any other tests

### Phase 1: Global Unit Smoke

**Command**

```bash
uv run pytest tests/unit/test_*.py -q
```

**Approximate size:** 205 tests

**Why here**

- these top-level tests are broad sanity checks for entry points, project initialization, regression guards, rewind/evolve flows, and repository wiring
- they give cheap cross-cutting signal before the larger directory suites

**Unlocks**

- all later unit phases

### Phase 2: Foundation Units

**Command**

```bash
uv run pytest \
  tests/unit/config \
  tests/unit/core \
  tests/unit/persistence \
  tests/unit/events \
  tests/unit/routing \
  tests/unit/secondary \
  tests/unit/scripts \
  tests/unit/agents \
  tests/unit/providers \
  -q
```

**Approximate size:** 1118 tests

**Why here**

- these suites validate configuration loading, core models, persistence invariants, routing logic, schedulers, helper scripts, agent loading, and provider adapters
- most later failures are lower value until these foundations are green

**Representative downstream suites they de-risk**

- `tests/unit/orchestrator/`
- `tests/unit/bigbang/`
- `tests/unit/cli/`
- `tests/unit/mcp/`
- all integration and e2e suites

### Phase 3A: Mid-Layer Contracts (Non-MCP)

**Command**

```bash
uv run pytest \
  tests/unit/execution \
  tests/unit/evaluation \
  tests/unit/plugin \
  tests/unit/observability \
  tests/unit/resilience \
  tests/unit/evolution \
  tests/unit/hermes \
  tests/unit/openclaw \
  tests/unit/pm \
  -q
```

**Approximate size:** 716 tests

**Why here**

- these suites validate subsystem contracts that are still more localized than the high-fan-in orchestrator and CLI layers
- failures here are usually easier to diagnose than the same issue surfacing later through CLI or e2e

### Phase 3B: MCP Unit Surface

**Command**

```bash
uv run pytest tests/unit/mcp -q
```

**Approximate size:** 583 tests

**Why separate**

- MCP is one of the largest single buckets in the repo
- it has strong value when MCP code changes, but it should be isolated so a failure here can explicitly block later MCP integration runs without masking the rest of the plan

**Representative high-volume files**

- `tests/unit/mcp/tools/test_pm_handler.py` (121)
- `tests/unit/mcp/tools/test_definitions.py` (106)

### Phase 4: High Fan-In Unit Flows

Run these suites in this order:

1. `uv run pytest tests/unit/orchestrator -q`
2. `uv run pytest tests/unit/bigbang -q`
3. `uv run pytest tests/unit/cli -q`
4. `uv run pytest tests/unit/tui -q`

**Approximate sizes**

- `tests/unit/orchestrator/`: 678 tests
- `tests/unit/bigbang/`: 471 tests
- `tests/unit/cli/`: 399 tests
- `tests/unit/tui/`: 186 tests

**Why this order**

- `orchestrator` is the main high-fan-in runtime coordinator, so it should fail before CLI and e2e do
- `bigbang` underpins interview, PM, ambiguity, and seed-generation flows that several CLI paths wrap
- `cli` is mostly a surface layer over lower-level engines and should run after those engines are green
- `tui` is comparatively isolated UI coverage; it is valuable, but it does not unlock the rest of the system

### Phase 5: Integration

Run these suites in this order:

1. `uv run pytest tests/integration/test_entry_point.py tests/integration/test_codex_skill_smoke.py tests/integration/test_codex_cli_passthrough_smoke.py tests/integration/test_codex_skill_fallback.py -q`
2. `uv run pytest tests/integration/plugin -q`
3. `uv run pytest tests/integration/mcp -q`

**Approximate sizes**

- root integration smoke: 6 tests
- `tests/integration/plugin/`: 11 tests
- `tests/integration/mcp/`: 84 tests

**Why this order**

- the root integration smoke tests are cheap and validate packaging/runtime entry points
- plugin integration is smaller and more localized than MCP integration
- MCP integration is the heaviest integration bucket and should only run after unit-level MCP coverage is green

### Phase 6: E2E

Run these files in this order:

1. `uv run pytest tests/e2e/test_cli_commands.py -q`
2. `uv run pytest tests/e2e/test_full_workflow.py -q`
3. `uv run pytest tests/e2e/test_session_persistence.py -q`

**Approximate sizes**

- `test_cli_commands.py`: 31 tests
- `test_full_workflow.py`: 18 tests
- `test_session_persistence.py`: 23 tests

**Why this order**

- `test_cli_commands.py` is the cheapest end-to-end validation of command-surface behavior
- `test_full_workflow.py` validates the main system path after CLI and orchestrator units are already green
- `test_session_persistence.py` is the most stateful confirmation layer and should run last

## Skip Rules

These rules are the main value of the plan. They prevent low-value downstream execution.

- If **Phase 0** fails, stop immediately.
- If **Phase 2** fails, skip Phases 3-6 unless the failure is clearly outside the changed area.
- If `tests/unit/mcp/` fails, skip `tests/integration/mcp/`.
- If `tests/unit/orchestrator/` fails, skip `tests/e2e/test_full_workflow.py` and `tests/e2e/test_session_persistence.py`.
- If `tests/unit/cli/` fails, skip `tests/e2e/test_cli_commands.py`.
- If `tests/unit/bigbang/` fails, skip PM/interview/init-style CLI paths until it is fixed.
- If only `tests/unit/tui/` or `src/ouroboros/tui/**` is relevant, do not spend context on MCP, integration, or e2e.
- If only `src/ouroboros/openclaw/**`, `src/ouroboros/hermes/**`, or plugin registry code changed, run those isolated suites before repo-wide phases and expand only if shared core code was touched.

## Change-Scoped Minimum Orders

Use these shortcuts during normal iteration. They are cheaper than the full repo-wide sequence.

| Change area | Minimum order |
| --- | --- |
| `src/ouroboros/config/**`, `src/ouroboros/core/**`, `src/ouroboros/persistence/**`, `src/ouroboros/routing/**` | Phase 0 -> Phase 1 -> relevant Phase 2 suites -> `tests/unit/orchestrator` -> `tests/unit/cli` |
| `src/ouroboros/providers/**` | Phase 0 -> Phase 1 -> `tests/unit/providers` -> `tests/unit/bigbang` -> `tests/unit/orchestrator` |
| `src/ouroboros/mcp/**` | Phase 0 -> Phase 1 -> `tests/unit/mcp` -> `tests/integration/mcp` |
| `src/ouroboros/orchestrator/**` | Phase 0 -> Phase 1 -> relevant Phase 2 suites -> `tests/unit/orchestrator` -> `tests/unit/cli` -> `tests/e2e/test_full_workflow.py` |
| `src/ouroboros/bigbang/**` | Phase 0 -> Phase 1 -> `tests/unit/providers` -> `tests/unit/bigbang` -> relevant CLI PM/init tests |
| `src/ouroboros/cli/**` | Phase 0 -> Phase 1 -> `tests/unit/cli` -> `tests/e2e/test_cli_commands.py` |
| `src/ouroboros/plugin/**` | Phase 0 -> Phase 1 -> `tests/unit/plugin` -> `tests/integration/plugin` |
| `src/ouroboros/tui/**` | Phase 0 -> `tests/unit/tui` |

## Recommended Usage Profiles

### Fast Iteration Profile

Use this for most local development and AI-agent loops:

1. Phase 0
2. Phase 1
3. Only the relevant suites from Phases 2-4 for the changed area
4. At most one matching integration or e2e suite

### Pre-Merge Profile

Use this before merging cross-cutting work:

1. Phase 0 through Phase 5 in order
2. Only the e2e files relevant to the change, unless the change is broad

### Release / Baseline Profile

Use this when validating a release branch or re-establishing repository health:

1. Phase 0 through Phase 6 in strict order
2. Do not skip optional isolated suites

## Why This Order Is Better Than Running `pytest tests/`

Running the entire suite monolithically is expensive and hides where the real breakage sits.

This plan gives a better feedback loop:

- early failures are easier to explain
- later suites are only run when their prerequisites are already green
- large buckets like MCP, orchestrator, bigbang, and CLI are delayed until smaller invariants pass
- end-to-end tests are reserved for confirmation, not discovery
