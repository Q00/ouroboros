# Telemetry

Ouroboros collects a deliberately small anonymous dataset for install adoption,
daily activity, runtime adoption, and actionable failures. Four events exist:
`install_started` and `install_completed` from the installer, one
`usage_daily` row per install per active day, and one `workflow_outcome` row
per finished run.

**We never collect:** code, prompts, seed content, file contents, file paths,
tool arguments, environment variables, account data, or project identifiers.

## How the data is used

1. **Product improvement** — terminal workflow failures, their closed reason
   codes, and daily tool-error counts decide what gets fixed next.
2. **Aggregate adoption** — installs, active users, versions, operating systems,
   countries, and runtime backends may be reported only as aggregates. Cells
   covering fewer than 10 users are withheld.

**Counting rules:**

- install count = `install_completed` event count;
- active installs for a day = distinct anonymous IDs with a `usage_daily` row
  for that `day` and `ci!=true`;
- command DAU = the same, restricted to rows where any `cmd_*` count is above zero;
- MCP attached / MCP used = rows with `mcp_serve_starts` / `surface_mcp` above zero;
- verified weekly active = distinct anonymous IDs with `workflow_outcome`,
  `command=evaluate`, `verified=true`, and `ci!=true` that week. Evaluations
  delegated to an external plugin bridge are excluded because no terminal evidence is available.

**How daily usage is counted.** Commands, MCP server starts and fan-out
dispatches are not sent as they happen. Each one adds to a counter for the
current UTC day in `~/.ouroboros/telemetry-usage.json`, on this machine. The
first Ouroboros process that does anything on a later day (any CLI command,
any MCP request including a status poll, a console request, or a run event)
sends each finished day as one `usage_daily` row, timestamped at noon UTC of
the day it describes, and deletes it once the send succeeds. A failed send is retried by a later
process with the same content. Every send of a day carries the same event
`uuid`, derived from the anonymous ID and the day, which PostHog uses to drop
duplicates. A day is therefore reported on the next day Ouroboros runs, and a
day after which Ouroboros never runs again is never reported. Polling and
status tools (for example `ouroboros_job_wait`, `ouroboros_session_status`)
are not counted as usage; only their errors add to `mcp_tool_errors`. The
file is bounded (at most 32 days, 96 counters per day) and is written
atomically under a file lock, so concurrent processes neither overwrite each
other's counts nor send a day twice. Once a day has
been picked up for sending, its row is fixed: a count that still arrives for
that day (from a process that records just as UTC midnight passes, or after
the system clock moved backwards) is dropped, not sent as a second row. A day
that is still in the future by this machine's clock is not sent until it is
over.

**Identity honesty:** the ID in `~/.ouroboros/telemetry.json` is a random UUID,
stable across sessions for lifecycle analysis and derived from nothing about the
machine or user. Delete the file to reset it; opt out to stop collection.

**Change policy:** scope expansions are recorded below, ship in a new
minor/major version with a fresh notice, and default off. Scope reductions do
not require users to acknowledge a new notice.

### Changelog

- v1 (2026-08): initial contract.
- v2 (2026-08): removed install starts, polling/helper successes, request
  durations, tool names, provider details, Python version, frontdoor/onboarding
  attribution, recovery actions, and subagent dispatch data; added daily
  deduplication for retained command and service activity.
- 2026-10: added `acceptance_no_evidence`, closed-reason counts of criteria a
  run accepted without evidence from any verifier (see the table below). On
  by default under the existing opt-outs: closed failure reason codes and
  counts, the same class of data as `ac_verify_failed`, so not a scope
  expansion.
- 2026-10: added `acceptance_artifact_checks`, the closed outcome of each
  controller-run artifact check (base regression, worker tests) of a decided
  run, with counts. On by default under the existing opt-outs: closed reason
  codes and counts of the same class as `acceptance_no_evidence`, so not a
  scope expansion.
