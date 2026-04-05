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

5. **Ask user about polling strategy** using `AskUserQuestion` immediately after IDs are returned:

   Present the session/job IDs first, then ask:

   ```
   Question: "Execution started. How would you like to monitor progress?"
   Header: "Monitoring"
   Options:
     - label: "Poll here (Recommended)"
       description: "Poll in this session. Context window is consumed but you get real-time updates."
     - label: "Don't poll — I'll monitor separately"
       description: "End here. Use `ooo status <session_id>` in a new terminal or /clone to monitor."
   ```

   **If user chooses "Poll here"**, ask follow-up:
   ```
   Question: "How often should I check progress?"
   Header: "Interval"
   Options:
     - label: "Per level (Recommended)"
       description: "Check once when each parallel level completes. Most context-efficient with meaningful updates."
     - label: "Every 10 minutes"
       description: "Periodic check regardless of level progress. Balanced context usage."
     - label: "Every 20 minutes"
       description: "Minimal context usage. Best for large seeds with many ACs."
   ```

   Then display:
   ```
   💡 Note: Context compression may occur during long executions.
   MCP tools remain available after compression, but prior poll results are summarized.
   If this session is needed for follow-up (ooo evaluate, ooo evolve), shorter polling = more context consumed.
   ```

   **If user chooses "Don't poll"**, display:

   ```
   Execution running in background.
   Session ID: <session_id>
   Job ID: <job_id>
   
   To monitor progress:
     Option A: Open a new terminal → `ooo status <session_id>`
     Option B: Use /clone to fork this conversation for monitoring
     Option C: Come back later and run `ooo status <session_id>` here
   
   When execution completes, continue with: `ooo evaluate <session_id>`
   ```
   Then **stop** — do NOT proceed to polling steps.

6. **Poll for progress** using `ouroboros_ac_tree_hud` (only if user chose to poll):

   The polling behavior differs based on the user's interval choice. In all cases,
   the tool returns a compact markdown snapshot when state changed, or a one-line
   delta ("No AC tree change since cursor <cursor>.") when nothing changed.
   Keep the latest cursor from the tool's meta payload for the next call.

   **Option A: "Per level (Recommended)"**
   ```
   prev_completed = 0

   loop:
     Tool: ouroboros_ac_tree_hud
     Arguments:
       session_id: <session_id from step 3>
       cursor: <cursor from previous response, starts at 0>
       max_nodes: 50

     # Parse completed/total from the snapshot or meta
     current_completed = <completed count>
     total = <total count>

     if current_completed > prev_completed:
       # A level completed — show the returned markdown snapshot as-is
       print snapshot
       prev_completed = current_completed
     # else: continue silently (no output)

     # Continue until meta.status is "completed", "failed", or "cancelled"
   ```

   **Option B: "Every 10 minutes"**
   ```
   loop:
     Tool: ouroboros_ac_tree_hud
     Arguments:
       session_id: <session_id from step 3>
       cursor: <cursor from previous response, starts at 0>
       max_nodes: 50

     # Report on every return regardless of change
     - If unchanged: echo the one-line delta only
     - If changed: show the returned markdown snapshot as-is
     sleep 600  # 10 min between polls

     # Continue until meta.status is "completed", "failed", or "cancelled"
   ```

   **Option C: "Every 20 minutes"**
   ```
   loop:
     Tool: ouroboros_ac_tree_hud
     Arguments:
       session_id: <session_id from step 3>
       cursor: <cursor from previous response, starts at 0>
       max_nodes: 50

     # Report on every return regardless of change
     - If unchanged: echo the one-line delta only
     - If changed: show the returned markdown snapshot as-is
     sleep 1200  # 20 min between polls

     # Continue until meta.status is "completed", "failed", or "cancelled"
   ```

7. **Fetch final result** with `ouroboros_job_result`:
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

8. Present the execution results to the user:
   - Show success/failure status
   - Show session ID (for later status checks)
   - Show execution summary

9. **Post-execution QA** (automatic):
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

[Polling for progress...]
🌳 AC Tree
✅ Parse seed
⏳ Implement workflow routing [Edit src/ouroboros/mcp/tools/ac_tree_hud_handler.py]
⬜ Verify output

elapsed 45s | messages 12 | tools 4

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
