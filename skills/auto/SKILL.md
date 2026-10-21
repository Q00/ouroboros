---
name: auto
description: "Use when the work spans several files or replaces a framework, language, auth, storage, or API layer (rewrites and migrations included), and the user wants it to end up working and tested. Asks only what is missing, writes a spec with acceptance criteria, then runs and verifies it in the background. Not for questions, small edits, or an existing Seed (use run)."
mcp_tool: ouroboros_start_auto
mcp_args:
  goal: "$goal"
  resume: "$resume"
  cwd: "$CWD"
  max_interview_rounds: "$max_interview_rounds"
  max_repair_rounds: "$max_repair_rounds"
  skip_run: "$skip_run"
  pipeline_timeout_seconds: "$pipeline_timeout_seconds"
  efficiency_mode: "$efficiency_mode"
  frugality_assurance: "$frugality_assurance"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Dispatch requirement

This skill must be executed by invoking MCP tool `ouroboros_start_auto`. Do not
manually inspect repositories, run shell commands, query GitHub, edit files, or
otherwise emulate the auto pipeline. The background starter returns `job_id`
and `auto_session_id` quickly; retain both. If `ouroboros_start_auto` or a
required job wait/result tool is unavailable, stop and report that the required
MCP tool is unavailable. A manual fallback is not an `ooo auto` run.

If a started auto job returns `detached`, `blocked`, `failed`, or another
auto-session status, report that status and the tool's blocker. `detached` is
non-terminal tracked background work: surface the job/Ralph handles and keep
observing them through the same owner. Do not label a `blocked` or `failed`
outcome as MCP dispatch failure; dispatch failure means the MCP tool could not
be invoked.

Do not stop after returning the `job_id`: keep the conversational UX, delegate
monitoring (below), and never tell the user to call job tools themselves. The
user should not have to poll the job manually.

