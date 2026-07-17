---
name: run
description: "Execute a Seed specification through the workflow engine"
aliases: [execute]
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

1. Use the active runtime's tool-discovery capability to find and load the execution MCP tools:
   ```
   tool discovery query: "+ouroboros execute"
   ```
2. The tools will typically be named with prefix `mcp__plugin_ouroboros_ouroboros__` (e.g., `ouroboros_execute_seed`, `ouroboros_session_status`). After runtime tool discovery returns, the tools become callable.
3. If the tools are callable — already exposed, or loaded by discovery — proceed with the steps below. An empty discovery result for already-exposed tools is expected, not a failure. Skip to the **Fallback** section only if they are genuinely absent (no Ouroboros MCP server).

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

**CRITICAL — deferred-schema guard (prevents "Invalid tool parameters"):**
This skill makes execution MCP calls across multiple turns, and each turn runs
in a fresh tool context. A deferred tool's schema loaded on one turn is NOT
guaranteed to still be loaded on the next. If you call any execution `ouroboros_*`
MCP tool while its schema is not loaded in the **current** turn, the runtime
rejects the call with **"Invalid tool parameters"** before it reaches the server.
Therefore: **immediately before EVERY execution MCP call in this skill, re-run
`tool discovery query: "+ouroboros execute"`** to reload the execution tool family,
including `ouroboros_start_execute_seed`, `ouroboros_job_wait`,
`ouroboros_ac_tree_hud`, and `ouroboros_job_result` (idempotent — a no-op when
already loaded). If the load returns no matching tool (and the tool is not already callable — an empty load for an already-exposed tool is an expected no-op, not absence), switch to the documented
fallback instead of retrying the failing call.

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

   Before a fresh start, when no efficiency choice is already known, ask in
   user-outcome language: **Efficient execution** maps to
   `efficiency_mode="adaptive"` plus `frugality_assurance="observe"`;
   **Quality-first execution** maps to `efficiency_mode="quality_first"` plus
   `frugality_assurance="off"`. `strict` assurance is a separate explicit
   opt-in because proof may cost extra. Do not ask or override these values on
   resume; the server restores the persisted contract.

