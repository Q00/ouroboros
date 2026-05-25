---
name: cancel
description: "Cancel stuck or orphaned executions"
---

# /ouroboros:cancel

Cancel stuck or orphaned executions by session ID, cancel all running sessions, or interactively pick from active executions.

## Usage

```
/ouroboros:cancel                          # Interactive: list active, pick one
/ouroboros:cancel <execution_id>           # Cancel specific execution
/ouroboros:cancel --all                    # Cancel all running executions
```

**Trigger keywords:** "cancel execution", "kill session", "stop running", "abort execution"

## How It Works

This skill interacts **directly with the event store** (not via MCP tool) to emit cancellation events. It uses the CLI command under the hood.

Three modes:

1. **Bare (no args)**: Lists all active (running/paused) executions in a numbered table and prompts you to pick one to cancel
2. **Explicit (`execution_id`)**: Cancels the specified execution immediately
3. **`--all` flag**: Cancels every running or paused execution at once

## Instructions

When the user invokes this skill:

1. Determine which mode to use:
   - If the user provided an execution/session ID: **Explicit mode**
   - If the user says "cancel all" or "cancel everything": **--all mode**
   - If no ID given and not "all": **Bare mode** (interactive listing)

2. Run the appropriate CLI command using Bash:

   **Bare mode** (interactive):
   ```bash
   ouroboros cancel execution
   ```
   This will list active executions and prompt for selection.

   **Explicit mode** (specific execution):
   ```bash
   ouroboros cancel execution <execution_id>
   ```

   **Cancel all mode**:
   ```bash
   ouroboros cancel execution --all
   ```

   **With custom reason**:
   ```bash
   ouroboros cancel execution <execution_id> --reason "Stuck for 2 hours"
   ```

3. Present results to the user:
   - Show which executions were cancelled
   - If bare mode, show the list and selection prompt
   - If no active executions, inform the user

4. End with a next-step suggestion:
   - After cancellation: `📍 Cancelled — use ooo status to verify, or ooo run to start fresh`
   - No active sessions: `📍 No active executions — use ooo run to start a new one`

## State Transitions

Only sessions in `running` or `paused` status can be cancelled. Sessions that are already `completed`, `failed`, or `cancelled` are skipped with a warning.

## Fallback (No Database)

If the event store database does not exist:

```
No Ouroboros database found at ~/.ouroboros/ouroboros.db.
Run an execution first with: /ouroboros:run
```

## Example

```
User: cancel that stuck execution

> ouroboros cancel execution

Active Executions
┌───┬──────────────────┬──────────────┬─────────┬─────────┬──────────────┐
│ # │ Session ID       │ Execution ID │ Seed ID │ Status  │ Started      │
├───┼──────────────────┼──────────────┼─────────┼─────────┼──────────────┤
│ 1 │ sess-abc-123     │ exec-001     │ seed-42 │ running │ 2024-01-15   │
│ 2 │ sess-def-456     │ exec-002     │ seed-99 │ paused  │ 2024-01-14   │
└───┴──────────────────┴──────────────┴─────────┴─────────┴──────────────┘

Enter number to cancel (1-2), or 'q' to quit: 1
Cancel session sess-abc-123 (running)? [y/N]: y
✓ Cancelled execution: sess-abc-123

📍 Cancelled — use `ooo status` to verify, or `ooo run` to start fresh
```
