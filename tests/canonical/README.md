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
`ouroboros_auto`. Use this to catch fixture rot. The run ends with a
copyable status line per scenario, for example:

```text
CANONICAL cli-todo: shape_valid domain=cli completion=product_complete probes=headless_run,stdout_golden budget=1800s live=available_opt_in
```

### Full live run (manual, costs LLM tokens)

```sh
OUROBOROS_RUN_CANONICAL=1 uv run pytest tests/canonical/ -v
```

This command invokes the `ouroboros_auto` MCP tool against each
scenario and asserts the documented terminal state — **use sparingly**,
each scenario will consume real LLM tokens (cli-todo ≈ \$1,
kart-racer ≈ \$5 with Sonnet-class models). Without the environment
variable, the live test skips and only the hermetic shape/catalog checks
run.

### Run a single scenario

All canonical tests live in `tests/canonical/test_canonical.py` and
are parametrized per discovered scenario directory. Filter by slug
with `-k`:

```sh
uv run pytest tests/canonical/ -v -k cli-todo
```

Add `OUROBOROS_RUN_CANONICAL=1` to opt into the live invocation for
that scenario.

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
runtime_probe_kinds:
  - headless_run
  - stdout_golden
wall_clock_budget_seconds: 600       # default: 7200
```

## When to extend

When a fifth scenario class (e.g. `desktop-app`) emerges as worth
canonicalizing, add a new `<slug>/` directory + populate
`expected.yaml`. No infrastructure change required. The runner
auto-discovers.

## Live-run path

The hermetic shape-check is the default. The live-run path
(`OUROBOROS_RUN_CANONICAL=1`) invokes `ouroboros_auto` against each
scenario and treats MCP errors, failed terminals, and unverified
PRODUCT_COMPLETE handoffs as test failures.

## Runtime-binary preflight (PR-γ / #1170)

The harness asserts at session-start that `ouroboros.__file__` resolves
under the repo root. If it does not, the entire harness fails fast with
a copy-pasteable fix command, rather than producing false-positive
acceptance evidence against a different binary.

This protects against the #1170 R2 (20260526-1636) and R2-1709
incidents: in both cases the MCP server was importing uvx-installed
0.39.1 from `/Users/.../uv/tools/ouroboros-ai/lib/...` while the
worktree carried 0.39.2.devNN with the substrate fixes under test.
The harness produced BLOCKED evidence and the team chased a dead-end
investigation for hours before noticing the binary mismatch.

**Opt-out:** set `OUROBOROS_CANONICAL_SKIP_RUNTIME_CHECK=1` for the
narrow case where a maintainer is deliberately validating against a
published release (e.g. confirming a release-cut PR before tagging).
The runtime path is still recorded in evidence; it just isn't
enforced.

## Evidence-integrity contract (PR-γ)

On every live run the harness persists the **raw MCP handler response**
verbatim to `<workdir>/.ooo-observability/canonical-<slug>-<UTC>.json`.
The file is written BEFORE any assertion runs, so even on assertion
failure the on-disk artifact is a faithful 1:1 capture of what the
MCP tool emitted.

Schema (stable, parseable):

```json
{
  "scenario": "cli-todo",
  "goal": "...",
  "workdir": "/tmp/.../cli-todo",
  "captured_at_utc": "20260527-123456",
  "preflight": {
    "runtime_path": "/Users/.../src/ouroboros/__init__.py",
    "runtime_version": "0.39.2.dev75",
    "repo_root": "/Users/...",
    "enforced": true,
    "python_executable": "/opt/homebrew/bin/python3.12"
  },
  "scenario_metadata": { "domain_class": "cli", ... },
  "mcp_result_is_ok": true,
  "mcp_result_is_error": false,
  "mcp_result_meta": { ... raw envelope ... },
  "mcp_result_content": [ ... raw content items ... ],
  "mcp_result_fallback_text": null
}
```

**Reporter contract:** when a maintainer or sub-agent reports a
canonical R2/R3 result to #1170, they MUST cite the on-disk JSON
artifact, not paraphrased field values. The #1170 R2-1709 evidence
contained two fabricated field values
(`interview_closure_mode="max_rounds_reached"`,
`stop_reason_code="interview_max_rounds_no_closure"`) that did not
exist anywhere in source — paraphrase had silently corrupted the
evidence. The raw-passthrough file is the SSOT for canonical
acceptance.

## Closure-mode contract (PR-β / SSOT #1157 Closure Policy)

The live-run test asserts that `interview_closure_mode` (when present
on the envelope) is one of `{ledger_only, mutual_agreement,
safe_default}`. Per SSOT #1157 *Closure Policy* (2026-05-27),
`ledger_only` is the expected default path; `mutual_agreement` is
the lucky case where both signals align; `safe_default` is the
max_rounds fallback for ledgers that never structurally complete.

Any `interview_max_rounds_exhausted` blocker on a canonical scenario
is treated as a hard failure — it indicates either the legacy
AND-gate is back in production or PR-β has not been deployed
(release cut needed via PR-δ).
