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
  efficiency_mode: "$efficiency_mode"
  frugality_assurance: "$frugality_assurance"
---

# /ouroboros:auto

Run the full-quality auto pipeline from a single task description.

## Dispatch requirement

This skill must be executed by invoking MCP tool `ouroboros_start_auto`. Do not
manually inspect repositories, run shell commands, query GitHub, edit files, or
otherwise emulate the auto pipeline as a substitute.

If `ouroboros_start_auto` is unavailable, stop and report that the required MCP tool
is unavailable. A manual fallback is not an `ooo auto` run.

If `ouroboros_start_auto` is invoked successfully but returns `blocked`, `failed`, or
another terminal auto-session status, report that auto-session status and the
tool's blocker. Do not label that outcome as MCP dispatch failure; dispatch
failure means the MCP tool could not be invoked.

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
| `--efficiency-mode adaptive\|quality_first` | `efficiency_mode=<value>` | string |
| `--frugality-assurance off\|observe\|strict` | `frugality_assurance=<value>` | string |
| `--resume <id>` | `resume=<id>` | string |

`--max-generations` is **not** a flag for `ooo auto`; it belongs to `ooo ralph`. When `complete_product=true`, the chained Ralph uses its built-in default (10 generations) bounded by `pipeline_timeout_seconds` or Ralph's own per-iteration / wall-clock budgets.

`--pipeline-timeout-seconds` is accepted only when starting a session. Passing it with `--resume` is rejected because the original deadline is preserved across process restarts.

Before a fresh start with no user choice, ask in outcome language: **Efficient
execution** maps to `adaptive/observe`; **Quality-first execution** maps to
`quality_first/off`. `strict` assurance is separate explicit consent because it
may spend extra work on proof. Do not send these arguments on resume; the server
restores the persisted contract.

## Behavior

1. Starts an auto session.
2. Runs bounded Socratic interview rounds with source-tagged auto answers.
3. Generates a Seed.
4. Reviews and repairs until A-grade or blocked.
5. Starts execution only after A-grade.
6. When `complete_product=true`, chains RUN → RALPH_HANDOFF after a successful run handoff and waits for a terminal Ralph status so a single invocation iterates Ralph until QA passes, convergence, or a budget bound trips. A QA-pass on the executed product completes the auto session; recognized failure modes (`iteration_timeout`, `wall_clock_exhausted`, `oscillation_detected`, `grade_regressing`, `max_generations reached`) block the auto session with the matching `stop_reason` in `last_error` so operators can resume after the cause is addressed.

During an executing auto run, additive human intent is routed by the main
session: reload deferred schemas with
`tool discovery query: "+ouroboros session signal"`, call
`ouroboros_session_signal_targets` with the observed execution ID,
match the intent to live AC content, and send the selected exact target through
`ouroboros_session_signal`. Never ask the human for internal IDs. Ask only when
multiple candidates remain genuinely tied, and do not route shared contract
changes to one AC.

## Active Conductor host UX

After a start response, show `dashboard_url` when present or mention
`ouroboros tui open` once. Include the returned runtime/LLM backends, efficiency
mode, and frugality assurance. Say that the exact active model and execution
plan will arrive from configuration/routing events rather than guessing.

When `response.meta.job_observer` is present and a Task/Agent child exists,
spawn exactly one read-only observer and pass the contract unchanged. It owns
job wait/result and the cursor exclusively; the main session must not poll the
same job. Keep the conversation available for requirement refinement, read-only
review, explicit control, or unrelated work in an isolated worktree. Check
active-worker overlap before writing to the Auto workspace. Without a child,
use the declared linked `ouroboros_job_wait` fallback and never run both owners.
Do not claim an observer until Task/Agent returns a live child handle. If child
creation fails, do not promise live proactive relays. The detached worker
survives the stdio turn; catch up from durable events on the next parent turn or
explicit status request. Keep the turn open only for explicit live watching.

Relay only structured changes:

- `run_configuration`: current runtime/harness, starting model/tier when known,
  efficiency mode, and frugality assurance.
- `execution_plan`: total ACs, total dependency/parallel levels, parallelism, and
  first scheduled AC summaries.
- `discovery_summary`: bounded targets and purpose, never raw commands or
  reasoning.
- level/routing/harness/verified changes: report once when material.
- `attention_required`: surface immediately.
- Synapse `queued`/`delivering` is pending, not applied;
  `applied`/`completed` is runtime-proven and may carry a bounded AC reply;
  rejected/uncertain delivery is surfaced immediately.

For additive refinements send exact guards with `contract_effect="additive"`,
`source="user"`, `mode="redirect"`, and explicit `fallback_mode="after_turn"`.
Use `mode="inform"` for a read-only AC question or assurance request and omit
`fallback_mode` entirely in that mode. Never ask for internal IDs; semantically
choose the relevant target and ask only on a genuine tie.

For conductor attention, use at most one short-lived read-only verifier. If the
host cannot verify, do not ACT. Otherwise VERIFY → DECIDE from the ordered
`recommended_host_actions` → LOG `selected` with
`ouroboros_record_conductor_decision` → ACT only a menu-listed registered tool →
LOG `completed`, `failed`, or `declined`. Auto may run only one bounded
deterministic non-relaxing successor and never changes the approved shared
contract itself.

These are English canonical instructions. Phrase them naturally in the user's
current conversation language.

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
