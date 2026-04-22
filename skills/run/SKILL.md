---
name: run
description: "Execute a Seed specification through the workflow engine"
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
---

# /ouroboros:run

Execute a Seed specification through the Ouroboros workflow engine.

## Usage

```
/ouroboros:run [seed_file_or_content]
```

**Trigger keywords:** "ouroboros run", "execute seed"

## How It Works

1. **Input**: Provide seed YAML content directly or a path to a `.yaml` file
2. **Validation**: Seed is parsed and validated (goal, constraints, acceptance criteria, ontology)
3. **Execution**: The orchestrator runs the workflow with PAL routing
4. **Progress**: Real-time progress updates via session tracking
5. **Result**: Execution summary with pass/fail status

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before proceeding.**

1. Use the `ToolSearch` tool to find and load the execution MCP tools:
   ```
   ToolSearch query: "+ouroboros execute"
   ```
2. The tools will typically be named with prefix `mcp__plugin_ouroboros_ouroboros__` (e.g., `ouroboros_execute_seed`, `ouroboros_session_status`). After ToolSearch returns, the tools become callable.
3. If ToolSearch finds the tools → proceed with the steps below. If not → skip to **Fallback** section.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Execution Steps

1. **Detect git workflow** (before any code changes):
   - Read the project's `CLAUDE.md` for git workflow preferences
   - If PR-based workflow detected and currently on `main`/`master`:
     - Create a feature branch: `ooo/run/<session_id>`
     - All code changes go to this branch
   - If no preference: use current branch (backward compatible)

2. Check if the user provided seed content or a file path:
   - If a file path: Read the file with the Read tool
   - If inline YAML: Use directly
   - If neither: Check conversation history for a recently generated seed

3. **Start background execution** with `ouroboros_start_execute_seed`:
   ```
   Tool: ouroboros_start_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     model_tier: "medium"  (or as specified by user)
     max_iterations: 10    (or as specified by user)
   ```
   This returns immediately with a `job_id`, `session_id`, and `execution_id`.

4. If resuming an existing session, include `session_id`:
   ```
   Tool: ouroboros_start_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     session_id: <existing session ID>
   ```

5. **Recommended monitoring stance: relay compact progress in the main session.**

   After IDs are returned, print only this short handoff:

   ```
   Execution started in background.
   Job ID: <job_id>
   Session ID: <session_id>
   Execution ID: <execution_id>

   I will wait in low-token relay mode and report only meaningful state changes.
   For full details later: `ouroboros_ac_tree_hud(session_id=<session_id>)`
   ```

   Rationale: the main chat session must wait for MCP calls. Frequent full-tree
   polling burns context without improving execution. The user usually wants
   "what is it doing now?" rather than the whole tree, so use compact job
   read-model snapshots and narrate them like a brief live relay.

6. **Low-token relay loop with `ouroboros_job_wait` (recommended default).**

   Use `ouroboros_job_wait`, not repeated `ouroboros_ac_tree_hud`, for routine
   monitoring. Keep the latest cursor and previous progress counters from the
   tool meta payload.

   This loop is intentionally harness/model friendly:
   - Treat `response.meta` as the source of truth.
   - Do not parse `response.text` for counts, status, or cursor.
   - Use `response.text` only as a human-readable current-message hint.
   - Keep all local monitor state in simple scalar variables.
   - Emit at most one short relay message per changed response.
   - Always continue to final `ouroboros_job_result` after a terminal status.

   ```
   cursor = <cursor from start/status response, or 0>
   prev_status = "running"
   prev_phase = null
   prev_ac_completed = 0
   prev_sub_ac_completed = 0
   prev_message = null

   loop:
     Tool: ouroboros_job_wait
     Arguments:
       job_id: <job_id from step 3>
       cursor: <cursor>
       timeout_seconds: 180
       view: "summary"

     cursor = response.meta.cursor

     if response.meta.changed is false:
       # Do not narrate unless the user explicitly asked for heartbeat updates.
       continue

     status = response.meta.status
     phase = response.meta.current_phase
     ac_completed = response.meta.ac_completed
     ac_total = response.meta.ac_total
     sub_ac_completed = response.meta.sub_ac_completed
     sub_ac_total = response.meta.sub_ac_total
     message_hint = first non-empty non-metadata line from response.text, or null

     # Build one short relay update from structured fields.
     if status in ["completed", "failed", "cancelled", "interrupted"]:
       print terminal_relay(status, phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total)
       break

     if ac_completed > prev_ac_completed:
       print ac_progress_relay(phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total, message_hint)
     elif sub_ac_completed > prev_sub_ac_completed:
       print sub_ac_progress_relay(phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total, message_hint)
     elif phase != prev_phase or status != prev_status:
       print phase_or_status_relay(status, phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total)
     elif message_hint != prev_message:
       print current_work_relay(phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total, message_hint)

     prev_status = status
     prev_phase = phase
     prev_ac_completed = ac_completed or prev_ac_completed
     prev_sub_ac_completed = sub_ac_completed or prev_sub_ac_completed
     prev_message = message_hint or prev_message
   ```

   Notes:
   - `timeout_seconds: 180` means the MCP call can block for up to 3 minutes.
     This keeps the main session available often enough for a live relay while
     still avoiding noisy polling.
   - Use `view: "compact"` for very long jobs or when the user only wants a
     heartbeat. It returns one-line state such as `job_x | running | AC 3/17`.
   - Use `view: "summary"` for normal monitoring. It includes the job message
     plus AC/Sub-AC counts.
   - Use `view: "full"` only when the user asks for detailed job status.

   Relay style examples:
   - `In progress: Deliver is at AC 1/3 and Sub-AC 12/16. Current work is the Sub-AC 3 regression test.`
   - `Level update: parallel level 1/1 has finished, and AC progress advanced to 3/3.`
   - `Completed: execution finished. Fetching the final job result now.`

   Relay output contract for other harnesses/models:
   - One update should be 1-2 sentences or 1 compact line.
   - Include `phase`, `AC completed/total`, and `Sub-AC completed/total` when present.
   - Include the current work hint only if it changes.
   - Never include the full AC tree in routine relay output.
   - Never include raw JSON, raw meta dumps, or repeated unchanged cursor lines.
   - Terminal statuses must be explicit: completed, failed, cancelled, or interrupted.

   Do not paste the full raw tool output unless the user asks for raw status.
   Do not add speculative ETA unless the tool provides one.

