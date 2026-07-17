---
name: auto
description: "Automatically converge from goal to A-grade Seed and execute it"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Dispatch requirement

### Required native tool set

Before dispatch, inspect the current host tool snapshot for the full set:
`ouroboros_start_auto`, `ouroboros_job_wait`, and `ouroboros_job_result`.
Discovery must finish before any start attempt. The prohibition is explicit:
manual repository work is not an `ooo auto` run and must never be used to
emulate this pipeline.

### Native MCP branch

When the full required native tool set is present, invoke
`ouroboros_start_auto`. Full auto runs routinely exceed interactive MCP
tool-call timeouts, so the background starter is the supported default: it
returns `job_id` and `auto_session_id` quickly. Retain both. When
`response.meta.job_observer` is present, delegate its read-only wait/result
contract to exactly one independent child session. The main session keeps only
start and explicit on-demand status responsibility.

Once `ouroboros_start_auto` has been invoked, CLI fallback is forbidden. This
includes a timeout, disconnect, or ambiguous transport outcome: reconcile the
possible native run through its durable handles and native status surfaces.
Never start a fresh CLI Auto request after an attempted native dispatch.

### Official foreground CLI recovery

Use this branch only when at least one required native tool is demonstrably
absent before any native dispatch attempt in the current host snapshot. This is
an official Auto entrypoint, not manual emulation:

1. Verify `ouroboros auto --help` exposes `--runtime`, `--timeout`,
   `--efficiency-mode`, `--frugality-assurance`, and
   `--codex-recovery`. Fail closed if any required option is absent.
2. Resolve the task's working directory explicitly and launch one retained
   foreground process there. Pass the goal as one argv element; never build a
   shell-concatenated command string.
3. For a fresh run, invoke `ouroboros auto <goal> --runtime codex
   --codex-recovery` and add the translated bounds, `--skip-run`,
   `--complete-product`, `--timeout`, `--efficiency-mode`, and
   `--frugality-assurance` arguments that apply; never add `--no-wait`.
4. Capture the early `auto_session_id=<id>` line. Keep the same foreground
   process until it returns verified terminal success or a non-zero resumable
   blocker.
5. Resume only with `ouroboros auto --resume <auto_session_id>
   --codex-recovery`. Do not pass goal, runtime, preferences,
   timeout, bounds, skip/complete, or other fresh-start options on resume.

If foreground recovery returns detached/nonterminal work, loses job ownership,
or cannot prove execution success, report the resumable blocker and non-zero
outcome. Do not present it as completion.

If a started auto job later returns `detached`, `blocked`, `failed`, or another
auto-session status, report that auto-session status and the tool's blocker.
`detached` is non-terminal tracked background work; surface the job/Ralph
handles and keep observing them through the same owner. Do not label a
`blocked` or `failed` outcome as MCP dispatch failure; dispatch failure means
the MCP tool could not be invoked.

