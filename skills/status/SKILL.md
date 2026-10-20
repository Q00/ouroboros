---
name: ouroboros-status
description: "Check session status and measure goal drift"
aliases: [drift]
mcp_tool: ouroboros_session_status
mcp_args:
  session_id: "$1"
---

# /ouroboros:ouroboros-status

Check an execution session's status and, on request, measure drift from the Seed goal.

```
/ouroboros:ouroboros-status [session_id]
```

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros session status"` for `ouroboros_session_status` and `ouroboros_measure_drift`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

### Steps

1. Session: use the given `session_id`, else the most recent one in the conversation, else ask the user.

2. Call `ouroboros_session_status` MCP tool:
   ```
   Tool: ouroboros_session_status
   Arguments:
     session_id: <session ID>
   ```

3. If the user asks about drift (or says "am I drifting?"), also call `ouroboros_measure_drift`:
   ```
   Tool: ouroboros_measure_drift
   Arguments:
     session_id: <session ID>
     current_output: <current execution output or file contents>
     seed_content: <original seed YAML>
     constraint_violations: []  (any known violations)
     current_concepts: []       (concepts in current output)
   ```

4. Show the status and progress, and the drift report as the tool renders it (weights, threshold, breakdown, suggestions). When combined drift exceeds 0.3 (status `EXCEEDED`), warn and suggest `ooo interview` to re-clarify or `ooo evolve` to course-correct; otherwise suggest continuing with `ooo run` or `ooo evaluate`.

## Fallback (no MCP server)

Tell the user session tracking needs the Ouroboros MCP server (`ooo setup`), and that without it they can compare the implementation against the Seed by hand.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
