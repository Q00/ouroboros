# Ouroboros for Codex

Use Ouroboros commands when the user is asking to clarify requirements, generate a seed, run a seed, inspect workflow status, evaluate an execution, or manage Ouroboros setup.

## CRITICAL: MCP Tool Routing

When the user types `ooo <command>`, you MUST call the corresponding MCP tool.
Do NOT interpret `ooo` commands as natural language. ALWAYS route to the MCP tool.

| User Input | MCP Tool to Call |
|-----------|-----------------|
| `ooo interview "<topic>"` | `ouroboros_interview` with `initial_context` |
| `ooo interview "<answer>"` (follow-up) | `ouroboros_interview` with `answer` and `session_id` |
| `ooo seed [session_id]` | `ouroboros_generate_seed` |
| `ooo run <seed.yaml>` | `ouroboros_start_execute_seed` with `seed_path` |
| `ooo auto ...` | `ouroboros_start_auto` with the resolved `goal` / `resume` / option arguments |
| `ooo status [session_id]` | `ouroboros_session_status` |
| `ooo evaluate <session_id>` | `ouroboros_evaluate` |
| `ooo evolve ...` | `ouroboros_evolve_step` |
| `ooo cancel [execution_id]` | `ouroboros_cancel_execution` |
| `ooo unstuck` / `ooo lateral` | `ouroboros_lateral_think` |

If `ouroboros_start_auto` is unavailable, stop and report that the MCP dispatch surface is broken. Do not manually emulate `ooo auto` with ordinary shell, GitHub, or coding work.

## Natural Language Mapping

For natural-language requests, map to the corresponding MCP tool:
- "clarify requirements", "interview me", "socratic interview" → call `ouroboros_interview`
- "generate a seed", "freeze requirements" → call `ouroboros_generate_seed`
- "run the seed", "execute the workflow" → call `ouroboros_start_execute_seed`
- "check status", "am I drifting?" → call `ouroboros_session_status`
- "evaluate", "verify the result" → call `ouroboros_evaluate`

## Auto Dispatch Safety

`ooo auto` has a strict product contract: bounded interview, Seed generation,
A-grade review/repair, and execution handoff. Do not emulate it with manual
shell, repository, or GitHub work.

If a user input starts with `ooo auto`, call `ouroboros_start_auto`. Full auto
runs routinely exceed interactive MCP tool-call timeouts, so the background
starter is the supported default. It returns `job_id` and `auto_session_id`
quickly; report both briefly, retain the `job_id` plus latest cursor, and keep
monitoring ownership inside the agent UX. When the response includes
`meta.job_observer`, explicitly delegate that object to exactly one native Codex
subagent session. That observer is read-only and exclusively owns
`ouroboros_job_wait`, its cursor, `ouroboros_job_result`, and any downstream job
IDs named by `follow_result_job_keys`. Keep the main session available for the
user; do not poll the same job from both sessions. The main session may perform
an on-demand status check only when the user asks. If native subagents are not
available, use the contract's main-session fallback and relay only meaningful
changes. Do not hand the user polling instructions as the final UX. If that MCP
tool is unavailable, or any required job polling/result MCP tool is unavailable,
stop and report that the MCP dispatch surface is incomplete instead of
continuing as a normal Codex task. Do not emulate the workflow with ordinary
shell or coding work.

For Codex delegation, call the native `spawn_agent` primitive exactly once with
`task_name="run_observer"` and include `meta.job_observer` unchanged in the child
message. A `wait` call does not create an observer. Do not claim delegation or
end the start turn until the spawn result returns a live child ID/path. After a
live child is acknowledged, keep the parent turn open with `wait_agent` while
the observer is active. The observer's `send_message` only queues a parent
mailbox event; it cannot revive a parent turn that already ended. Relay each
meaningful mailbox update, then call `wait_agent` again until the observer sends
its terminal summary. Use waits of at most 60 seconds so user input can steer
the active turn; handle that input and resume waiting while the observer remains
active. This parent relay wait does not call job tools and does not take the
observer's exclusive cursor ownership. If spawn is unavailable or fails, do not
claim live proactive observation. The detached worker survives the stdio turn;
tell the user that the main session will catch up from durable events on their
next message or explicit status request. Keep the current turn open in the
fallback polling loop only when the user explicitly asks for live watching.

Immediately after observer delegation, give one compact handoff to the user:
- show `meta.dashboard_url` when present; otherwise mention that
  `ouroboros tui open` opens the live view;