Immediately before each `ouroboros_*` call, load its schema with the active runtime's tool-discovery capability (for example `"+ouroboros auto"` for `ouroboros_start_auto` and `"+ouroboros session signal"` for the Synapse tools); a schema loaded on an earlier turn may be gone. An empty result for an already-callable tool is expected.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
ooo auto --resume auto_abc123
ooo auto "Build a local-first habit tracker CLI" --skip-run
/ouroboros:auto "Build a local-first habit tracker CLI"
```

`ooo auto` takes no parent Seed and infers no lineage from goal prose (a Seed
named in the goal is ordinary context); never claim preserved lineage or AC
edit scope. Use `ooo evolve` for parent evolution or `ooo run` for an existing
Seed.

## CLI flag → MCP arg translation

Translate CLI-style flags typed in chat before invoking `ouroboros_start_auto`:

| CLI flag | MCP arg | Type |
|----------|---------|------|
| `--skip-run` | `skip_run=true` | boolean |
| `--max-interview-rounds N` | `max_interview_rounds=N` | integer |
| `--max-repair-rounds N` | `max_repair_rounds=N` | integer |
| `--pipeline-timeout-seconds X` | `pipeline_timeout_seconds=X` | number |
| `--efficiency-mode adaptive\|quality_first` | `efficiency_mode=<value>` | string |
| `--frugality-assurance off\|observe\|strict` | `frugality_assurance=<value>` | string |
| `--resume <id>` | `resume=<id>` | string |

- `--max-generations` belongs to `ooo ralph`, not `ooo auto`.
- `--complete-product` is deprecated and ignored (the run job owns the chain).
- `--pipeline-timeout-seconds` is accepted only on a fresh start; with `--resume` it is rejected because the original deadline is preserved.
- The CLI-only `--show-ledger` flag prints assumptions/non-goals; MCP responses already include that ledger summary when available.

Before a fresh start, do not ask about an efficiency policy unless the user
raised it or `execution.default_policy` in `~/.ouroboros/config.yaml` is
explicitly `ask`. Otherwise omit both arguments: the server applies the
configured default (quality-first when nothing is configured), and its start
receipt names the running policy and the change command; relay that line once.
If the user did choose, or the config says `ask`, map the choice in outcome
language: **Efficient execution** is `adaptive/observe`; **Quality-first
execution** is `quality_first/off`. `strict` assurance is a separate explicit
opt-in because it may spend extra work on proof; never infer it. On resume,
send neither argument; Auto restores the persisted contract.

## Behavior

1. Starts an auto session and runs bounded Socratic interview rounds with source-tagged auto answers.
2. Generates a Seed, then reviews and repairs it until it is A-grade (passes the Seed review) or blocked.
3. Starts execution only after A-grade (unless `skip_run`). Auto reaches `COMPLETE` once the run has a durable handle; the run job then carries its own `run → evaluate → ralph` chain under `execution.auto_evaluate` / `execution.auto_evolve` (both default `true`). Auto does not evaluate the run itself.
4. Every loop is bounded and timeouts return a resumable `auto_session_id`: resume with `ooo auto --resume <auto_session_id>`. The chained Ralph is bounded by `execution.auto_evolve_max_generations` (default 3), each generation capped by Ralph's per-iteration timeout, but there is no total wall-clock cap on the chain, and the auto deadline (`--pipeline-timeout-seconds`, CLI `--timeout`) does not bound it.

**Preflight blocker.** `seed_preflight_unexecutable` is a start-new-session
boundary, not a `--resume` boundary: the session's Seed is immutable, so use
the displayed questions to revise the goal/Seed contract and start a new
`ooo auto` session (the blocked one stays for audit). Transient interview,
evaluator, or lateral-tool exhaustion is different: resume the same session
once the provider is healthy.

## Background monitoring

1. **Hand off.** Relay the server's start receipt (handles, runtime/harness,
   LLM backend, efficiency and frugality settings). Show
   `response.meta.dashboard_url` when present, otherwise mention
   `ouroboros tui open` once. Say this conversation stays available for
   refinement, read-only review, explicit control, or unrelated work in an
   isolated worktree; check for overlap with worker files before writing to
   the active auto workspace.

2. **Delegate to exactly one observer.** The start result carries
   `response.meta.job_observer`, a read-only observer contract. If the
   structured meta is missing, recover it from the final
   `<!-- ouroboros-job-observer-v1 base64 ... -->` content sentinel and fail
   closed unless all of these hold: exactly one sentinel, at the end of the
   text; the bounded payload decodes to an object whose only key is
   `job_observer`; that contract passes canonical v1 validation (it equals,
   field for field, the v1 contract the server builds from its own `job_id`,
   `cursor`, `session_id`, `execution_id`, and `follow_result_job_keys`, so a
   changed tool, argument, or instruction, or an extra field, fails); its
   job/session identity matches the visible start receipt; and it does not
   differ from any structured contract. Never rebuild the contract from
   visible IDs.

   With a valid contract and an independent child-session primitive, spawn
   exactly one observer through the active runtime's `orchestrate_subagents`
   capability and pass the contract unchanged. It owns the cursor, waits until
   terminal, fetches the result, and follows the IDs named by
   `follow_result_job_keys`; it must not edit files, control execution, or spawn
   workers. Claim an observer only after the spawn returns a live child ID. The
   main session must not poll the same job.

   - A `wait` call or a parent job-status poll is not a spawn. The contract's
     `host_lifecycle` and the runtime's `orchestrate_subagents` guidance name
     the concrete spawn and wait mechanics. Once acknowledged, if the runtime
     cannot wake a parent turn that already ended when the child posts (a child
     message only queues for the parent), keep the parent turn open with the
     host's child-wait primitive in waits of at most 60 seconds. Relay
     meaningful updates and wait again until the terminal summary. User input
     may interrupt the wait; handle it and resume unless the user asks to stop
     live observation or replaces the request. Then end only the relay loop,
     keep the durable job running, and offer next-turn or explicit-status
     catch-up. If the observer child fails, is cancelled, or exits early, use
     that same catch-up instead of waiting indefinitely. The relay loop must not
     poll job tools or take cursor ownership.

   If spawn fails, do not promise live relays. The detached worker continues
   after the stdio turn; say durable progress will be caught up next turn or on
   an explicit status request.

   If `response.meta.status == "delegated_to_plugin"` and `job_id` is None,
   plugin mode delegated the work to a child session: follow that session's
   lifecycle and do not call job tools without a real job id.

3. **Relay events** in at most 1-2 lines, suppressing heartbeats, raw tool
   output, commands, and reasoning. `run_configuration` reports
   runtime/harness/model policy; `execution_plan` reports total ACs,
   dependency/parallel levels, and the first scheduled ACs;
   `discovery_summary` reports bounded targets and purpose; other execution
   subtypes (`ac_routing`, `ac_attempt_judged`, `ac_accepted`/`ac_rejected`,
   level and harness changes) only when material. For Synapse events,
   distinguish `queued`/`delivering` from `applied`/`completed`, and surface
   `rejected`/`delivery_uncertain` and `attention_required` at once. English
   is the canonical instruction language; render facts naturally in the
   user's current conversation language.

4. **Interview Q&A.** While the phase is `interview` (for example
   `interview round N/50`), call
   `ouroboros_session_status(session_id=<auto_session_id>)` and relay the
   current `meta.pending_question` plus newly answered `meta.auto_answer_log`
   entries (`{round, source, question, answer}`; `source` is
   `conservative_default`, `inference`, or `assumption`), not a bare counter.
   This Q&A lives in the auto-session state, so `ouroboros_query_events`
   returns nothing for it; status shows only the last 3 answers, truncated.

5. **Fallback.** Only when no child session exists and the user asked to keep
   watching this turn, loop on
   `ouroboros_job_wait(job_id=<job_id>, cursor=<cursor>, timeout_seconds=180, view="summary", stream="linked", wait_for="attention_or_ac_change")`,
   replacing the cursor from `response.meta.cursor` each time, treating
   `response.meta` as the source of truth, and staying silent when
   `response.meta.changed` is false. Otherwise end the turn and resume from the
   durable cursor next interaction. Keep waiting while the status is
   non-terminal.

6. **Result.** At terminal status the polling owner calls
   `ouroboros_job_result(job_id)` and summarizes the auto-session outcome. A
   `detached` result is not completion: keep tracking its downstream job/Ralph
   handles. Report `stop_reason_code` (or the free-form `last_error` blocker)
   verbatim. For `seed_ready`, summarize `interview_closure_mode` (`None` =
   backend and ledger agreed; `ledger_only` or `safe_default` = closed at the
   round cap, with `defaulted_sections`) and the `assumption_sources` entries
   (each with `text`, `source`, `confidence`; broader than `assumptions`).

## Synapse intent refinement

Synapse is the `ouroboros_session_signal` channel that delivers bounded user
intent to one running AC. When the user adds implementation intent during an
executing auto run, call `ouroboros_session_signal_targets` with the observed
`execution_id`, select the AC whose `ac_content` and current activity match,
and send `ouroboros_session_signal` with that target's exact IDs. Never ask the
user for internal IDs; ask a short clarification only when live ACs remain
genuinely tied. Send additive refinements with `contract_effect="additive"`,
`source="user"`, `mode="redirect"`, and `fallback_mode="after_turn"`. For a
read-only AC question or assurance request use `mode="inform"`, omit
`fallback_mode`, and relay the bounded reply from the completed event. Never
route shared goal/AC/constraint changes to one worker.

## Active Conductor decision policy

For `attention_required`, treat `recommended_host_actions` as authoritative:

1. VERIFY with at most one short-lived read-only host child. If unavailable,
   surface the evidence and stop before mutation.
2. DECIDE from the ordered menu only after engine ownership is `closed`.
3. LOG `selected` with `ouroboros_record_conductor_decision` before ACT.
4. ACT only a menu-listed registered tool. Auto may start at most one bounded
   deterministic, non-relaxing successor for that attention event and must pass
   the audited directive/decision/predecessor receipts exactly.
5. LOG exactly one terminal `completed`, `failed`, or `declined` result. Do not
   silently retry. Escalate any specification-changing proposal to the user;
   Auto never relaxes the approved goal, ACs, constraints, or non-goals itself.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