- v3 (2026-11): scope reduction. The per-call `command_run` and the
  `service_active`, `mcp_serve_started`, `subagent_dispatch`,
  `ac_verify_failed`, `runtime_drift`, `acceptance_no_evidence` and
  `acceptance_artifact_checks` events are no longer sent. Command, MCP-start
  and fan-out activity became daily counts on one `usage_daily` row per
  install per day; verify-gate, acceptance and runtime-drift observations
  became counts and labels on the run's `workflow_outcome`. Dropped
  entirely: `error_type`, command status, MCP transport, fan-out kind, and
  the no-evidence reason pairs, surface and check-package labels.

## How to opt out

Any one of these disables telemetry completely:

```bash
export DO_NOT_TRACK=1            # the cross-tool standard, always wins
export OUROBOROS_TELEMETRY=0     # ouroboros-specific
```

or in `~/.ouroboros/config.yaml`:

```yaml
telemetry:
  enabled: false
```

Deleting `~/.ouroboros/telemetry.json` resets your anonymous ID.
`~/.ouroboros/telemetry-usage.json` holds the unsent daily counts; nothing in
it is sent while telemetry is disabled, and nothing new is recorded.
`ouroboros uninstall` removes both.

Telemetry controls and destination overrides are operator-owned. The real
process environment and `~/.ouroboros/.env` are trusted; a project-directory
`.env` cannot set `DO_NOT_TRACK`, `OUROBOROS_TELEMETRY`, `OUROBOROS_POSTHOG_HOST`,
`OUROBOROS_POSTHOG_API_KEY`, `_OUROBOROS_TELEMETRY_SURFACE` (set only by the
console on the commands it starts), `CI`, or `GITHUB_ACTIONS` — the last two feed the
`ci!=true` exclusion in the counting rule above, so a cloned repository's
`.env` cannot forge CI classification to deregister genuine local users from
the published metric. Invalid or unreadable user configuration disables
collection rather than silently restoring the default. An explicit
`OUROBOROS_TELEMETRY=1` is never an override: any disabling control above —
including a persisted `enabled: false` or malformed configuration — still
wins. The installer honors the same controls, reading `~/.ouroboros/.env`
before its first notice or event.

## What is sent

Identity is a random UUID (`~/.ouroboros/telemetry.json`) generated on first
use. Each row below is the exact property set accepted by the serializer.

| Event | When | Properties (exact set) |
|---|---|---|
| `install_completed` | `install.sh` finishes successfully (the Windows `install.ps1` emits neither installer event) | os, runtime, version, ref |
| `install_started` | `install.sh` begins — deliberately per-invocation, NOT daily-deduplicated: each row pairs with (or lacks) an `install_completed` to measure install drop-off and retry behavior, and volume is bounded by install attempts (~hundreds/month) | os, version, ref |
| `usage_daily` | At most once per install per UTC day of activity, sent by the first Ouroboros process on a later day (see "How daily usage is counted"); counters that stayed at zero are omitted | `cmd_<command>` (count per command, from the closed set below), `surface_cli` / `surface_mcp` / `surface_console` (counted commands per surface), `mcp_serve_starts`, `subagent_dispatches`, `mcp_tool_errors`, `seed_session_context`, `seed_session_context_gap`, day, runtime_backend, app_version, os, ci |
| `workflow_outcome` | A background workflow, a terminal `ooo run`, or direct evaluation reaches a terminal result inside Ouroboros (a paused run is not terminal and emits nothing) | command, terminal_status, verified, failure_reason_code (non-success only), failure_cause (non-success `run` only; closed enum, see below), ac_passed / ac_total (`run` only; see below), verify_rejections / verify_top_cause, runtime_drifts / runtime_drift_kind, no_evidence_criteria / no_evidence_top_reason / verification_coverage, base_regression / worker_tests / artifact_failed_criteria (each only when observed during that run; see below), runtime_backend, app_version, os, ci, `$insert_id` |

