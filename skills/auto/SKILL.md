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
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
ooo auto --resume auto_abc123
ooo auto "Build a local-first habit tracker CLI" --skip-run
/ouroboros:auto "Build a local-first habit tracker CLI"
```

## Behavior

1. Starts an auto session.
2. Runs bounded Socratic interview rounds with source-tagged auto answers.
3. Generates a Seed.
4. Reviews and repairs until A-grade or blocked.
5. Starts execution only after A-grade.

The pipeline must not hang indefinitely: all loops are bounded and timeout failures return a resumable `auto_session_id`. Resume with `ooo auto --resume <auto_session_id>`. Use `--skip-run` to stop after the A-grade Seed. The CLI-only `--show-ledger` flag prints assumptions/non-goals; MCP skill responses already include the same ledger summary when available.