7. **Use `ouroboros_ac_tree_hud` only for manual drill-down or anomaly checks.**

   Do not call full tree HUD in the normal polling loop.

   Use these targeted calls:

   ```
   # Explicit short HUD, useful for a one-off check
   Tool: ouroboros_ac_tree_hud
   Arguments:
     session_id: <session_id>
     cursor: <cursor>
     view: "summary"

   # Lowest-token one-line HUD
   Tool: ouroboros_ac_tree_hud
   Arguments:
     session_id: <session_id>
     cursor: <cursor>
     view: "compact"

   # Full tree only when user asks "show details", progress looks stuck,
   # or debugging requires seeing AC/Sub-AC structure.
   Tool: ouroboros_ac_tree_hud
   Arguments:
     session_id: <session_id>
     cursor: <cursor>
     view: "tree"
     max_nodes: 30
   ```

   Treat `unchanged cursor=<cursor>` from explicit compact/summary views as a
   no-op. Do not explain it to the user unless they explicitly asked for
   heartbeat messages.

8. **Fetch final result** with `ouroboros_job_result`:
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

9. Present the execution results to the user:
   - Show success/failure status
   - Show session ID (for later status checks)
   - Show execution summary

10. **Post-execution QA** (automatic):
   `ouroboros_start_execute_seed` automatically runs QA after successful execution.
   The QA verdict is included in the final job result text.
   To skip: pass `skip_qa: true` to the tool.

   Present QA verdict with next step:
   - **PASS**: `Next: ooo evaluate <session_id> for formal 3-stage verification`
   - **REVISE**: Show differences/suggestions, then `Next: Fix the issues above, then ooo run to retry -- or ooo unstuck if blocked`
   - **FAIL/ESCALATE**: `Next: Review failures above, then ooo run to retry -- or ooo unstuck if blocked`

## Fallback (No MCP Server)

If the MCP server is not available, inform the user:

```
Ouroboros MCP server is not configured.
To enable full execution mode, run: /ouroboros:setup

Without MCP, you can still:
- Use /ouroboros:interview for requirement clarification
- Use /ouroboros:seed to generate specifications
- Manually implement the seed specification
```

## Example

```
User: /ouroboros:run seed.yaml

[Reads seed.yaml, validates, starts background execution]

Background execution started.
Job ID: job_a1b2c3d4e5f6
Session ID: orch_x1y2z3
Execution ID: exec_m1n2o3

[Relay]
In progress: Deliver is at AC 1/3 and Sub-AC 12/16.
Current work is finishing the workflow routing Sub-AC.

[Relay]
Level update: parallel level 1/1 has finished, and AC progress advanced to 3/3.
Execution is complete. Fetching the final job result now.

[Fetching final result...]

Result:
  Seed Execution SUCCESS
  ========================
  Session ID: orch_x1y2z3
  Goal: Build a CLI task manager
  Duration: 45.2s
  Messages Processed: 12

  Next: `ooo evaluate orch_x1y2z3` for formal 3-stage verification
```
