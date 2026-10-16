---
name: ouroboros-run
description: "Use when an Ouroboros Seed (a spec file with a goal and acceptance criteria, such as *.seed.yaml) already exists and the user wants it executed: 'run the seed', 'execute this spec', 'ooo run'. Executes it and verifies every acceptance criterion. Not for raw requests without a Seed (use interview or auto)."
aliases: [execute]
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
---

# /ouroboros:ouroboros-run

Execute a Seed specification through the Ouroboros workflow engine as a background job. A completed run enqueues formal evaluation by default, and a rejection continues through a bounded Ralph loop.

## Usage

```
/ouroboros:ouroboros-run [seed_file_or_content]
```

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros execute"` for `ouroboros_start_execute_seed`, `ouroboros_job_wait`, `ouroboros_job_result`, and `ouroboros_ac_tree_hud`; `"+ouroboros session signal"` for `ouroboros_session_signal_targets` and `ouroboros_session_signal`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the **Fallback (No MCP Server)** section only when the tools are genuinely absent.

### Execution Steps

1. **Do not create or switch branches yourself.** For a background execution
   job, `use_worktree` defaults to true: the server runs the task in its own
   worktree and reports `worktree_path` / `worktree_branch` in the job result.
   Tell the user the task was isolated only when those fields are present. A
   `dispatch_mode="plugin"` response delegates to a child session and carries
   no worktree.

2. **Get the Seed.** Read a given file path with the active runtime's `inspect_code` capability, use inline YAML directly, or otherwise take the most recently generated Seed in the conversation.

   Before a fresh start, do not ask about an efficiency policy unless the user
   raised it or `execution.default_policy` in `~/.ouroboros/config.yaml` is
   explicitly `ask`. Otherwise omit both arguments: the server applies the
   configured default (quality-first when nothing is configured), and its start
   receipt names the running policy and the change command. If the user did
   choose, or the config says `ask`, map the choice in outcome language:

   - **Efficient execution**: start parallel/decomposed work economically and
     strengthen the route only when recovery requires it. Send
     `efficiency_mode="adaptive"` and `frugality_assurance="observe"`.
   - **Quality-first execution**: keep child work at the parent starting tier.
     Send `efficiency_mode="quality_first"` and `frugality_assurance="off"`.

   `frugality_assurance="strict"` is a separate explicit opt-in because it may
   spend extra work on proof; never infer it from the efficiency choice. Do not
   ask again on resume: the server restores the persisted policy and rejects a
   resume-time change.

3. **Start background execution:**
   ```
   Tool: ouroboros_start_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     efficiency_mode: <only when chosen: adaptive or quality_first>
     frugality_assurance: <only when chosen: observe, off, or explicit strict>
     session_id: <only when resuming an existing session>
   ```
   Omit `model_tier` by default so the runtime selects automatically. Include
   `model_tier: <user choice>` only when the user explicitly requested a tier.
   The call returns at once with `job_id`, `session_id`, and `execution_id`.

4. **Hand off.** Relay the server's start receipt (IDs, backends, `Efficiency Mode` with any change command, dashboard). Then add:
   - Live view: if `execution.tui_autolaunch: true` (or legacy top-level `tui_autolaunch: true`) is set, run `ouroboros tui open`; otherwise offer it once without blocking. If it prints a manual command (headless, SSH), relay that once.
   - Observation: a confirmed read-only observer (step 5), or durable catch-up mode.
   - This conversation stays available: the user can refine requirements, review code, or work on an unrelated task in an isolated worktree; check for active-worker conflicts before editing this run's workspace.

