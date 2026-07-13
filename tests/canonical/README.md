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
- Not a general-purpose recorded-replay system. The issue-specific #1450
  experiment below replays one frozen ledger solely to answer that issue's
  bounded quality question.
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
scenario, asserts the documented terminal state, and then executes any
scenario-declared generated-artifact smoke commands — **use sparingly**,
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

### Issue #1450 paired auto-fill quality experiment

`test_issue_1450_quality.py` compares two closure strategies from the
same frozen non-converged `cli-todo` ledger:

- arm `x`: `finalize_safe_defaultable_gaps`,
- arm `y`: one proposal manifest produced through the existing
  `AutoAnswerer.answer_gap` + temperature-zero `LLMAnswerRefiner`
  contract, then applied through `auto_fill_remaining`.

The default path is hermetic and checks fixture reproduction, ledger
clone invariants, both transformations, and verdict classification:

```sh
uv run pytest tests/canonical/test_issue_1450_quality.py -v
```

The product comparison is costly and requires an explicit opt-in plus
an evidence directory that will survive pytest cleanup:

```sh
PATH="$PWD/.venv/bin:$PATH" \
OUROBOROS_RUN_AUTO_FILL_QUALITY=1 \
OUROBOROS_AUTO_FILL_QUALITY_EVIDENCE_DIR=/absolute/path/to/issue-1450-evidence \
uv run pytest tests/canonical/test_issue_1450_quality.py \
  -k live_paired_quality_experiment -v -s
```

The `PATH` preflight matters because the `cli-todo` smoke contract
invokes `python`. The live runner also fails before any LLM call when
the frozen fixture, `src/ouroboros` tree, goal, expected contract, or
required smoke executables have drifted.

The runner generates the treatment proposals once and reuses the exact
manifest for paired orders `x/y`, `y/x`, `x/y`. It extends to five
pairs only when the first three pairs contain exactly one classified
infrastructure/transient failure. Every arm uses a fresh `AutoStore`
and workdir.

Evidence is written under
`$OUROBOROS_AUTO_FILL_QUALITY_EVIDENCE_DIR/issue-1450-<UTC>/` and
includes:

- fixture/source hashes, resolved runtime-config fingerprint, proposal
  manifest and hash,
- both transformed ledgers and their provenance,
- each arm's pre-repair Seed, final persisted state, MCP envelope, and
  A2 trace directory,
- complete smoke argv/stdout/stderr/exit-code records,
- pair outcomes and the final experiment verdict.

Verdicts are intentionally narrow:

| Verdict | Meaning |
|---|---|
| `no_observed_gap` | Every valid pair passed all mandatory product smoke checks in both arms. |
| `treatment_improvement_candidate` | Arm `y` alone passed the same mandatory oracle in at least two pairs, with no opposite result. |
| `treatment_regression` | Arm `x` alone passed in at least two pairs, with no opposite result. |
| `inconclusive` | Mixed results, insufficient valid pairs, repeated shared failure, degraded/partial output, or unresolved infrastructure variance. |

An `inconclusive` verdict fails the pytest invocation after persisting
the evidence. Other verdicts are valid experiment outcomes and do not
themselves fail the test.

This experiment does **not** establish broad strategy equivalence and
does not isolate metadata causality. It only measures whether the two
proposal-content strategies produce a reproducible product-smoke
difference for the frozen canonical `cli-todo` fixture.

## Scenario directory shape

Each `tests/canonical/<slug>/` directory contains:

| File | Purpose |
|---|---|
| `goal.txt` | One-line goal string fed to `ooo auto`. No leading/trailing whitespace beyond a final newline. |
| `expected.yaml` | Frozen metadata: `domain_class`, `completion_mode`, `runtime_probe_kinds`, optional `wall_clock_budget_seconds`, and optional live product-reality smoke checks. |
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

# optional live product-reality smoke checks
product_artifact_path: habit_tracker.py
declared_output_paths:
  - habits.json
product_smoke_commands:
  - argv: ["python", "{artifact}", "add", "drink water"]
    expect_exit_code: 0
    stdout_contains: ["drink water"]
  - argv: ["python", "{artifact}", "unknown-command"]
    expect_exit_code: 2
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
  "mcp_result_fallback_text": null,
  "product_reality": {
    "artifact_path": "habit_tracker.py",
    "declared_output_paths": ["habits.json"],
    "smoke_results": [
      {"argv": ["python", "/tmp/.../habit_tracker.py", "list"], "exit_code": 0, "stdout_preview": "...", "stderr_preview": ""}
    ],
    "auto_session_id": "...",
    "execution_id": "...",
    "run_session_id": "..."
  }
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

The live-run test accepts `interview_closure_mode` values
`{ledger_only, mutual_agreement, safe_default}` for persisted-session
compatibility. On current code, a normal backend closure is
`mutual_agreement`, while bounded unresolved but safely defaultable
gaps may close as `safe_default` only after the backend confirms the
post-synthesis ambiguity gate. `ledger_only` remains accepted for
legacy evidence and resume compatibility; it is not the expected
current-main default path.

Any `interview_max_rounds_exhausted` blocker on a canonical scenario
is treated as a hard failure — it indicates either the legacy
AND-gate is back in production or PR-β has not been deployed
(release cut needed via PR-δ).

## Interview-deadline closure ladder (PR-D / #1257)

`interview_phase_deadline` BLOCKED is also a hard failure on canonical
scenarios. PR-A/B/C/D of #1257 replace that terminal with a typed
closure ladder:

- the deadline emits `runtime.deadline.interview.fired`,
- the pipeline synthesizes a `Seed` (complete ledger →
  `synthesize_seed_from_ledger`, incomplete → `partial_seed_from_evidence`),
- and the partial Seed reaches `AutoPhase.COMPLETE` with
  `auto.product.partial_emitted` + `partial_product=True` /
  `partial_unresolved_slots` on the result envelope.

The hermetic regression for that contract lives in
`tests/integration/auto/test_interview_deadline_partial_product_regression.py`
and runs in plain CI alongside this hermetic harness (no
`OUROBOROS_RUN_CANONICAL=1` needed because the deadline simulator is
deterministic and incurs no LLM cost). The live-run path here MUST
also surface `partial_product=True` and the deadline/partial-emitted
event pair when the deadline branch is taken — paraphrased absence of
those fields is treated as evidence the closure ladder has regressed.
