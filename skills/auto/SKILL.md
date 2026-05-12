---
name: auto
description: "Automatically converge from goal to A-grade Seed and execute it"
mcp_tool: ouroboros_auto
mcp_args:
  goal: "$goal"
  resume: "$resume"
  cwd: "$CWD"
  max_interview_rounds: "$max_interview_rounds"
  max_repair_rounds: "$max_repair_rounds"
  skip_run: "$skip_run"
  complete_product: "$complete_product"
  pipeline_timeout_seconds: "$pipeline_timeout_seconds"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Dispatch requirement

This skill must be executed by invoking MCP tool `ouroboros_auto`. Do not
manually inspect repositories, run shell commands, query GitHub, edit files, or
otherwise emulate the auto pipeline as a substitute.

If `ouroboros_auto` is unavailable, stop and report that the required MCP tool
is unavailable. A manual fallback is not an `ooo auto` run.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
ooo auto --resume auto_abc123
ooo auto "Build a local-first habit tracker CLI" --skip-run
ooo auto "Build a local-first habit tracker CLI" --complete-product
/ouroboros:auto "Build a local-first habit tracker CLI"
```

## CLI flag → MCP arg translation

When the user types `ooo auto` with CLI-style flags inside chat, translate to MCP arguments before invoking `ouroboros_auto`:

| CLI flag | MCP arg | Type |
|----------|---------|------|
| `--complete-product` | `complete_product=true` | boolean |
| `--skip-run` | `skip_run=true` | boolean |
| `--max-interview-rounds N` | `max_interview_rounds=N` | integer |
| `--max-repair-rounds N` | `max_repair_rounds=N` | integer |
| `--pipeline-timeout-seconds X` | `pipeline_timeout_seconds=X` | number |
| `--resume <id>` | `resume=<id>` | string |

`--max-generations` is **not** a flag for `ooo auto`; it belongs to `ooo ralph`. When `complete_product=true`, the chained Ralph uses its built-in default (10 generations) bounded by `pipeline_timeout_seconds` or Ralph's own per-iteration / wall-clock budgets.

`--pipeline-timeout-seconds` is accepted only when starting a session. Passing it with `--resume` is rejected because the original deadline is preserved across process restarts.

## Behavior

1. Starts an auto session.
2. Runs bounded Socratic interview rounds with source-tagged auto answers.
3. Generates a Seed.
4. Reviews and repairs until A-grade or blocked.
5. Starts execution only after A-grade.
6. When `complete_product=true`, chains RUN → RALPH_HANDOFF after a successful run handoff so a single invocation iterates Ralph until QA passes, convergence, or a budget bound trips.

The pipeline must not hang indefinitely: all loops are bounded and timeout failures return a resumable `auto_session_id`. Resume with `ooo auto --resume <auto_session_id>`. Use `--skip-run` to stop after the A-grade Seed. The CLI-only `--show-ledger` flag prints assumptions/non-goals; MCP skill responses already include the same ledger summary when available.