- state that progress, attention-required, and terminal events will be posted
  back into this conversation;
- state that the main conversation remains available for requirement refinement,
  read-only inspection/review, explicit status or control requests, and unrelated
  work in an isolated worktree.

Treat observer messages as events. Relay `phase_changed` and
`progress_advanced` in at most two concise lines. Surface `attention_required`
immediately when a blocker or pending decision needs the user. Present
`terminal` as the final result. Suppress unchanged heartbeats and raw tool
output. Before editing the active run's workspace from the main session, check
for overlap with worker files or use an isolated worktree.

If `ouroboros_start_auto` is invoked and returns an auto-session outcome such as
`blocked`, `failed`, or `complete`, report that outcome as the auto session
result. `detached` is non-terminal tracked background work; surface the job or
Ralph handles and keep the same observer ownership without blocking the main
session. After the auto job reaches a terminal job status, the observer calls
`ouroboros_job_result(job_id)` and returns a compact final result. Do not call a
`blocked` or `failed` auto-session result a dispatch failure; dispatch failure
is reserved for cases where the MCP tool could not be invoked.

## Active Conductor Host Contract

For a fresh `ooo run` or `ooo auto` start with no remembered choice, ask in
user-outcome language whether to use efficient execution (`adaptive` with
lightweight `observe`) or quality-first execution (`quality_first` with
frugality assurance `off`). `strict` assurance is a separate explicit opt-in
because it may spend extra work on proof. Never change these preferences on
resume; use the persisted server contract.

After start acceptance, immediately tell the user:

- the returned runtime/harness and LLM backend;
- the resolved efficiency and frugality assurance;
- that the exact active model will be reported from routing events when it is
  not yet known;
- that one read-only observer will report meaningful progress, attention, and
  completion while the main conversation remains available.

Interpret observer `relay_events` semantically:

- `run_configuration`: current runtime/harness, model/tier when known, and the
  efficiency/assurance contract;
- `execution_plan`: total ACs, total dependency/parallel levels, whether work is
  parallelizable, and the first scheduled AC summaries;
- `discovery_summary`: bounded targets and purpose only—never raw commands,
  searches, tool output, or model reasoning;
- `level_started` / `level_completed`, `ac_routing`, `harness_changed`, and
  `ac_verified`: report only material changes and say "currently running with"
  because routing can escalate;
- `attention_required`: surface immediately;
- Synapse `queued`/`delivering`: pending or claimed, not applied;
  `applied`/`completed`: runtime-proven and may contain a bounded AC reply;
  `rejected`/`delivery_uncertain`: surface immediately without claiming change.

These are English canonical instructions. Phrase the facts naturally in the
user's current conversation language.

### Ouroboros Synapse

When the user asks about or refines a live AC, reload deferred Synapse schemas,
call `ouroboros_session_signal_targets(execution_id=...)`, and semantically map
the user's wording to `ac_content`, display path, and current activity. Never ask
the user for internal IDs. Select directly when one target is materially more
relevant and ask a short clarification only for a genuine tie.

- For a read-only AC question or assurance request, send `mode="inform"` with
  `contract_effect="additive"`, omit `fallback_mode` entirely, and relay the
  bounded completed reply.
- For additive implementation intent, send exact execution/scope/attempt and
  contract-version guards with `source="user"`, `mode="redirect"`, and explicit
  `fallback_mode="after_turn"`. Use one stable idempotency key for that exact
  user turn.
- Never send a shared goal, AC, constraint, or non-goal change to one worker.
  It requires explicit approval and a shared successor contract.

### Attention decisions

`recommended_host_actions` is authoritative. Use at most one short-lived
read-only Codex subagent to VERIFY the supplied evidence. If verification cannot
be delegated, surface the event and do not mutate. Otherwise DECIDE from the
ordered menu, LOG `selected` via `ouroboros_record_conductor_decision`, ACT only
a menu-listed currently registered tool, then LOG exactly one `completed`,
`failed`, or `declined` outcome. Never duplicate engine retry/routing or silently
retry a failed conductor action. Auto/Ralph may run only bounded deterministic
non-relaxing successors; run-mode specification changes require explicit user
approval.

## Setup & Update

- `ooo setup` → write Ouroboros config (`~/.ouroboros/config.yaml`) and register the MCP server
- `ooo update` → upgrade Ouroboros to the latest PyPI version

If the request is clearly unrelated to Ouroboros, handle it normally.
