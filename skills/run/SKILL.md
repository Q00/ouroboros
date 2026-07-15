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
6. **Verification status**: Run-only results are `executed_unverified` until
   `ooo evaluate <session_id>` performs formal 3-stage verification

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

   Before a fresh start, when the user has not already chosen an efficiency
   policy, ask in outcome language:

   - **Efficient execution** — start parallel/decomposed work economically and
     strengthen the route only when recovery requires it. Send
     `efficiency_mode="adaptive"` and `frugality_assurance="observe"`.
   - **Quality-first execution** — keep child work at the parent starting tier.
     Send `efficiency_mode="quality_first"` and `frugality_assurance="off"`.

   `frugality_assurance="strict"` is a separate explicit opt-in because it may
   spend extra work on proof. Never enable it merely because efficient execution
   was chosen. Do not ask again on resume; the server restores the persisted
   policy and rejects an attempted resume-time change.

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

5. **Recommended monitoring stance: delegate observation to one child session.**

   **TUI surfacing at job start (RFC #1392):**

   After `job_id`, `session_id`, and `execution_id` are returned, surface a live
   view once without delaying execution or observer delegation:

   - If `response.meta.dashboard_url` exists, show it as the primary live view.

   - If `execution.tui_autolaunch: true` (or legacy top-level
     `tui_autolaunch: true`) is present in the loaded Ouroboros config, run
     `ouroboros tui open` unconditionally and mention the dashboard in one
     short line.
   - Otherwise mention once that the TUI can be opened in a new terminal with
     `ouroboros tui open`. Offer to open it, but do not block the run waiting for
     an answer. Remember the answer for this session and do not repeat the offer.
   - If the user accepts, run `ouroboros tui open`.
   - If `ouroboros tui open` reports a manual command because the environment
     is headless, SSH, or unsupported, relay that command once and continue.
   - The dashboard is an external observer. It does not change which chat
     session owns MCP polling.

   After IDs are returned, print only this short handoff:

   ```
   Execution started in background.
   Job ID: <job_id>
   Session ID: <session_id>
   Execution ID: <execution_id>
   Live view: <dashboard_url, or `ouroboros tui open`>
   Runtime/harness: <response.meta.runtime_backend>
   LLM backend: <response.meta.llm_backend>
   Efficiency: <response.meta.efficiency_mode>
   Frugality assurance: <response.meta.frugality_assurance>

   Observation: <confirmed read-only child observer, or durable catch-up mode>.
   With a confirmed observer, meaningful progress, attention, and completion
   events will be posted here. Without one, the run still survives this turn
   and I will catch up from durable events on your next message or status request.
   This conversation stays available while the run continues.
   We can refine requirements, inspect or review code, or work on an unrelated
   task in an isolated worktree. I will check for active-worker conflicts before
   editing this run's workspace.
   For full details later: `ouroboros_ac_tree_hud(session_id=<session_id>)`
   ```

   When `response.meta.job_observer` is present and the host has an independent
   child/subagent session primitive, spawn exactly one observer session and pass
   that object unchanged. Codex requires explicit native subagent delegation;
   Claude Code uses one Task/Agent child. The observer must:

   - On Codex, call the native `spawn_agent` primitive exactly once with
     `task_name="run_observer"` and include `response.meta.job_observer`
     unchanged in the child message. A `wait` call is not a spawn.
   - Require the spawn result to return a live child ID/path before saying an
     observer is connected or before ending the start turn.

   - remain read-only: no repository edits, execution control, or worker fan-out;
   - own the job cursor exclusively and reload deferred MCP schemas immediately
     before each observer tool call;
   - call the declared `wait.tool` with the declared arguments, update its local
     cursor from response meta, and repeat until terminal;
   - call the declared `result.tool` after terminal status;
   - follow any job IDs named by `follow_result_job_keys`, including chained
     formal evaluation, before returning one compact final summary;
   - send sparse progress notices only when the host supports child-to-parent
     messages and the state meaningfully changes.

   The main session must not poll the same job while the observer owns it. It may
   continue the user conversation, refine requirements, perform read-only
   inspection/review, handle explicit status/control requests, or work on an
   unrelated task in an isolated worktree. Before writing to the active run's
   workspace, check for overlap with worker files or isolate the work. If
   `job_id` is absent because plugin mode already delegated the whole execution,
   follow that plugin child lifecycle instead.

   Handle observer messages as events, not as a transcript:

   - `phase_changed` / `progress_advanced`: relay at most 1-2 concise lines.
     Interpret the structured subtype, not raw logs:
     - `run_configuration`: state the current runtime/harness, starting model or
       tier when known, efficiency mode, and frugality assurance. If the exact
       model is not known yet, say it will be reported by the first routing event.
     - `execution_plan`: state total ACs, total dependency/parallel levels,
       whether work can run in parallel, and the first scheduled AC summaries.
     - `discovery_summary`: say which bounded targets the AC is examining and
       the purpose; never expose search queries, raw commands, or reasoning.
     - `level_started` / `level_completed`: say which parallel level is active
       or finished and the meaningful success/failure counts.
     - `ac_routing` / `harness_changed`: say "currently running with" and report
       only initial routing or a real model/tier/harness change.
     - `ac_verified`: report the completed AC and its compact assurance evidence.
   - `attention_required`: surface the blocker or pending decision immediately
     and ask the user only when human judgment is required.
   - `terminal`: fetch/present the final result and any chained evaluation.
   - Synapse `.queued` / `.delivering`: say the exact AC has a pending or claimed
     intent signal and name
     the effective boundary; do not claim application yet.
   - Synapse `.applied` / `.completed`: confirm runtime-proven application and
     relay the bounded AC reply when present.
   - Synapse `.rejected` / `.delivery_uncertain`: surface immediately and never
     claim the AC changed course.
   - Suppress unchanged heartbeats and raw tool output.

   Render every relay in the user's current conversation language. Keep event
   codes and effective-mode values unchanged only when exact diagnostics help.
   These are English canonical host instructions. Phrase the facts naturally in
   the active conversation language.

   This ownership split is the default for SOL-class models: the main model
   performs one start handoff, while a small isolated context owns the repetitive
   wait/result state machine.

   If child creation is unavailable, fails, or returns no live child, do not
   claim that an observer exists or promise live proactive messages. The
   detached worker survives the stdio MCP turn. Tell the user that progress is
   durable and will be caught up on the next parent turn or explicit status
   request. Keep the turn open only when the user explicitly asked for live
   watching; then use the fallback below.

6. **Fallback: low-token relay loop in the main session.**

   Use this only when `response.meta.job_observer` is absent, the host has no
   independent child session primitive, and the user explicitly asked to keep
   watching in the current turn. Do not run it in parallel with a delegated
   observer. Otherwise end the turn safely and catch up from the same cursor on
   the next parent turn.

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

   Do not paste the full raw tool output unless the user asks for raw status.
   Do not add speculative ETA unless the tool provides one.

   **Synapse intent refinement:** When the user gives additive implementation
   intent during an active run, the main session owns target selection; never ask
   the user for internal AC/session IDs.

   Immediately before each Synapse call, reload its deferred schemas with
   `tool discovery query: "+ouroboros session signal"`.

   1. Take `execution_id` from the start result or observer contract and call
      `ouroboros_session_signal_targets(execution_id=...)`.
   2. Match the user's meaning against each target's `ac_content`, display path,
      and current HUD activity when needed. One active target may be selected
      directly. With multiple targets, select only when one is materially more
      relevant; ask a short user-language clarification only for genuine ties.
   3. For additive implementation refinement, call `ouroboros_session_signal`
      with the selected target's exact scope/attempt/execution guards,
      `contract_effect="additive"`, `source="user"`, `mode="redirect"`, and
      `fallback_mode="after_turn"`. Copy `expected_contract_version` when target
      discovery supplies it, and create one stable idempotency key for this exact
      user turn. If the target went stale, rediscover once instead of asking for
      IDs.
   4. When the user asks the AC a read-only question or requests assurance rather
      than an implementation change, use `mode="inform"` and
      `contract_effect="additive"`, and omit `fallback_mode` entirely because it
      is valid only for redirect. Synapse runs a no-tools reply turn when the
      runtime supports it; relay the bounded reply from the completed event.
   5. Tell the user which AC was selected and report the returned effective mode.
      Wait for observer `.applied` or `.completed` before saying it was reflected.

   Never use Synapse to change approved goals, ACs, constraints, or non-goals;
   those require an approved shared successor or replacement contract.

   **Active Conductor attention handling:** `recommended_host_actions` is the
   authoritative menu. Never invent a mutating tool call.

   1. VERIFY with at most one short-lived read-only host child using the supplied
      evidence IDs. If the host has no verifier primitive, surface the attention
      and stop before mutation.
   2. DECIDE among the ordered menu actions. Engine-owned retry/routing must be
      closed before any successor action is considered.
   3. LOG `phase="selected"` through
      `ouroboros_record_conductor_decision` before ACT. For a specification
      change in run mode, obtain explicit user approval and bind its receipt.
   4. ACT only when the menu names a currently registered MCP tool. A corrective
      successor must preserve the approved contract unless the user approved the
      shared specification change.
   5. LOG exactly one terminal `completed`, `failed`, or `declined` outcome. Do
      not silently retry a failed conductor action.

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

8. **Fetch final result in the polling owner** with `ouroboros_job_result`.

   The delegated observer performs this call on the default path. The main
   session performs it only on the fallback path from step 6.
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

9. Present the execution results to the user:
   - Show success/failure status
   - Show session ID (for later status checks)
   - Show execution summary

10. **Post-execution QA and formal evaluation** (automatic):
   `ouroboros_start_execute_seed` automatically runs QA after successful execution.
   The QA verdict is included in the final job result text. This QA check is
   **not** the formal 3-stage evaluator. On servers that return
   `chained_evaluate_job_id`, the successful run has already enqueued the formal
   evaluator as a separate bounded background job.
   To skip: pass `skip_qa: true` to the tool.

   If the final run result meta contains `chained_evaluate_job_id`:
   - The current polling owner continues with that job ID
   - Fetch its verdict with `ouroboros_job_result` after terminal status
   - Render **APPROVED** when `final_approved: true`; otherwise render not approved and list failed ACs or the failure reason from the evaluation result
   - If the evaluate job failed or timed out, keep the run success intact and show `Next: ooo evaluate <session_id>` as the manual retry

   If `chained_evaluate_job_id` is absent, keep the legacy path verbatim:
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
  Verification Status: executed_unverified
  Formal Evaluation: NOT evaluated by the 3-stage evaluator

  Next: `ooo evaluate orch_x1y2z3` for formal 3-stage verification

  # Newer server path:
  Verification Status: evaluation_enqueued
  Chained Evaluation Job ID: job_eval987
  [Poll ouroboros_job_wait/job_status, then fetch ouroboros_job_result]
  Formal Evaluation Verdict: APPROVED
```

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
