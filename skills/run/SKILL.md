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

5. **Poll for progress** using `ouroboros_job_wait`:
   ```
   loop:
     Tool: ouroboros_job_wait
     Arguments:
       job_id: <job_id from step 3>
       cursor: <cursor from previous response, starts at 0>
       timeout_seconds: 60

     # Returns immediately when state changes; waits up to 60s otherwise.
     # This reduces tool call round-trips and context consumption.
     # Continue until status is "completed", "failed", or "cancelled"
   ```

   Between polls, report progress concisely (one line):
   ```
   [Executing] Phase: <current_phase> | AC: <completed>/<total>
   ```

6. **Fetch final result** with `ouroboros_job_result`:
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

7. Present the execution results to the user:
   - Show success/failure status
   - Show session ID (for later status checks)
   - Show execution summary

8. **Post-execution QA** (automatic):
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
Phase: Executing | AC Progress: 1/3
Phase: Executing | AC Progress: 2/3
Phase: Executing | AC Progress: 3/3

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
