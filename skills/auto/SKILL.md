---
name: auto
description: "Automatically converge from goal to A-grade Seed and execute it"
mcp_tool: ouroboros_start_auto
mcp_args:
  goal: "$goal"
  resume: "$resume"
  cwd: "$CWD"
  max_interview_rounds: "$max_interview_rounds"
  max_repair_rounds: "$max_repair_rounds"
  skip_run: "$skip_run"
  complete_product: "$complete_product"
  pipeline_timeout_seconds: "$pipeline_timeout_seconds"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Dispatch requirement

This skill must be executed by invoking MCP tool `ouroboros_start_auto`. Do not
manually inspect repositories, run shell commands, query GitHub, edit files, or
otherwise emulate the auto pipeline as a substitute. Full auto runs routinely
exceed interactive MCP tool-call timeouts, so the background starter is the
supported default: it returns `job_id` and `auto_session_id` quickly. Retain
both, poll progress with `ouroboros_job_wait` / `ouroboros_job_status`, and read
terminal results with `ouroboros_job_result`.

If `ouroboros_start_auto` is unavailable, or if any required job polling/result
MCP tool is unavailable, stop and report that the required MCP tool is
unavailable. A manual fallback is not an `ooo auto` run.

If a started auto job later returns `detached`, `blocked`, `failed`, or another
auto-session status, report that auto-session status and the tool's blocker.
`detached` is non-terminal tracked background work; surface the job/Ralph
handles and keep polling. Do not label a `blocked` or `failed` outcome as MCP
dispatch failure; dispatch failure means the MCP tool could not be invoked.

If the active runtime routes `ooo auto` through a background starter such as
`ouroboros_start_auto`, do not stop after returning the `job_id`. Keep ownership
of the conversational UX: retain the returned `job_id`, `auto_session_id`, and
cursor, then monitor the job with `ouroboros_job_wait` / `ouroboros_job_status`
until a terminal job status is reached or the user explicitly asks you to stop.
The user should not have to poll the job manually.

## Usage

```text
ooo auto "Build a local-first habit tracker CLI"
ooo auto --resume auto_abc123
ooo auto "Build a local-first habit tracker CLI" --skip-run
ooo auto "Build a local-first habit tracker CLI" --complete-product
/ouroboros:auto "Build a local-first habit tracker CLI"
```

## CLI flag → MCP arg translation

When the user types `ooo auto` with CLI-style flags inside chat, translate to MCP arguments before invoking `ouroboros_start_auto`:

| CLI flag | MCP arg | Type |
|----------|---------|------|
| `--complete-product` | `complete_product=true` | boolean |
| `--skip-run` | `skip_run=true` | boolean |
| `--max-interview-rounds N` | `max_interview_rounds=N` | integer |
| `--max-repair-rounds N` | `max_repair_rounds=N` | integer |
| `--pipeline-timeout-seconds X` | `pipeline_timeout_seconds=X` | number |
| `--resume <id>` | `resume=<id>` | string |

`--max-generations` is **not** a flag for `ooo auto`; it belongs to `ooo ralph`. When `complete_product=true`, the chained Ralph uses its built-in default (10 generations) bounded by `pipeline_timeout_seconds` or Ralph's own per-iteration / wall-clock budgets.

`--pipeline-timeout-seconds` is accepted only when starting a session. Passing it with `--resume` is rejected because the original deadline is preserved across process restarts.

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
   if present.
2. Immediately enter a low-noise monitor loop with:
   - `ouroboros_job_wait(job_id=<job_id>, cursor=<cursor>, timeout_seconds=120, view="summary")`
   - update `cursor = response.meta.cursor` after every wait/status response
   - treat `response.meta` as the source of truth; use response text only as a
     human-readable hint
3. Relay only meaningful changes: status changes, phase changes, new
   execution/session/lineage handles, progress counters, blocker/error text, or
   a terminal state. If `response.meta.changed is false`, continue silently
   unless the user asked for heartbeat updates.
4. **During the interview phase, surface the live Q&A — not just the round
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
5. If the job status is non-terminal (`queued`, `running`, or another active
   status), keep waiting. Do not tell the user to call job tools themselves.
6. When the job reaches a terminal status, call `ouroboros_job_result(job_id)`
   and summarize the final auto-session outcome. If the final auto result is
   `detached`, keep tracking the surfaced downstream job/Ralph handles when
   available instead of presenting `detached` as completion.
7. If `response.meta.status == "delegated_to_plugin"` and
   `response.meta.job_id is None`, report that OpenCode plugin mode delegated
   the work to the child Task/session. Do not call job wait/result without a
   real job id; follow the host Task widget/session lifecycle.

Use short progress relays; the goal is “I am still watching this for you,” not a
wall of logs.

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
