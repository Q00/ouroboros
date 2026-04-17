---
name: resume
description: "List in-flight sessions and show the commands needed to re-attach after MCP disconnect"
---

# /ouroboros:resume

Recover in-flight sessions after an unexpected MCP server disconnect.

## Usage

```
ooo resume
ooo resume --all
```

**Trigger keywords:** "resume session", "re-attach", "mcp disconnected", "lost session", "in-flight"

## How It Works

`ooo resume` reads the EventStore directly (no MCP server required) and lists
every session that is still in a `running` or `paused` state. The command is
strictly read-only — it never creates the data directory, never writes
schema, and never appends events. Its job is to surface the identifiers you
need to re-attach.

- `ooo resume` shows the 20 most recent active sessions.
- `ooo resume --all` shows every active session.

## Instructions

When the user invokes this skill:

1. Run the CLI command:

   ```
   ouroboros resume
   ```

   This reads `~/.ouroboros/ouroboros.db` directly — the MCP server does **not**
   need to be running.

2. If sessions are listed, enter the number of the session you want to work
   with. The command prints both the `session_id` and the `exec_id`, along
   with the two re-attach paths.

3. Pick the right re-attach path:

   - **Inspect only** (read-only interactive monitor):

     ```
     ouroboros tui monitor
     ```

     Launches the TUI and lets you pick the session to inspect. The
     `ouroboros status execution <exec_id>` command is *registered* but its
     handler is still a placeholder in `src/ouroboros/cli/commands/status.py`
     (it only prints "Would show details for execution: …"), so it is
     intentionally not surfaced here. Follow-up tracked as a separate issue.

   - **Resume execution** (requires the original seed file):

     ```
     ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>
     ```

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success — sessions listed, or no sessions found |
| `1`  | Invalid user selection (non-numeric or out-of-range) |
| `2`  | EventStore exists but could not be opened or read |

## Fallback (No sessions found)

If the command reports "No in-flight sessions found", the execution either
completed, failed, was cancelled, or the EventStore has never been created.
To browse historical sessions interactively, use the TUI monitor:

```
ouroboros tui monitor
```

## Example

```
User: ooo resume

┌─────────────────────── In-Flight Sessions ───────────────────────┐
│  #  Session ID          Execution ID        Status    Started    │
│  1  sess-abc123         exec-xyz789         running   2026-04-15 │
└───────────────────────────────────────────────────────────────────┘

Enter number to re-attach (1-1), or 'q' to quit: 1

╭─ Re-attach ────────────────────────────────────────────────────────────────╮
│ Session ID:   sess-abc123                                                  │
│ Execution ID: exec-xyz789                                                  │
│                                                                            │
│ Inspect (read-only interactive monitor):                                   │
│     ouroboros tui monitor                                                  │
│                                                                            │
│ Resume execution (requires the original seed file):                        │
│     ouroboros run workflow --orchestrator --resume sess-abc123 seed-001    │
╰────────────────────────────────────────────────────────────────────────────╯
```

## Next Steps

After you have the identifiers:

- `ouroboros tui monitor` — launch the TUI and pick the session to inspect
- `ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>` — resume execution
- `ooo evaluate` — evaluate results once the execution completes
- `ooo cancel execution <exec_id>` — cancel if the session is stuck