Notes:

- `install_started` is excluded from daily rollup on purpose: retries are the
  signal (a started-but-never-completed sequence is install drop-off), and
  installer volume is self-bounding.
- `cmd_<command>` counts requests a person or their agent made to do work.
  From MCP: `interview`, `pm`, `seed`, `run`, `evolve`, `auto`, `evaluate`,
  `qa`, `ralph`, `unstuck` (`ouroboros_lateral_think`), `brownfield`, and
  `cancel`; an interview counts once per turn. From the CLI: each built-in
  `ouroboros <command>` except the internal `mcp`, `job` and `dispatch`
  (`init` counts as `interview`, `workflow-ir` as `cmd_workflow_ir`), and
  `extension_command` for any plugin command. `ouroboros gui` and the commands
  the console starts for you count under `surface_console`. Polling, status,
  read-only and internal tools (`ouroboros_job_wait`, `ouroboros_job_status`,
  `ouroboros_job_result`, `ouroboros_session_status`, `ouroboros_query_events`,
  `ouroboros_fetch_artifact`, and the rest of `_UNCOUNTED_TOOLS` in
  `telemetry.py`) are not counted.
- `mcp_serve_starts` counts MCP servers that reached their serve loop (the
  top of the attached → used funnel); a bind failure does not count.
  `subagent_dispatches` counts fan-out contracts handed to a host.
  `mcp_tool_errors` counts MCP requests, to any tool, that failed with an
  error or returned a result marked as an error (for example a recoverable
  interview failure, or a status poll of a failed job). Only a result marked
  `input_required`, a request for missing input, is not an error.
- `seed_session_context` / `seed_session_context_gap` count
  `ouroboros_generate_seed` calls that took the interview-less path and
  produced a Seed / returned gap questions instead. It answers one adoption
  question and carries none of the goal, criteria, or question text.
- `runtime_backend` on `usage_daily` is the backend of the last MCP process
  that counted something that day; CLI-only days carry none.
- `verify_rejections` counts the run's AC attempts the deterministic verify
  gate rejected, and `verify_top_cause` names the most frequent structural
  reason (closed enum: `invalid_contract`, `artifacts_missing`,
  `artifacts_missing_found_elsewhere` — the artifact exists in the workspace
  but not at the contract path, `environment_unverifiable`, `timeout`,
  `exit_nonzero`, `output_assertion_unmatched`, `workspace_mutated` — files
  changed while verification ran, `no_evidence`, or `unknown`). The AC text,
  command, path, artifact name and output stay in the local event store
  (`execution.verify.failed`).
- `runtime_drifts` counts how often a frozen runtime authority input (Codex
  config, CLI executable, dispatch registry, profile routing) changed under
  the run, and `runtime_drift_kind` names the most frequent kind (closed enum:
  `codex_config`/`cli_executable`/`skill_dispatcher`/`mcp_handler_registry`/`skill_dispatch_registry`/`profile_routing`/`baseline_unavailable`/`attestation_timeout`/`unknown`).
  The run continues on the re-baselined input.
- `no_evidence_criteria` counts criteria the run accepted although no verifier
  had evidence for them (the check package did not decide them and the legacy
  verifier had no evidence); the acceptance itself is unchanged.
  `no_evidence_top_reason` names the most frequent reason the check package
  did not decide: `uncovered` (no admitted check is linked to it),
  `no_admitted_package`, `no_binding`, `no_binding_after_request`,
  `no_binding_budget_exhausted` (no binding reaches the target),
  `script_check_advisory` (only model-written script checks passed),
  `no_held_out_case`, `no_reproduction_check`, or `unknown`.
  `verification_coverage` is `full`/`partial`/`low`/`unknown`. All three are
  absent when every accepted criterion had evidence, or when the run's check
  package was off or not consulted.
