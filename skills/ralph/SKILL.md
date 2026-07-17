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

# For a plain natural-language request, run `ooo interview` + `ooo seed` first,
# then call the MCP tool with a fresh lineage_id and the validated Seed YAML.
```

**Trigger keywords:** "ralph", "don't stop", "must complete", "until it works", "keep going"

## How It Works

Ralph is owned by the `ouroboros_ralph` MCP tool. In non-plugin runtimes, the
tool starts one background Ralph job, runs repeated `evolve_step` generations
inside that job, and stops only when QA passes, convergence is reached, a
terminal evolution action occurs, cancellation is requested, or
`max_generations` is reached. In OpenCode plugin mode, the MCP tool returns a
`delegated_to_plugin` envelope with `job_id=None`; the bridge plugin dispatches
a child Task session that owns the loop instead of creating a local JobManager
job.

The client skill should not reimplement the loop. Deterministic frontmatter
dispatch is limited to the router's named `--lineage-id` option so raw trailing
text is never treated as lineage identity. Raw natural-language
`ooo ralph "<request>"` input must flow through the validated Seed path before
any mutating Ralph loop starts. Until a lineage id and optional Seed YAML are
prepared, `ouroboros_ralph` returns structured input guidance instead of
starting a job. Once the inputs are prepared, start the MCP-owned Ralph surface
once, then follow either the returned job tools path or the OpenCode Task widget
path.

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

The Ouroboros MCP tools are often registered as deferred tools that must be
explicitly loaded before use. Do this before preparing input or calling Ralph:

1. Use the active runtime's tool-discovery capability to find and load the Ralph/job MCP tools:
   ```
   tool discovery query: "+ouroboros ralph job"
   ```
2. The loaded tools may be exposed under plugin-prefixed names such as
   `mcp__plugin_ouroboros_ouroboros__ouroboros_ralph`. Use the actual tool
   names returned by runtime tool discovery; the bare names below are the canonical MCP
   tool names for documentation.
3. Confirm that `ouroboros_ralph` and the job tools (`ouroboros_job_wait`,
   `ouroboros_job_status`, `ouroboros_job_result`, and
   `ouroboros_cancel_job`) are callable. If the tools are unavailable, stop and
   tell the user that Ralph requires the Ouroboros MCP runtime.

### Ralph Flow

1. **Prepare lineage input**:
   - If the user provides an existing `lineage_id` and explicitly wants to
     continue it, reuse that `lineage_id` and omit `seed_content` unless they
     explicitly provide an updated Seed.
   - If the user provides Seed YAML for a new Ralph run, use it as
     `seed_content` and generate a fresh `lineage_id` for this run. Keep
     `lineage_id` separate from Seed, interview, and session IDs so separate
     Ralph runs over the same Seed do not collide.
   - If the user provides only a plain natural-language request, do not treat
     it as a direct `ooo ralph "<request>"` command, do not freehand Seed YAML,
     and do not pass raw text as `seed_content`. Route through the authoritative
     Seed path first: `ooo interview` to capture requirements, then `ooo seed` /
     `ouroboros_generate_seed` to produce validated Seed YAML with the normal
     ambiguity gate. After Seed generation, call the MCP tool with a fresh
     `lineage_id` and that validated Seed YAML as `seed_content`; do not use the
     raw request text. If an interview/seed session already exists in context,
     reuse that validated Seed output instead of regenerating it.

2. **Start Ralph** by calling `ouroboros_ralph` with:
   - `lineage_id`: existing lineage id for an explicit continuation, otherwise a
     freshly generated stable id for this Ralph run, such as
     `ralph-<short-slug>-<uuid>`; do not use a Seed/interview id by itself
   - `seed_content`: valid Seed YAML for generation 1 when starting a new lineage
   - `execute`: default `true`
   - `parallel`: default `true`
   - `skip_qa`: default `false`
   - `project_dir`: explicit target project directory when known
   - `max_generations`: default `10` unless the user requests a tighter bound

3. **Handle the start response**:
   - If `response.meta.job_id` is present, report it concisely and retain the
     job cursor from `response.meta.cursor`:

     ```
     [Ralph] Started background loop: <job_id>
     Lineage: <lineage_id>
     Live view: <dashboard_url, or `ouroboros tui open`>

     A read-only observer will post meaningful progress, attention, and terminal
     events here. This conversation remains available for other safe work.
     ```

   - If `response.meta.job_observer` is present and the host supports an
     independent child session, spawn exactly one read-only observer and pass
     that contract unchanged. The observer exclusively owns job wait/result
     calls and the cursor. The main session retains only user conversation,
     explicit on-demand status, and cancellation when the user requests it. The
     main session must not poll the same job while the observer is active. It may
     refine requirements, perform read-only review, or work in an unrelated
     isolated worktree; check active-worker conflicts before writing to Ralph's
     workspace.
     On Codex, call `spawn_agent` exactly once with `task_name="run_observer"`;
     `wait` is not a spawn, and do not claim an observer until a live child
     ID/path is returned. Once acknowledged, keep the parent turn open with
     `wait_agent` calls of at most 60 seconds while the observer is active. Child
     `send_message` calls only enqueue mailbox events and cannot revive an ended
     parent turn. Relay meaningful updates and wait again until the terminal
     summary arrives. User input may interrupt the wait; handle it and resume
     waiting while observation remains active unless the user asks to stop live
     observation or replaces the active request. Then end only the relay loop,
     keep the durable job running, and offer next-turn or explicit-status catch-
     up. If the observer child fails, is cancelled, or exits before a terminal
     summary, use that same fallback instead of waiting indefinitely. This relay
     loop must not poll the Ouroboros job or take cursor ownership. If spawn
     fails, do not promise live proactive relays: the detached worker continues
     after the stdio turn, and the main session catches up from durable events
     on the next interaction or explicit status request. Keep the main turn open
     in the fallback polling loop only when the user asked for live watching.

   - If `response.meta.status == "delegated_to_plugin"` and
     `response.meta.job_id is None`, report that OpenCode plugin mode delegated
     the loop to a child Task session. Do not call `ouroboros_job_wait`,
     `ouroboros_job_result`, or `ouroboros_cancel_job` without a job id; follow
     the host Task widget/session lifecycle instead.

4. **Monitor non-plugin job progress in the polling owner** when a `job_id` exists.

   The delegated observer is the default owner. Use the main-session loop below
   only when no independent child session exists and the user explicitly asked
   for live watching; otherwise catch up on the next parent turn. Never run both
   loops:

   - `ouroboros_job_wait(job_id, cursor, timeout_seconds=120, stream="linked", wait_for="attention_or_ac_change")` for long polling;
     after every wait/status response, update `cursor = response.meta.cursor`
   - `ouroboros_job_status(job_id)` for a quick status check
   - `ouroboros_job_result(job_id)` when the job is terminal
   - `ouroboros_cancel_job(job_id)` if the user says stop/cancel

   Observer events are concise: relay phase/progress changes in 1-2 lines,
   surface `attention_required` immediately, present `terminal` as the final
   result, distinguish Synapse `queued` from runtime-proven `applied`, surface
   rejected/uncertain delivery immediately, and suppress unchanged heartbeats or
   raw tool output. Render every relay in the user's current conversation
   language; preserve raw event codes only when exact diagnostics help.
   Interpret structured subtypes: report run configuration, total ACs and
   dependency/parallel levels, first scheduled ACs, bounded Discover targets,
   current model/harness changes, level transitions, and verified AC completion.
   Say "currently running with" because later generations may escalate or switch
   harnesses. Never forward raw commands or model reasoning.

   When the user asks a live AC a read-only question or provides additive intent,
   reload `+ouroboros session signal`, call
   `ouroboros_session_signal_targets` for the observed execution, and select the
   semantically relevant AC without asking for internal IDs. Use
   `mode="inform"` for assurance/questions and omit `fallback_mode` in that
   mode. For implementation refinement use
   `contract_effect="additive"`, `source="user"`, `mode="redirect"`, and explicit
   `fallback_mode="after_turn"` with the exact discovered guards. Shared goal/AC/
   constraint/non-goal changes require an approved shared successor.

5. **On non-plugin job termination**, the polling owner fetches
   `ouroboros_job_result(job_id)` and
   summarize the final job result and next step:
   - Success / convergence: summarize the final generation output, QA verdict,
     and any `worktree_path` / `worktree_branch` returned in job metadata. Do not
     present `ooo evaluate` as an automatic next step for Ralph results: the
     Ralph job contract preserves the evolution `lineage_id`, but it does not
     reliably preserve a separate execution `session_id` for the evaluate
     workflow. If a valid execution `session_id` is explicitly available from a
     separate run result, keep it distinct from the Ralph `lineage_id` and follow
     the `ooo evaluate <session_id>` contract; otherwise state that formal
     evaluation needs a real execution session and should not be invoked from the
     Ralph lineage id alone.
   - Max generations / failure: summarize the stop reason and suggest
     `ooo unstuck`, `ooo interview`, or a narrower Ralph retry
   - Cancelled: confirm cancellation and preserve the job id for later inspection

6. **On OpenCode plugin delegation**, rely on the child Task result as the
   terminal surface. Summarize the Task completion/error state and lineage id; do
   not claim a local Ralph job can be polled or cancelled.

### Active Conductor decision policy

For `attention_required`, use at most one short-lived read-only verifier. If the
host has no verifier primitive, surface the evidence and do not mutate.
Otherwise VERIFY → DECIDE from `recommended_host_actions` → LOG `selected` with
`ouroboros_record_conductor_decision` → ACT only a menu-listed registered tool →
LOG exactly one `completed`, `failed`, or `declined` outcome. Ralph may apply a
conductor directive only to the first and sole bounded successor generation
(`max_generations=1`), and only when it is deterministic and non-relaxing. Never
silently retry or weaken the approved shared contract.

These are English canonical host instructions. Render them naturally in the
user's conversation language.

## Tool Mapping

| Skill action | MCP tool |
| --- | --- |
| Start Ralph loop | `ouroboros_ralph` |
| Wait for progress | `ouroboros_job_wait` |
| Fetch final result | `ouroboros_job_result` |
| Cancel loop | `ouroboros_cancel_job` |
| Inspect current status | `ouroboros_job_status` |

## The Boulder Never Stops

This is the key phrase. Ralph does not give up:

- Each failure is data for the next attempt.
- Verification drives the loop.
- Only success, convergence, terminal failure, cancellation, or max-generation
  limits stop it.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
