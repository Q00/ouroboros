---
name: auto
description: "Automatically converge from goal to A-grade Seed and execute it"
mcp_tool: ouroboros_auto
mcp_args:
  goal: "$1"
  cwd: "$CWD"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
/ouroboros:auto "Build a local-first habit tracker CLI"
```

## Behavior

1. Starts an auto session.
2. Runs bounded Socratic interview rounds with source-tagged auto answers.
3. Generates a Seed.
4. Reviews and repairs until A-grade or blocked.
5. Starts execution only after A-grade.

The pipeline must not hang indefinitely: all loops are bounded and timeout failures return a resumable `auto_session_id`.