If the active runtime routes `ooo auto` through a background starter such as
`ouroboros_start_auto`, do not stop after returning the `job_id`. Keep ownership
of the conversational UX: retain the returned `job_id`, `auto_session_id`, and
cursor, then delegate monitoring when the host supports child sessions. Only
the fallback path monitors with `ouroboros_job_wait` / `ouroboros_job_status`
in the main session. The user should not have to poll the job manually.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
ooo auto --resume auto_abc123
ooo auto "Build a local-first habit tracker CLI" --skip-run
ooo auto "Build a local-first habit tracker CLI" --complete-product
/ouroboros:auto "Build a local-first habit tracker CLI"
```

## CLI flag → MCP arg translation

When the full required native tool set was discovered and the native MCP branch
was selected, translate CLI-style chat flags to the following
`ouroboros_start_auto` arguments:

| CLI flag | MCP arg | Type |
|----------|---------|------|
| `--complete-product` | `complete_product=true` | boolean |
| `--skip-run` | `skip_run=true` | boolean |
| `--max-interview-rounds N` | `max_interview_rounds=N` | integer |
| `--max-repair-rounds N` | `max_repair_rounds=N` | integer |
| `--pipeline-timeout-seconds X` | `pipeline_timeout_seconds=X` | number |
| `--efficiency-mode adaptive\|quality_first` | `efficiency_mode=<value>` | string |
| `--frugality-assurance off\|observe\|strict` | `frugality_assurance=<value>` | string |
| `--resume <id>` | `resume=<id>` | string |

`--max-generations` is **not** a flag for `ooo auto`; it belongs to `ooo ralph`. When `complete_product=true`, the chained Ralph uses its built-in default (10 generations) bounded by `pipeline_timeout_seconds` or Ralph's own per-iteration / wall-clock budgets.

`--pipeline-timeout-seconds` is accepted only when starting a session. Passing it with `--resume` is rejected because the original deadline is preserved across process restarts.

Before a fresh Auto start, if the user did not already choose an efficiency
policy, ask in outcome language: **Efficient execution** maps to
`adaptive/observe`; **Quality-first execution** maps to `quality_first/off`.
`strict` assurance is a separate explicit opt-in because it may spend extra
work on proof. Never infer strict from the efficiency choice. On resume, do not
ask or send either argument; Auto restores the persisted contract.

## Behavior

1. Starts an auto session.
2. Runs bounded Socratic interview rounds with source-tagged auto answers.
3. Generates a Seed.
4. Reviews and repairs until A-grade or blocked.
5. Starts execution only after A-grade.
6. When `complete_product=true`, chains RUN → RALPH_HANDOFF after a successful run handoff and waits for a terminal Ralph status so a single invocation iterates Ralph until QA passes, convergence, or a budget bound trips. A QA-pass on the executed product completes the auto session; recognized failure modes (`iteration_timeout`, `wall_clock_exhausted`, `oscillation_detected`, `grade_regressing`, `max_generations reached`) block the auto session with the matching `stop_reason` in `last_error` so operators can resume after the cause is addressed.

## Background monitoring UX

When an auto start response includes `response.meta.job_id`:

1. Briefly acknowledge that auto started and keep the handles in local state:
   `job_id`, `auto_session_id` / `session_id`, and `cursor` from `response.meta`
   if present. Show `response.meta.dashboard_url` when available; otherwise
   mention `ouroboros tui open` once as the live view. Tell the user that an
   observer will post meaningful progress/attention/completion events here and
   that this conversation remains available for requirement refinement,
   read-only inspection/review, explicit control, or unrelated isolated work.
   Include the resolved `runtime_backend`, `llm_backend`, `efficiency_mode`, and
   `frugality_assurance` when present. Say that the exact active model and first
   parallel level will be announced from the first configuration/plan events
   rather than guessing them.
2. If `response.meta.job_observer` is present and the host supports independent
   child sessions, spawn exactly one read-only observer and pass the contract
   unchanged. Codex uses explicit native subagent delegation; Claude Code uses
   one Task/Agent child. The observer owns the cursor, waits until terminal,
   fetches the result, and follows downstream IDs named by
   `follow_result_job_keys`. It must not edit files, control execution, or spawn
   implementation workers. The main session must not poll the same job.
   On Codex, call `spawn_agent` exactly once with `task_name="run_observer"`;
   a `wait` call is not a spawn, and the handoff may claim an observer only after
   the spawn result returns a live child ID/path. If spawn fails, do not promise
   live proactive relays. The detached worker continues after the stdio turn;
   say that durable progress will be caught up on the next parent turn or an
   explicit status request. Keep the current turn open only when the user asked
   for live watching.
3. If child-to-parent progress messages are supported, the observer relays only
   meaningful `phase_changed`, `progress_advanced`, `attention_required`, and
   `terminal` events in at most 1-2 lines. During interview, it may use
   `ouroboros_session_status(session_id=<auto_session_id>)` to surface a new
   pending question or newly answered rounds. Otherwise, keep the main session
   available and use on-demand status only when the user asks. Surface
   `attention_required` immediately when a blocker needs human judgment.
   Suppress unchanged heartbeats and raw tool output.
   Interpret execution relays explicitly: `run_configuration` reports current
   runtime/harness/model policy; `execution_plan` reports total ACs, total
   dependency/parallel levels, and first scheduled ACs; `discovery_summary`
   reports bounded targets and purpose; level/routing/harness/verified subtypes
   report only material changes. Never relay raw commands or reasoning.
   Before the main session writes to the active auto workspace, check for overlap
   with worker files or move the unrelated work to an isolated worktree.
4. When no independent child session exists and the user explicitly asks to
   keep watching in this turn, enter the fallback low-noise loop. Otherwise end
   the turn safely and resume from the durable cursor on the next interaction:
   - `ouroboros_job_wait(job_id=<job_id>, cursor=<cursor>, timeout_seconds=120, view="summary", stream="linked", wait_for="attention_or_ac_change")`
   - update `cursor = response.meta.cursor` after every wait/status response
   - treat `response.meta` as the source of truth; use response text only as a
     human-readable hint
5. Relay only meaningful changes: status changes, phase changes, new
   execution/session/lineage handles, progress counters, blocker/error text, or
   a terminal state. If `response.meta.changed is false`, continue silently
   unless the user asked for heartbeat updates.
   Synapse delivery events are meaningful: distinguish `queued`/`delivering` from
   `applied`/`completed`, and surface `rejected`/`delivery_uncertain`
   immediately. Render the relay in the user's current conversation language;
   preserve raw event codes only when exact diagnostics help.
   When the user adds implementation intent during an executing auto run, the
   main session first reloads Synapse schemas with
   `tool discovery query: "+ouroboros session signal"`, then calls
   `ouroboros_session_signal_targets` with the observed
   `execution_id`, selects the semantically matching AC from `ac_content` and
   current activity, then sends `ouroboros_session_signal` with that target's
   exact IDs. Never ask the user for internal IDs. Ask a short clarification only
   when multiple live ACs remain genuinely tied, and never route shared goal/AC/
   constraint changes to one worker.
   Send additive implementation refinements with exact target guards,
   `contract_effect="additive"`, `source="user"`, `mode="redirect"`, and explicit
   `fallback_mode="after_turn"`. Use `mode="inform"` for a read-only AC question
   or assurance request, omit `fallback_mode` entirely in that mode, and relay
   the bounded reply from the completed event.
6. **During the interview phase, surface the live Q&A — not just the round
   counter.** Whenever the relayed phase is `interview` (e.g. progress reads
   `interview round N/50`), call
   `ouroboros_session_status(session_id=<auto_session_id>)` and relay to the
   user: (a) the current `meta.pending_question` (the question the interview is
   asking right now), and (b) the `meta.auto_answer_log` entries — each is
   `{round, source, question, answer}`, i.e. what the auto-answerer answered and
   why (`source`: `conservative_default` = safe-default policy,
   `inference` = model reasoning, `assumption` = auto-answerer fallback). Show
   this so the user sees what the interview is converging on, not a bare
   counter. Note: this Q&A lives in the auto-session state, so
   `session_status` surfaces it even though `ouroboros_query_events` returns
   nothing for the auto session, and it shows only the last 3 answers (each
   truncated). Keep it low-noise: relay the pending question and any newly
   answered rounds, not the same 3 entries every poll.
7. If the job status is non-terminal (`queued`, `running`, or another active
   status), keep waiting. Do not tell the user to call job tools themselves.
8. When the job reaches a terminal status, the polling owner calls
   `ouroboros_job_result(job_id)`
   and summarize the final auto-session outcome. If the final auto result is
   `detached`, keep tracking the surfaced downstream job/Ralph handles when
   available instead of presenting `detached` as completion.
9. If `response.meta.status == "delegated_to_plugin"` and
   `response.meta.job_id is None`, report that OpenCode plugin mode delegated
   the work to the child Task/session. Do not call job wait/result without a
   real job id; follow the host Task widget/session lifecycle.

Use short progress relays; the goal is “I am still watching this for you,” not a
wall of logs.

## Active Conductor decision policy

English is the canonical instruction language; render facts naturally in the
user's current conversation language.

For `attention_required`, treat `recommended_host_actions` as authoritative:

1. VERIFY with at most one short-lived read-only host child. If unavailable,
   surface the evidence and stop before mutation.
2. DECIDE from the ordered menu only after engine ownership is `closed`.
3. LOG `selected` with `ouroboros_record_conductor_decision` before ACT.
4. ACT only a menu-listed registered tool. Auto may start at most one bounded
   deterministic, non-relaxing successor for that attention event and must pass
   the audited directive/decision/predecessor receipts exactly.
5. LOG exactly one terminal `completed`, `failed`, or `declined` result. Do not
   silently retry. Any specification-changing proposal is escalated to the user;
   Auto never relaxes the approved goal, ACs, constraints, or non-goals itself.

### Canonical stop_reason_code taxonomy

| Layer | Code | Surface | Meaning |
|---|---|---|---|
| Interview | `interview_max_rounds_exhausted` | `last_error_code`, `result.stop_reason_code` | Auto interview ran `max_interview_rounds` without ledger+backend mutual closure, no section was safely defaultable, and no partial defaults applied — i.e. genuine deadlock with nothing the policy could close. |
| Interview | `interview_unsafe_gaps_remain` | `last_error_code`, `result.stop_reason_code` | Auto interview ran `max_interview_rounds` with at least one section safely defaultable and at least one section remaining unsafe (e.g. CONFLICTING ledger entry, production/credential context). Partial defaults are rolled back so the persisted transcript and ledger stay aligned; resume can address the unsafe gap and re-run. |
| Interview | `interview_phase_deadline` | `last_error_code`, `result.stop_reason_code` | Interview phase exceeded its per-phase timeout. |
| Ralph | `iteration_timeout` | blocker text + (future) `result.stop_reason_code` | A single Ralph iteration exceeded its per-iteration timeout. |
| Ralph | `wall_clock_exhausted` | blocker text + (future) `result.stop_reason_code` | The Ralph wall-clock budget was exhausted before convergence. |
| Ralph | `oscillation_detected` | blocker text + (future) `result.stop_reason_code` | Ralph oscillated between two grade states without making progress. |
| Ralph | `grade_regressing` | blocker text + (future) `result.stop_reason_code` | A subsequent Ralph generation produced a strictly worse grade than its predecessor. |
| Ralph | `max_generations reached` | blocker text + (future) `result.stop_reason_code` | Ralph hit its configured generation cap before reaching A grade. |

Blockers without a canonical code keep using the free-form ``last_error`` text. Ralph-layer codes are surfaced via blocker text today; their result-envelope promotion is tracked as a follow-up.

### Interview closure mode taxonomy

When `result.status == "seed_ready"`, `result.interview_closure_mode` distinguishes how the interview was closed:

| Value | Meaning |
|---|---|
| `None` | Mutual agreement — both the backend and the ledger declared the seed ready in the same round. The default healthy path. |
| `"ledger_only"` | PR-B1 / #1148: `max_rounds` hit; the ledger was structurally complete but the backend refused to declare closure. The interview closes on ledger-only consensus. Defaulted sections (if any) are tagged in `result.defaulted_sections`. |
| `"safe_default"` | PR-B2: `max_rounds` hit; the safe-default policy successfully filled every remaining required gap with auditable assumptions. Synthesis was pushed back into the persisted transcript so the seed generator sees the same assumptions the ledger records. Defaulted sections are tagged in `result.defaulted_sections`. |

Genuine-deadlock and partial-unsafe outcomes do **not** set `interview_closure_mode`; they reach a `blocked` terminal with the matching `stop_reason_code` above instead.

### Assumption-source provenance (PR-C2 / #1157)

`result.assumptions: tuple[str, ...]` (the existing list of assumption texts) is now accompanied by `result.assumption_sources: tuple[AssumptionRecord, ...]`, where each `AssumptionRecord` is a frozen dataclass with:

| Field | Type | Meaning |
|---|---|---|
| `text` | `str` | The assumption text (same surface as the corresponding `assumptions` entry where present). |
| `source` | `str` | One of `"assumption"` (auto-answerer fallback), `"inference"` (model reasoning), `"conservative_default"` (safe-default policy). These are the three assumption-class `LedgerSource` values that produce `assumption_only_sections`. |
| `confidence` | `float` | Per-entry confidence as recorded by the ledger. |

`assumption_sources` is a *broader* surface than `assumptions` — it includes inference- and conservative-default-class entries that `assumptions` (filtered to `LedgerSource.ASSUMPTION` only) does not surface. Callers wanting to know *which assumptions the system made on the user's behalf* should read `assumption_sources`; callers preserving the older string-only contract continue to read `assumptions`.

The pipeline must not hang indefinitely: all loops are bounded and timeout failures return a resumable `auto_session_id`. Resume with `ooo auto --resume <auto_session_id>`. Use `--skip-run` to stop after the A-grade Seed. Use `--complete-product` to drive the full Interview → Seed → Run → Ralph → Product chain on a single `ooo auto` invocation; the chained Ralph loop honors the same wall-clock deadline as the parent auto session (`--timeout`). The CLI-only `--show-ledger` flag prints assumptions/non-goals; MCP skill responses already include the same ledger summary when available.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
