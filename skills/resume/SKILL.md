---
name: resume
description: "List in-flight sessions and re-attach after MCP disconnect"
---

# /ouroboros:resume

Recover in-flight sessions after an unexpected MCP server disconnect.

## Usage

```
ooo resume
```

**Trigger keywords:** "resume session", "re-attach", "mcp disconnected", "lost session", "in-flight"

## How It Works

`ooo resume` reads the EventStore directly (no MCP server required) and lists
every session that is still in a `running` or `paused` state. You can then
pick one from the interactive prompt to receive re-attach instructions.

## Instructions

When the user invokes this skill:

1. Run the CLI command:

   ```
   ouroboros resume
   ```

   This reads `~/.ouroboros/ouroboros.db` directly — the MCP server does **not**
   need to be running.

2. If sessions are listed, enter the number corresponding to the session you
   want to re-attach to. The command will print the `exec_id`.

3. Re-attach using:

   ```
   ooo status <exec_id>
   ```

## Fallback (No sessions found)

If the command reports "No in-flight sessions found", the execution either
completed, failed, or was already cancelled. Check with:

```
ouroboros status executions
```

## Example

```
User: ooo resume

┌─────────────────────── In-Flight Sessions ───────────────────────┐
│  #  Session ID          Execution ID        Status    Started    │
│  1  sess-abc123         exec-xyz789         running   2026-04-15 │
└───────────────────────────────────────────────────────────────────┘

Enter number to re-attach (1-1), or 'q' to quit: 1

╭─ Re-attach ──────────────────────────────────────────╮
│ Session selected: sess-abc123                        │
│ Execution ID:     exec-xyz789                        │
│                                                      │
│ Re-attach by running:                                │
│                                                      │
│     ooo status exec-xyz789                           │
╰──────────────────────────────────────────────────────╯
```

## Next Steps

After re-attaching:
- `ooo status <exec_id>` — Check current execution status and drift
- `ooo evaluate` — Evaluate results once execution completes
- `ooo cancel execution <exec_id>` — Cancel if the session is stuck
