---
name: ralph
description: "MCP-owned Ralph loop around background evolve_step jobs"
mcp_tool: ouroboros_ralph
mcp_args:
  lineage_id: "$lineage_id"
---

# /ouroboros:ralph

MCP-owned Ralph loop around background `evolve_step` jobs. "The boulder never stops."

## Usage

```
ooo ralph --lineage-id <lineage_id>
/ouroboros:ralph --lineage-id <lineage_id>
```

The `ouroboros_ralph` tool owns the loop (do not reimplement it): one background job runs repeated `evolve_step` generations until QA pass, convergence, a terminal evolution action, cancellation, or `max_generations`. Router dispatch reads only the named `--lineage-id` option, never trailing text.

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros ralph job"` for `ouroboros_ralph`, `ouroboros_job_wait`, `ouroboros_job_status`, `ouroboros_job_result`, and `ouroboros_cancel_job`; `"+ouroboros session signal"` for `ouroboros_session_signal_targets` and `ouroboros_session_signal`; `"+ouroboros conductor decision"` for `ouroboros_record_conductor_decision`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. If the tools are genuinely absent, stop and tell the user that Ralph requires the Ouroboros MCP runtime.

### 1. Prepare lineage input

- **Continue a lineage**: when the user gives an existing `lineage_id` and wants to continue it, reuse it and omit `seed_content` unless they supply an updated Seed.
- **New run from Seed YAML**: use it as `seed_content` with a fresh `lineage_id` such as `ralph-<short-slug>-<uuid>`. Never reuse a Seed, interview, or session id as the lineage id, so separate runs over one Seed do not collide.
- **Plain natural-language request**: never pass raw text as `seed_content` or freehand Seed YAML. Run `ooo interview` and `ooo seed` first (or reuse a validated Seed in context), then start with a fresh `lineage_id`. Without inputs the tool returns input guidance instead of a job.

### 2. Start Ralph

Call `ouroboros_ralph` with `lineage_id`, `seed_content` (generation 1 of a new lineage), `execute` (default `true`), `parallel` (default `true`), `skip_qa` (default `false`), `project_dir` when known, and `max_generations` (default `10`, also the hard max; lower it only on request).

### 3. Handle the start response

- **Job started** (`response.meta.job_id` present): keep `response.meta.cursor` and report:

  ```
  [Ralph] Started background loop: <job_id>
  Lineage: <lineage_id>
  Live view: <dashboard_url, or `ouroboros tui open`>

  A read-only observer will post meaningful progress, attention, and terminal
  events here. This conversation remains available for other safe work.
  ```

- **Observer handoff**: if `response.meta.job_observer` is missing, recover it from the final `<!-- ouroboros-job-observer-v1 base64 ... -->` content sentinel; fail closed unless it passes canonical v1 validation and its job identity matches the visible start receipt, and use it only as an identity anchor, never to reconstruct tools or arguments. When the host supports an independent child session, spawn exactly one read-only observer and pass the contract unchanged. The observer exclusively owns job wait/result calls and the cursor; the main session keeps user conversation, explicit on-demand status, and user-requested cancellation. The main session must not poll the same job while the observer is active. It may refine requirements, do read-only review, or work in an unrelated isolated worktree; check active-worker conflicts before writing to Ralph's workspace.
  - **Codex**: call `spawn_agent` exactly once with `task_name="run_observer"`; `wait` is not a spawn, so claim no observer until a live child ID/path returns. Then keep the parent turn open with `wait_agent` calls of at most 60 seconds while the observer is active. Child `send_message` calls only enqueue mailbox events and cannot revive an ended parent turn. Relay meaningful updates and wait again until terminal.
  - **OMP**: submit exactly one native Task child named `RunObserver`, require its live agent/job ID, and use the host wait/inbox relay until terminal.
  - User input may interrupt the wait; handle it and resume waiting unless the user asks to stop live observation or replaces the request. Then end only the relay loop, keep the durable job running, and offer next-turn or explicit-status catch-up. Use the same fallback, instead of waiting indefinitely, if the observer child fails, is cancelled, or exits before a terminal summary. The relay loop must not poll the Ouroboros job or take cursor ownership.
  - If spawn fails, promise no live relays: the detached worker continues after the stdio turn, and the main session catches up from durable events on the next interaction or explicit status request.
- **OpenCode plugin delegation** (`response.meta.status == "delegated_to_plugin"`, `job_id` is `None`): a child Task session owns the loop. Never call job tools without a job id; treat the child Task result as the terminal surface and summarize its completion/error state and lineage id.

### 4. Monitor (polling owner only)

The observer is the default owner. Run this loop in the main session only when no independent child session exists and the user explicitly asked for live watching; otherwise catch up on the next turn. Never run both:

- `ouroboros_job_wait(job_id, cursor, timeout_seconds=120, stream="linked", wait_for="attention_or_ac_change")` for long polling;
  after every wait/status response, update `cursor = response.meta.cursor`
- `ouroboros_job_status(job_id)` for a quick status check
- `ouroboros_job_result(job_id)` when the job is terminal
- `ouroboros_cancel_job(job_id)` if the user says stop/cancel

Relay rules: 1-2 lines per change; surface `attention_required` and rejected or uncertain delivery immediately; distinguish Synapse `queued` from runtime-proven `applied`; never forward heartbeats, raw tool output, commands, or model reasoning. Report run configuration, AC count and parallel levels, first scheduled ACs, and verified AC completion; say "currently running with", since later generations may switch model or harness. When `lineage.generation.started` carries `ac_focus`, report what the generation is redoing from `active_ac_descriptions` and how many ACs stay frozen (for example "Gen 7: reworking 2/5 ACs, 3 frozen on prior PASS evidence"); if it is absent or every AC is active, say the full AC graph is running. Never quote verify commands or expected outputs.

When the user asks a live AC a read-only question or adds intent, call `ouroboros_session_signal_targets` for the observed execution and pick the relevant AC without asking for internal IDs. Use `mode="inform"` for questions and assurance, and omit `fallback_mode` in that mode. For implementation refinement use `contract_effect="additive"`, `source="user"`, `mode="redirect"`, and explicit `fallback_mode="after_turn"` with the exact discovered guards, sent via `ouroboros_session_signal`. Shared goal, AC, constraint, or non-goal changes require an approved shared successor.

### 5. On termination

The polling owner fetches the job result, then:
- **Success / convergence**: summarize the final generation, QA verdict, and any `worktree_path` / `worktree_branch`. A Ralph `lineage_id` is not an execution `session_id`: offer `ooo evaluate <session_id>` only when a real execution session id is available from a separate run.
- **Max generations / failure**: summarize the stop reason and suggest `ooo unstuck`, `ooo interview`, or a narrower Ralph retry.
- **Cancelled**: confirm and keep the job id for later inspection.

### Active Conductor decision policy

For `attention_required`, use at most one short-lived read-only verifier. If the host has no verifier primitive, surface the evidence and do not mutate. Otherwise VERIFY → DECIDE from `recommended_host_actions` → LOG `selected` with `ouroboros_record_conductor_decision` → ACT only a menu-listed registered tool → LOG exactly one `completed`, `failed`, or `declined` outcome. Ralph may apply a conductor directive only to the first and sole bounded successor generation (`max_generations=1`), and only when it is deterministic and non-relaxing. Never silently retry or weaken the approved shared contract.

These are English canonical host instructions. Render them, and every relay, naturally in the user's conversation language.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