5. **Delegate observation to exactly one observer.** The start result carries
   `response.meta.job_observer`, a read-only observer contract with its own wait
   and result tools, `instructions`, `restrictions`, and fallback. If the
   structured meta is missing, recover the contract from the final
   `<!-- ouroboros-job-observer-v1 base64 ... -->` content sentinel and fail
   closed: if there is more than one sentinel or it is not at the end of the
   text, the bounded payload does not decode to an object whose only key is
   `job_observer`, that contract fails canonical v1 validation (it must equal,
   field for field, the v1 contract the server builds from its own `job_id`,
   `cursor`, `session_id`, `execution_id`, and `follow_result_job_keys`, so a
   changed tool, argument, or instruction, or an extra field, fails), its
   `job_id` differs from the visible start receipt, or the structured and inline
   contracts differ, do not spawn an observer and report a transport-integrity
   failure. Never rebuild the contract from visible IDs.

   With a valid contract and an independent child-session primitive, spawn
   exactly one observer through the active runtime's `orchestrate_subagents`
   capability and pass the contract unchanged. It owns the job cursor, follows
   the contract (including `follow_result_job_keys` for chained evaluation and
   Ralph), and stays read-only. Claim an observer only after the spawn returns
   a live child ID/path. The main session must not poll the same job while the
   observer owns it. If plugin mode already delegated the whole execution (no
   `job_id`), follow that child's lifecycle instead.

   - On Codex, call `spawn_agent` exactly once with `task_name="run_observer"`
     and the contract unchanged in the child message; a `wait` call is not a
     spawn. After the live child ID returns, keep the parent turn open with
     `wait_agent` calls of at most 60 seconds: a child `send_message` only
     queues a mailbox event and cannot wake a parent turn that already ended.
     Relay meaningful updates and wait again until the terminal summary. User
     input may interrupt the wait; handle it and resume unless the user asks to
     stop live observation or replaces the request. Then end only the relay
     loop, keep the durable job running, and offer next-turn or explicit-status
     catch-up. If the observer child fails, is cancelled, or exits early, use
     that same catch-up instead of waiting indefinitely. The relay loop must not
     poll Ouroboros job tools or touch the observer's cursor.
   - On OMP, submit exactly one native Task child named `RunObserver` with the
     contract unchanged, require its live agent/job ID, and relay through the
     host wait/inbox until terminal. A parent job-status poll is not a spawn.

   If no child can be created, do not claim an observer or promise live
   messages. The detached worker survives the stdio MCP turn: say progress is
   durable and will be caught up on the next turn or an explicit status
   request. Keep the turn open only when the user explicitly asked for live
   watching (step 6).

   **Relaying observer events.** Relay each meaningful event in 1-2 lines;
   suppress unchanged heartbeats, raw tool output, commands, and reasoning.
   - `run_configuration`: runtime/harness, starting model or tier when known,
     efficiency mode, frugality assurance.
   - `execution_plan`: total ACs, dependency/parallel levels, and the first
     scheduled AC summaries.
   - `discovery_summary`: bounded targets and purpose.
   - `level_started` / `level_completed`, `ac_routing` / `harness_changed`
     ("currently running with"), `ac_attempt_judged`: only real changes.
   - `ac_accepted` / `ac_rejected`: the AC's final acceptance and compact
     disposition evidence.
   - `attention_required`: surface immediately (see Active Conductor below).
   - Synapse `.queued` / `.delivering`: pending, not applied yet. `.applied` /
     `.completed`: confirm and relay any bounded AC reply. `.rejected` /
     `.delivery_uncertain`: surface immediately; never claim the AC changed
     course.

   These are English canonical host instructions: render every relay in the
   user's current conversation language, keeping event codes and mode values
   only when exact diagnostics help.

6. **Fallback: live relay in the main session.** Only when there is no
   observer contract or no child primitive, and the user explicitly asked to
   keep watching this turn. Never run it beside a delegated observer;
   otherwise end the turn and catch up from the same cursor next turn.
   ```
   Tool: ouroboros_job_wait
   Arguments:
     job_id: <job_id>
     cursor: <latest cursor, or 0>
     timeout_seconds: 180
     view: "summary"
     stream: "linked"
     wait_for: "attention_or_ac_change"
   ```
   Replace the cursor from `response.meta.cursor` after every call. Treat
   `response.meta` as the source of truth and `response.text` only as a hint.
   If `response.meta.changed` is false, repeat silently; otherwise emit one
   compact line (phase, AC/sub-AC counts, new current-work hint), never raw
   JSON, the full tree, or an ETA. On `completed`, `failed`, `cancelled`, or
   `interrupted`, say so explicitly and go to step 8.

