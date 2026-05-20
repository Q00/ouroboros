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

If `ouroboros_auto` is invoked successfully but returns `blocked`, `failed`, or
another terminal auto-session status, report that auto-session status and the
tool's blocker. Do not label that outcome as MCP dispatch failure; dispatch
failure means the MCP tool could not be invoked.

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
6. When `complete_product=true`, chains RUN → RALPH_HANDOFF after a successful run handoff and waits for a terminal Ralph status so a single invocation iterates Ralph until QA passes, convergence, or a budget bound trips. A QA-pass on the executed product completes the auto session; recognized failure modes (`iteration_timeout`, `wall_clock_exhausted`, `oscillation_detected`, `grade_regressing`, `max_generations reached`) block the auto session with the matching `stop_reason` in `last_error` so operators can resume after the cause is addressed.

### Canonical stop_reason_code taxonomy

| Layer | Code | Surface | Meaning |
|---|---|---|---|
| Interview | `interview_max_rounds_exhausted` | `last_error_code`, `result.stop_reason_code` | Auto interview ran `max_interview_rounds` without ledger+backend mutual closure or safe-default fallback. |
| Interview | `interview_phase_deadline` | `last_error_code`, `result.stop_reason_code` | Interview phase exceeded its per-phase timeout. |
| Ralph | `iteration_timeout` | blocker text + (future) `result.stop_reason_code` | A single Ralph iteration exceeded its per-iteration timeout. |
| Ralph | `wall_clock_exhausted` | blocker text + (future) `result.stop_reason_code` | The Ralph wall-clock budget was exhausted before convergence. |
| Ralph | `oscillation_detected` | blocker text + (future) `result.stop_reason_code` | Ralph oscillated between two grade states without making progress. |
| Ralph | `grade_regressing` | blocker text + (future) `result.stop_reason_code` | A subsequent Ralph generation produced a strictly worse grade than its predecessor. |
| Ralph | `max_generations reached` | blocker text + (future) `result.stop_reason_code` | Ralph hit its configured generation cap before reaching A grade. |

Blockers without a canonical code keep using the free-form ``last_error`` text. Ralph-layer codes are surfaced via blocker text today; their result-envelope promotion is tracked as a follow-up.

The pipeline must not hang indefinitely: all loops are bounded and timeout failures return a resumable `auto_session_id`. Resume with `ooo auto --resume <auto_session_id>`. Use `--skip-run` to stop after the A-grade Seed. Use `--complete-product` to drive the full Interview → Seed → Run → Ralph → Product chain on a single `ooo auto` invocation; the chained Ralph loop honors the same wall-clock deadline as the parent auto session (`--timeout`). The CLI-only `--show-ledger` flag prints assumptions/non-goals; MCP skill responses already include the same ledger summary when available.
