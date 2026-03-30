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
ooo run [seed_file_or_content]
/ouroboros:run [seed_file_or_content]
```

**Trigger keywords:** "ouroboros run", "execute seed"

## Instructions

When the user invokes this skill:

### Step 0: Load Tools

**If `ToolSearch` is available** (Claude Code): call it in a single message:
1. `ToolSearch` with `+ouroboros execute` — load execution MCP tools.
2. `ToolSearch` with `select:AskUserQuestion` — load question tool.

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip directly to Step 1.

- Store whichever question tool is available (`AskUserQuestion` or `AskQuestion`) as the **question tool**.
- If `ouroboros_execute_seed` is reachable → use **Path A** (native subagents).
- If unavailable → skip to **Path B** (no MCP fallback).

### Step 1: Determine seed input

One of these (in priority order):
- If a remembered `seed_id` is already available: use `seed_id` directly
- If the argument matches `seed_<hex>.yaml` or `seed_<hex>` (no path separators, seed ID pattern):
  strip `.yaml` extension and use as `seed_id` — do NOT use `seed_path`
- If the user passed an absolute or relative file path (contains `/` or `\`): use `seed_path`
- If the user passed inline YAML (starts with `---` or contains `goal:`): use `seed_content`
- If neither is available: **ask the user** for an explicit seed reference using the question tool.

Do not inspect MCP schemas, browse the repository, or guess the seed from folders.

### Step 1.5: Determine working directory

The execution `cwd` determines where files are created and commands are run.
Do NOT assume the current session directory is the target project.

Ask the user using the question tool:
- Prompt: `Which directory should the code be executed in?`
- Options: `Current directory (<session_cwd>)`, `Enter path manually`

If the user picks `Enter path manually`, ask: `Enter the absolute path to the project directory.`

Use the confirmed directory as `cwd` in all subsequent MCP calls.

## Path A: Native Subagent Execution

Use this when MCP tools are available and the runtime supports native subagents.

**Architecture**: MCP owns state. Main session only routes stages and records the final result. `@ac-executor` agents fetch their own full seed state from MCP.

```
MCP (prepare + record)
  ←→ You (main — orchestrate stages, detect conflicts)
      ←→ @ac-executor × N (parallel, one per AC per stage)
```

### Flow

1. **Prepare session** via MCP:
   ```
   Tool: ouroboros_execute_seed
   Arguments:
     action: prepare
     seed_id: <seed_id>        # preferred
     # OR seed_content: <yaml>
     # OR seed_path: <path>
     cwd: <working directory confirmed in Step 1.5>
   ```
   **Always pass `cwd`** — without it the MCP server falls back to its own process cwd.
   Returns a text line `Prepared session:... ac:N stages:M` followed by a `meta:` JSON line.
   Parse the `meta:` JSON to extract `session_id`, `seed_id`, `ac_count`, `ac_briefs`, `stage_plan`, `cwd`.

2. **Do not call `action: state` in the main session.**
   Use only `stage_plan` and `ac_briefs` from the parsed `meta:` JSON for routing.
   If `stage_plan` is absent from the response, default to one stage with all AC indices.

3. **Execute stages sequentially. Within each stage, execute ACs in parallel.**

   For each stage in `stage_plan` (in order):

   a. Spawn one `@ac-executor` per AC in this stage **in a single message** (parallel):

      ```
      Tool: Agent
        subagent_type: ouroboros:ac-executor
        description: "AC <index>: <brief description>"
        prompt: |
          session_id: <session_id>
          ac_index: <index>
          cwd: <cwd from prepare.meta.cwd>
          parallel_acs: <other AC indices in this stage>
          stage_context: <summary from prior stage results or conflict notes, if any>
      ```

      **Always include `cwd` from the prepare response** — subagents cannot reliably infer it.

   b. **Wait for ALL executors in this stage to complete** before starting the next stage.

   c. **Collect results and detect conflicts**:

      Parse each executor's compact result:
      ```
      ac:<index> status:DONE|PARTIAL|FAILED
      created:<files>
      modified:<files>
      tests:<n/total>
      note:<summary>
      ```

      Check for file overlap between executors. If conflicts exist, summarize them as `stage_context` for the next stage.

   d. **Proceed to the next stage** (if any) with the updated `stage_context`.

4. **Record final result** via MCP:
   ```
   Tool: ouroboros_execute_seed
   Arguments:
     action: record_result
     session_id: <session_id>
     agent_execution_result: |
       <aggregated summary: ac results, files, tests, conflicts if any>
   ```

5. **Present result**:
    ```
    Stage 1: AC1 ✅  AC2 ✅  AC3 ⚠️ PARTIAL
    Stage 2: AC4 ✅  AC5 ✅

    status: SUCCESS (4/5)
    session: <session_id>
    ```
   - Keep progress updates terse.
   - Do not run a separate verification pass if executors already reported tests.
   - **SUCCESS**: `📍 Next: ooo evaluate <session_id>`
   - **PARTIAL**: `📍 Next: ooo run again to retry — or ooo unstuck`
   - **FAILED**: `📍 Next: review failures, then ooo run — or ooo unstuck`

### Retry Rule

- Retry the native path once if it fails.
- If it still fails, continue via **Path A Fallback**.

### Path A Fallback: Internal MCP execution

If native subagent spawning is unavailable or fails twice, fall back to the original background execution flow:

1. **Start background execution**:
   ```
   Tool: ouroboros_start_execute_seed
   Arguments:
     seed_content: <yaml>    # or seed_path / seed_id
     cwd: <working directory confirmed in Step 1.5>
     model_tier: "medium"
     max_iterations: 10
   ```
   Returns `job_id`, `session_id`, `execution_id`.

2. **Poll for progress**:
   ```
   loop:
     Tool: ouroboros_job_wait
     Arguments:
       job_id: <job_id>
       cursor: <cursor from previous response, starts at 0>
       timeout_seconds: 60
     # Continue until status is "completed", "failed", or "cancelled"
   ```

3. **Fetch final result**:
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

4. Present result with next-step guidance (same as native path step 7).

**Context minimization**:
- Do not implement code in the main session.
- Prefer `seed_id` over inlining full YAML.
- Do not replay large execution logs into main context.
- Let MCP own orchestration on the fallback path.

## Path B: No MCP Server

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
User: ooo run

Prepared session:orch_x1y2z3 seed:seed_abc ac:5

Stage 1: AC1 ✅  AC2 ✅  AC3 ✅  AC4 ✅  AC5 ✅

status: SUCCESS  5/5 AC met
📍 Next: ooo evaluate orch_x1y2z3
```
