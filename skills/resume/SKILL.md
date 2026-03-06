---
name: resume
description: "Resume an interrupted workflow session from checkpoint"
---

# /ouroboros:resume

Resume an interrupted workflow session from its last checkpoint.

## Usage

```
ooo resume [session_id]
/ouroboros:resume [session_id]
```

**Trigger keywords:** "resume", "continue session", "pick up where I left off"

## How It Works

When a workflow is interrupted (Ctrl+C), Ouroboros saves a checkpoint with:
- Which acceptance criteria were completed
- Which are still pending
- The seed specification and goal
- Progress metadata (messages, duration)

Resuming injects this context so the agent continues from the last completed AC.

## Instructions

When the user invokes this skill:

1. **Find interrupted sessions**:
   - If `session_id` provided: use it directly
   - If no session_id: check for the most recently interrupted session
   - If multiple found: present options via AskUserQuestion

2. **Load checkpoint context**:
   - Call `ouroboros workflow resume <session_id>` CLI or reconstruct from events
   - Build a summary of completed vs remaining ACs

3. **Resume execution**:
   - Call `ouroboros_execute_seed` MCP tool with:
     ```
     Tool: ouroboros_execute_seed
     Arguments:
       seed_content: <original seed YAML>
       session_id: <interrupted session ID>
     ```
   - The resumed session continues from the last checkpoint

4. **Present progress**:
   - Show which ACs were already completed (from previous session)
   - Show which ACs are being worked on now
   - Track new progress normally

## Fallback (No MCP Server)

If the MCP server is not available:

1. Run `ouroboros workflow list --interrupted` to find sessions
2. Run `ouroboros workflow resume <session_id>` to get the context summary
3. Use the context summary to manually guide the agent on what remains

## Example

```
User: ooo resume

Resuming session sess-abc-123
Goal: Build a CLI task manager
Previous progress: 3/5 ACs completed

Completed:
  [x] Tasks can be created
  [x] Tasks can be listed
  [x] Tasks support tags

Remaining:
  [ ] Tasks can be marked complete
  [ ] Tasks persist across restarts

Continuing execution from AC 4...
```

## Next Steps (Always Display)

After resumption completes, show the same next steps as `ooo run`:

**On success:**
```
📍 Next: `ooo evaluate <session_id>` to verify against acceptance criteria
```

**On failure:**
```
📍 Next steps:
  - Fix the issues identified above, then `ooo resume` to continue
  - `ooo unstuck` if you're blocked on how to fix it
```