3. **Start background execution** with `ouroboros_start_execute_seed`:
   ```
   Tool: ouroboros_start_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     model_tier: "medium"  (or as specified by user)
     efficiency_mode: <adaptive or quality_first>
     frugality_assurance: <observe, off, or explicit strict>
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

5. **Recommended monitoring stance: delegate one exclusive observer.**

   After IDs are returned, print only this short handoff:

   ```
   Execution started in background.
   Job ID: <job_id>
   Session ID: <session_id>
   Execution ID: <execution_id>
   Live view: <response.meta.dashboard_url, or `ouroboros tui open`>
   Runtime/harness: <response.meta.runtime_backend>
   LLM backend: <response.meta.llm_backend>
   Efficiency: <response.meta.efficiency_mode>
   Frugality assurance: <response.meta.frugality_assurance>

   A read-only observer will report meaningful progress, attention, and terminal
   events here. This conversation remains available for requirement refinement,
   read-only review, explicit control, or unrelated work in an isolated worktree.
   For full details later: `ouroboros_ac_tree_hud(session_id=<session_id>)`
   ```

   When `response.meta.job_observer` is present and an independent Task/Agent
   child is available, spawn exactly one read-only observer and pass the contract
   unchanged. It exclusively owns job wait/result and the cursor. The main
   session must not poll the same job. Before writing to the active workspace,
   check worker overlap or use an isolated worktree.
   Do not claim an observer exists until Task/Agent returns a live child handle.
   On Codex, once `spawn_agent` returns that handle, keep the parent turn open
   with `wait_agent` calls of at most 60 seconds until the observer returns its
   terminal summary. Child `send_message` calls only queue mailbox events and
   cannot revive an ended parent turn. Relay meaningful updates, handle user
   input if it interrupts the wait, and resume waiting while the observer is
   active unless the user asks to stop live observation or replaces the active
   request. Then end only the relay loop, keep the durable job running, and
   offer next-turn or explicit-status catch-up. If the observer child fails, is
   cancelled, or exits before a terminal summary, use that same fallback instead
   of waiting indefinitely. This relay loop must not poll the job or take cursor
   ownership.
   If creation fails, do not promise live proactive relays. The detached worker
   survives the stdio turn; catch up from durable events on the next parent turn
   or explicit status request. Keep the fallback polling loop open only for
   explicit live watching.

6. **Fallback low-token relay loop with `ouroboros_job_wait`.**

   Use this only when no independent observer session exists and the user asked
   for live watching in this turn; otherwise catch up on the next parent turn.
   Never run both.

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
       stream: "linked"
       wait_for: "attention_or_ac_change"

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
     # The metadata field names remain legacy-compatible; relay them to users as Task/Subtask progress.
     message_hint = first non-empty non-metadata line from response.text, or null

     # Build one short relay update from structured fields.
     if status in ["completed", "failed", "cancelled", "interrupted"]:
       print terminal_relay(status, phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total)
       break

     if ac_completed > prev_ac_completed:
       print task_progress_relay(phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total, message_hint)
     elif sub_ac_completed > prev_sub_ac_completed:
       print subtask_progress_relay(phase, ac_completed, ac_total, sub_ac_completed, sub_ac_total, message_hint)
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
     heartbeat. The raw tool may still return legacy text such as
     `job_x | running | AC 3/17`; relay that to users as Task progress.
   - Use `view: "summary"` for normal monitoring. It includes the job message
     plus Task/Subtask counts derived from legacy `ac_completed`/`sub_ac_completed`
     metadata fields.
   - Use `view: "full"` only when the user asks for detailed job status.

   Relay style examples:
   - `In progress: Deliver is at Task 1/3 and Subtask 12/16. Current work is the Subtask 3 regression test.`
   - `Level update: parallel level 1/1 has finished, and Task progress advanced to 3/3.`
   - `Completed: execution finished. Fetching the final job result now.`

   Relay output contract for other harnesses/models:
   - One update should be 1-2 sentences or 1 compact line.
   - Include `phase`, Task completed/total and Subtask completed/total when present.
   - Include the current work hint only if it changes.
   - Never include the full task tree in routine relay output.
   - Never include raw JSON, raw meta dumps, or repeated unchanged cursor lines.
   - Terminal statuses must be explicit: completed, failed, cancelled, or interrupted.

   Interpret `meta.relay_events` as structured user-facing facts:
   - `run_configuration`: current runtime/harness, starting model or tier when
     known, efficiency mode, and frugality assurance.
   - `execution_plan`: total ACs and dependency/parallel levels, whether work is
     parallelizable, and the first scheduled AC summaries.
   - `discovery_summary`: bounded targets and purpose, never raw commands or
     reasoning.
   - `level_started` / `level_completed`, `ac_routing`, `harness_changed`, and
     `ac_verified`: report only meaningful transitions. Say "currently running
     with" because routes can escalate.
   - `attention_required`: surface immediately and follow the verified menu.
   - Synapse `queued`/`delivering` is not application;
     `applied`/`completed` is runtime-proven and may contain a bounded AC reply.

   Phrase the English canonical guidance naturally in the user's current
   conversation language.

   Do not paste the full raw tool output unless the user asks for raw status.
   Do not add speculative ETA unless the tool provides one.

   **Synapse intent refinement:** For additive user intent during a live run,
   reload deferred schemas with
   `tool discovery query: "+ouroboros session signal"`, call
   `ouroboros_session_signal_targets` with the observed `execution_id`, and
   semantically match the user's meaning to `ac_content` and current activity.
   Never ask the user for internal IDs. Use the selected exact target with
   `ouroboros_session_signal(mode="redirect", fallback_mode="after_turn",
   contract_effect="additive", source="user")`, copying the exact execution,
   scope, attempt, and contract-version guards plus a stable idempotency key.
   Use `mode="inform"` for a read-only AC question or assurance request, omit
   `fallback_mode` entirely in that mode, and relay the bounded completed reply.
   Ask only when multiple candidates remain genuinely tied. Distinguish durable
   `queued` from runtime-proven `applied`/`completed`, never change the approved
   Seed contract, and render delivery state in the user's conversation language.

   For `attention_required`, use at most one short-lived read-only verifier.
   Without a verifier primitive, surface the evidence and do not ACT. Otherwise
   VERIFY → DECIDE from `recommended_host_actions` → LOG `selected` with
   `ouroboros_record_conductor_decision` → ACT only a menu-listed registered tool
   → LOG `completed`, `failed`, or `declined`. Run-mode specification changes
   require explicit user approval and a shared successor contract; never inject
   them into one live AC.

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
   # or debugging requires seeing the task/subtask structure.
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
In progress: Deliver is at Task 1/3 and Subtask 12/16.
Current work is finishing the workflow routing Subtask.

[Relay]
Level update: parallel level 1/1 has finished, and Task progress advanced to 3/3.
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