- `base_regression` and `worker_tests` report what each controller-run
  artifact check observed on a decided run (`base_regression`: the base
  tree's existing tests that pair with or import a changed module;
  `worker_tests`: the test files the worker added): `rejected` (it failed the
  criteria the check package left undecided), `exempted`, `passed`, or why it
  had no observation (`timeout`, `base_runner_crash`, `no_selected_files`,
  `unsupported_runner`, `not_a_test_result`, `unavailable`,
  `imported_outside_copy`, `not_run`, `no_admitted_command`, `unconfirmed`,
  `unknown`). `artifact_failed_criteria` counts the criteria the checks failed
  in the final decision. Absent when no artifact check ran.
- These folded fields come only from the product's typed state, and anything
  outside each closed vocabulary folds to `unknown` before serialization.
  Counts are capped at 10,000. When a job runs several acceptance passes,
  counts add up and labels describe the last pass.
- `ref` is one of `direct`, `readme`, `readme-hero`, `readme-ko`,
  `readme-hero-ko`, `readme-zh`, `readme-hero-zh`, or `docs-getting-started`.
  Every other value folds to `direct` before serialization.
- User lifecycle is derived by PostHog's lifecycle query over the stable random
  identity and daily `usage_daily` rows; no extra lifecycle-state property is sent.
- `failure_reason_code` is one of `config`, `auth`, `timeout`, `model`,
  `tool`, `validation`, `cancelled`, or `unknown`.
- `failure_cause` on a failed/cancelled `run` names which structural branch
  failed the run, derived only from the executor's durable machine-readable
  evidence (never prose, commands, paths, or output). Closed enum:
  `verify_<cause>` (the verify-gate cause that rejected the run at
  final settlement, exhausted an AC's retries, or failed an AC judged once
  from a shared attempt), `worker_evidence_missing`,
  `worker_fabrication_suspected`, `worker_blocked`, `worker_failed` (an AC was
  judged not done and no retry budget or route remained), `dependency_blocked`
  (every judged AC was blocked upstream), `runtime_error` (the orchestrator
  raised an audited exception class), `launch_<branch>` (the run was rejected
  before any executor evidence existed: `launch_workspace_unavailable`,
  `launch_seed_invalid`, `launch_resume_blocked`, `launch_config_error`,
  `launch_prepare_failed`, `launch_rejected`), `cancelled`, or `unknown`.
  Anything else folds to `unknown` before serialization. A job that fails
  before its work function returns (pre-launch rejection, or a run whose
  terminal is recovered from linked execution evidence after a restart)
  carries the same closed values. A CLI run rejected while preparing or restoring
  its task workspace also emits `launch_workspace_unavailable`; malformed Seed
  YAML and Seed model validation failures emit `launch_seed_invalid`. Other
  CLI pre-launch failures are not covered by that event. A background `evaluate` job that fails
  carries only the branch-level `failure_reason_code` its handler already
  reports on the direct path (`validation`/`config`/`auth`/`timeout`/`model`),
  never a `failure_cause`.
- `ac_passed` / `ac_total` on a `run` outcome count the root-level acceptance
  criteria the executor judged accepted versus judged at all, derived from
  durable `execution.ac.attempt_judged` events (counts only — never AC text,
  paths, commands, or output). A run is all-or-nothing, so this is what
  separates a run that delivered 3 of 4 criteria from one that delivered
  none. Absent when nothing was judged (e.g. a pre-launch rejection); the
  boundary forwards the pair only as consistent non-negative integers with
  `ac_passed <= ac_total`.
- `command` values come only from static built-in command/tool/job registries.
- `$insert_id` on a job-derived `workflow_outcome` is a one-way digest of the
  job id, and the event `uuid` is derived from it, so PostHog can drop a
  retried terminal for the same job. Direct evaluations are not deduplicated.
- The event `uuid` on `usage_daily` is a one-way digest (UUID v5) of the
  anonymous ID and the `day`, so every send of a day dedupes to one row.
- PostHog may derive coarse country from the request IP for the country
  aggregate. Ouroboros does not include the IP address in event properties.
