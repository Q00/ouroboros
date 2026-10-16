---
name: cancel
description: "Cancel stuck or orphaned executions"
aliases: [kill, abort]
---

# /ouroboros:cancel

Cancel stuck or orphaned executions through the CLI, which writes cancellation events straight to the event store.

```
/ouroboros:cancel                 # list active executions, ask which to cancel
/ouroboros:cancel <session_id>    # cancel one execution
/ouroboros:cancel --all           # cancel every running or paused execution
```

## Instructions

Run the command for the user's request with the active runtime's `run_shell` capability:

- **No ID, not "all"**: run `ouroboros cancel execution --list`. It prints the active executions and exits without prompting (the command's own interactive prompt aborts when no terminal is attached). Show the list, ask which session to cancel with the active runtime's `ask_user` capability, then cancel that `session_id` as below. If the user declines, cancel nothing.
- **ID given**: `ouroboros cancel execution <session_id>`
- **"cancel all" / "cancel everything"**: `ouroboros cancel execution --all`

Add `--reason "<text>"` to record why. Only `running` or `paused` sessions can be cancelled; completed, failed, or already cancelled ones are skipped with a warning.

Report what the command printed (`Cancelled execution: <id>`, `Cancellation requested for execution: <id>`, or `No active executions found.`) and relay any warning verbatim. Then suggest `ooo status` to verify or `ooo run` to start fresh.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
