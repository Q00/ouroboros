---
name: resume-session
description: "List in-flight Ouroboros sessions and show the commands needed to re-attach after MCP disconnect"
---

# /ouroboros:resume-session

Recover in-flight Ouroboros sessions after an unexpected MCP server disconnect. The name avoids shadowing Claude Code's built-in `/resume`.

```
ooo resume-session [--all]
/ouroboros:resume-session
```

## Instructions

1. Run with the active runtime's `run_shell` capability:

   ```
   ouroboros resume --list
   ```

   It reads the configured EventStore directly and read-only (no MCP server needed, nothing created or written) and lists `running` or `paused` sessions: the 20 most recent, or all with `--all`. `--list` prints the table and exits without the interactive prompt, which aborts when no terminal is attached. Exit code 2 means the EventStore exists but could not be read.

2. If sessions are listed, show the table and ask which session to work with (active runtime's `ask_user` capability). Each row carries the `session_id`, `exec_id`, and Seed id used below.

3. Give the matching re-attach path:

   - Inspect persisted events (read-only, no MCP server):
     ```
     ouroboros status execution <exec_id> --events
     ```
   - Inspect interactively (read-only monitor; pick the session in the TUI):
     ```
     ouroboros tui monitor
     ```
   - Resume execution (needs the original Seed file; `--resume` takes the `session_id`):
     ```
     ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>
     ```

   Afterwards: `ooo evaluate` once the execution completes, or `ooo cancel <session_id>` if it is stuck.

If it reports "No in-flight sessions found", the execution completed, failed, or was cancelled, or the EventStore was never created; `ouroboros tui monitor` browses past sessions.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