- Events use a public write-only project key and a fire-and-forget worker.
  Event capture queues without waiting for delivery. A `workflow_outcome`
  that cannot be delivered (offline, process exit) is dropped; a
  `usage_daily` row stays on disk until a later process delivers it.
  Explicit exit flushes may delay return: CLI run exit paths wait up to 1.5
  seconds after user-facing output; detached job workers wait up to 5
  seconds for their terminal `workflow_outcome` before exit. These waits are
  best effort and do not guarantee delivery.

## Where the code lives

Serialization, allowlisted properties, identity, the outcome folding and
transport live in [`src/ouroboros/telemetry.py`](src/ouroboros/telemetry.py)
(stdlib only); the daily rollup file in
[`src/ouroboros/telemetry_rollup.py`](src/ouroboros/telemetry_rollup.py); the
installer helpers in [`scripts/install.sh`](scripts/install.sh).
Collection is triggered only at these audited call sites:

- [`src/ouroboros/cli/main.py`](src/ouroboros/cli/main.py) — direct CLI command
  (a `cmd_*` count) and first-run notice;
- [`src/ouroboros/cli/commands/mcp.py`](src/ouroboros/cli/commands/mcp.py) — MCP
  serve attachment (`mcp_serve_starts`);
- [`src/ouroboros/mcp/server/adapter.py`](src/ouroboros/mcp/server/adapter.py) —
  one count per MCP request, including validation and security failures;
- [`src/ouroboros/mcp/job_manager.py`](src/ouroboros/mcp/job_manager.py) and
  [`src/ouroboros/mcp/job_start.py`](src/ouroboros/mcp/job_start.py) —
  durable background-job terminal outcomes, and the per-job outcome tally the
  job's runner reports into;
- [`src/ouroboros/mcp/telemetry_boundary.py`](src/ouroboros/mcp/telemetry_boundary.py) —
  the shared boundary module: the adapter's per-request observation wrapper,
  the job-terminal observer `job_manager.py` calls into, fan-out dispatch
  counts, and the direct (non-job) evaluation-outcome boundary described above;
- [`src/ouroboros/mcp/tools/evaluation_handlers.py`](src/ouroboros/mcp/tools/evaluation_handlers.py) —
  triggers the direct-evaluation `workflow_outcome` variant from
  `EvaluateHandler.handle()` (direct `ouroboros_evaluate`) and
  `ChecklistVerifyHandler`'s nested multi-AC delegation; suppresses it when
  the same handler runs behind the job-backed `ouroboros_start_evaluate` path;
- [`src/ouroboros/cli/commands/run.py`](src/ouroboros/cli/commands/run.py) —
  the terminal `ooo run` outcome and its outcome tally;
- [`src/ouroboros/orchestrator/parallel_executor.py`](src/ouroboros/orchestrator/parallel_executor.py),
  [`src/ouroboros/orchestrator/runtime_drift.py`](src/ouroboros/orchestrator/runtime_drift.py)
  and [`src/ouroboros/boundary/run_control.py`](src/ouroboros/boundary/run_control.py)
  (through [`no_evidence.py`](src/ouroboros/boundary/no_evidence.py) and
  [`base_regression.py`](src/ouroboros/boundary/base_regression.py)) — verify-gate
  rejections, runtime drift, and acceptance observations, folded into the
  run's outcome tally, never sent on their own;
- [`src/ouroboros/gui/jobs.py`](src/ouroboros/gui/jobs.py) — marks the
  commands the console starts so they count under `surface_console`;
- [`scripts/install.sh`](scripts/install.sh) — install start and completion.
  [`scripts/install.ps1`](scripts/install.ps1), the Windows installer, emits
  neither `install_started` nor `install_completed`. The `ouroboros setup`
  calls it makes are ordinary CLI invocations and are counted in the
  `usage_daily` row under the usual controls, as they are for `install.sh`.