7. **Drill-down only.** Use `ouroboros_ac_tree_hud` when the user asks for
   details or progress looks stuck, never in the routine loop. `view` is
   `"compact"`, `"summary"`, or `"tree"` (add `max_nodes: 30`); treat
   `unchanged cursor=<cursor>` as a silent no-op.
   ```
   Tool: ouroboros_ac_tree_hud
   Arguments:
     session_id: <session_id>
     cursor: <cursor>
     view: "summary"
   ```

8. **Fetch the final result in the polling owner** (the observer by default,
   the main session only on the step 6 fallback):
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```
   Present status, session ID, and summary. Its QA verdict (skippable with
   `skip_qa: true` at start) is not the formal 3-stage evaluator.

   If the result meta has `chained_evaluate_job_id`, the polling owner follows
   that job and fetches its result: render **APPROVED** when
   `final_approved: true`, otherwise not approved with the failed ACs or
   reason. If the evaluate job failed or timed out, keep the run success and
   show `Next: ooo evaluate <session_id>` as the manual retry. If the
   evaluation has `chained_ralph_job_id`, follow it too and report approval or
   the bounded Ralph stop reason; a rejected first evaluation does not make
   the run blocked.

   Without `chained_evaluate_job_id`, show:
   - **PASS**: `Next: ooo evaluate <session_id> for formal 3-stage verification`
   - **REVISE**: the differences/suggestions, then `Next: Fix the issues above, then ooo run to retry -- or ooo unstuck if blocked`
   - **FAIL/ESCALATE**: `Next: Review failures above, then ooo run to retry -- or ooo unstuck if blocked`

### Synapse intent refinement

Synapse is the `ouroboros_session_signal` channel that delivers bounded user intent to one running AC. When the user adds implementation intent during an active run, the main session picks the target; never ask the user for internal AC or session IDs.

1. Call `ouroboros_session_signal_targets(execution_id=...)` with the `execution_id` from the start result or observer contract.
2. Match the user's meaning against each target's `ac_content`, display path, and current HUD activity. Select one active target directly; with several, select only a materially more relevant one, and ask a short clarification only for a genuine tie.
3. Additive implementation refinement: call `ouroboros_session_signal` with the target's exact scope/attempt/execution guards, `contract_effect="additive"`, `source="user"`, `mode="redirect"`, `fallback_mode="after_turn"`, `expected_contract_version` when discovery supplies it, and one stable idempotency key for this user turn. If the target went stale, rediscover once instead of asking for IDs.
4. Read-only question or assurance request: use `mode="inform"` and `contract_effect="additive"`, and omit `fallback_mode` (valid only for redirect). Relay the bounded reply from the completed event.
5. Tell the user which AC was selected and the effective mode; say it was reflected only after `.applied` or `.completed`.

Never use Synapse to change approved goals, ACs, constraints, or non-goals; those need an approved shared successor or replacement contract.

### Active Conductor attention handling

`recommended_host_actions` is the authoritative menu; never invent a mutating tool call.

1. VERIFY with at most one short-lived read-only host child using the supplied evidence IDs. Without a verifier primitive, surface the attention and stop before mutation.
2. DECIDE among the ordered menu actions only after engine-owned retry/routing is closed.
3. LOG `phase="selected"` through `ouroboros_record_conductor_decision` before ACT. A specification change in run mode needs explicit user approval with its receipt bound.
4. ACT only when the menu names a currently registered MCP tool. A corrective successor preserves the approved contract unless the user approved the shared specification change.
5. LOG exactly one terminal `completed`, `failed`, or `declined` outcome. Never silently retry a failed conductor action.

## Fallback (No MCP Server)

Tell the user:

```
Ouroboros MCP server is not configured.
To enable full execution mode, run: /ouroboros:setup

Without MCP, you can still:
- Use /ouroboros:interview for requirement clarification
- Use /ouroboros:seed to generate specifications
- Manually implement the seed specification
```

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
