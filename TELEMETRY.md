# Telemetry

Ouroboros collects a deliberately small anonymous dataset for install adoption,
daily activity, runtime adoption, and actionable failures.

**We never collect:** code, prompts, seed content, file contents, file paths,
tool arguments, environment variables, account data, or project identifiers.

## How the data is used

1. **Product improvement** — failed or blocked lifecycle commands and terminal
   workflow failures decide what gets fixed next.
2. **Aggregate adoption** — installs, active users, versions, operating systems,
   countries, and runtime backends may be reported only as aggregates. Cells
   covering fewer than 10 users are withheld.
3. **Research use.** Anonymous aggregate statistics derived from the existing
   events may be published in research publications. See
   [Research use](#research-use).
4. **Randomized product defaults.** Some product defaults are assigned at
   random per anonymous installation so that the default with better outcomes
   can be kept. See [Randomized defaults](#randomized-defaults).

**Counting rules:**

- install count = `install_completed` event count;
- command DAU = distinct anonymous IDs with `command_run` and `ci!=true` that day;
- service DAU = distinct anonymous IDs with `service_active` and `ci!=true` that day;
- verified weekly active = distinct anonymous IDs with `workflow_outcome`,
  `command=evaluate`, `verified=true`, and `ci!=true` that week. Evaluations
  delegated to an external plugin bridge are excluded because no terminal evidence is available.

`command_run` and `service_active` carry deterministic daily `$insert_id` values.
PostHog therefore stores at most one row per anonymous user/day/dimension tuple
for activity metrics, even when a host repeats a command or starts multiple MCP processes.

**Identity honesty:** the ID in `~/.ouroboros/telemetry.json` is a random UUID,
stable across sessions for lifecycle analysis and derived from nothing about the
machine or user. Delete the file to reset it; opt out to stop collection. The
same file records `notice_shown` and `notice_version`, the version of the notice
last displayed; an older version shows the updated notice once.

**Change policy:** scope expansions are recorded below, ship in a new
minor/major version with a fresh notice, and default off. Scope reductions do
not require users to acknowledge a new notice.

### Research use

Anonymous aggregate statistics derived from the existing telemetry events may
be published in research publications (papers, technical reports) about
coding-agent reliability and verification, including comparisons between the
arms of a [randomized product default](#randomized-defaults). Research use
itself adds no events and no properties; it is a purpose for the data listed
under [What is sent](#what-is-sent).

- **Aggregates only.** Only aggregate counts and rates are published. Any cell
  covering fewer than 10 distinct anonymous IDs is pooled into "other" or
  withheld. No raw event rows leave the analytics store.
- **Populations.** Only these events may be analyzed for research:
  `workflow_outcome` (for example `command`, `terminal_status`, `verified`,
  `failure_reason_code`, `failure_cause`, and the check-package dimensions
  listed under [Randomized defaults](#randomized-defaults)) and
  `ac_verify_failed` (`cause`), each with its `runtime_backend`,
  `app_version`, and `os`.
- **Collection window.** Research use applies from the first release
  containing notice v3 (the next release after v0.54.6; version:
  `<filled in at release>`) until this section is changed. Events collected
  before that release are not used for research; the window is enforced with
  the `app_version` property that every analyzed event carries.
- **Recorded versions.** Every published aggregate records the `app_version`
  values it includes.
- **Exclusions.** Opted-out installs send nothing, so they never appear in
  research data. Events with `ci=true` are excluded.
- **Questions and opt-out.** Ask questions in a
  [GitHub issue](https://github.com/Q00/ouroboros/issues). You can opt out at
  any time with any control under [How to opt out](#how-to-opt-out).

### Randomized defaults

A product default under evaluation is assigned per installation, at random,
so that its effect can be measured against the previous behavior. One default
is under evaluation today: the check package boundary of `ooo run` (checks
built from the acceptance criteria before the worker starts, then run against
the finished workspace).

- **Assignment.** The arm is a deterministic function of the anonymous ID in
  `~/.ouroboros/telemetry.json`: a SHA-256 digest of a fixed experiment key and
  the ID, split 50/50 into `on` and `off`. The same installation always gets
  the same arm. Nothing about the machine or user enters the digest.
- **No telemetry, no randomization.** When telemetry is disabled, when no
  anonymous ID exists yet, or when the installation has not been shown the
  current notice, the arm is `off` (the previous behavior) and recorded as
  `fallback`. Installs that send no telemetry are never assigned at random.
- **Your setting wins.** `ooo run --check-package` / `--no-check-package`,
  `OUROBOROS_CHECK_PACKAGE=on|off`, or `boundary.check_package: on|off` in
  `~/.ouroboros/config.yaml` always override the assignment, in that order of
  precedence, and are recorded as `user_forced_on` or `user_forced_off`.
- **What is recorded.** A terminal `ooo run` (CLI or MCP) adds the enumerated
  properties below to its `workflow_outcome` row. Values only: never the
  checks, the criteria, commands, paths, or output.

| Property | Values |
|---|---|
| `check_package_arm` | `on`, `off` |
| `check_package_assignment` | `randomized`, `user_forced_on`, `user_forced_off`, `fallback` |
| `check_package_status` | `admitted`, `rejected`, `construction_failed`, `not_run` |
| `package_verdict` | `pass`, `fail`, `indeterminate`, `none` |
| `legacy_verdict` | `accept`, `reject`, `none` |
| `reconciliation` | `agree`, `package_accepted_over_legacy_reject`, `package_rejected_over_legacy_accept`, `fallback_to_legacy`, `none` |
| `legacy_failure_class` | `evidence_missing`, `evidence_form_mismatch`, `fabrication_suspected`, `scope_creep`, `stall`, `blocked`, `transcript_missing_infrastructure`, `accepted`, `other`, `none` |
| `legacy_failure_class_count` | `0`, `1`, `2`, `3+` |

- `check_package_status` describes the package the worker was bound to:
  `admitted` (a package passed admission on the starting tree, possibly after
  a regenerated version replaced a rejected one), `rejected` (the last package
  built was not admitted), `construction_failed` (the last attempt built no
  package, or preparation failed), `not_run` (arm `off` or a resumed session).
- `package_verdict` is the admitted package's verdict on the finished
  workspace; `none` when no admitted package was verified (including an
  execution path that never consults the package, such as a single-criterion
  Seed with `orchestrator.execution_mode: legacy`), `indeterminate` also when
  the verification itself failed.
- `legacy_verdict` is the verdict of the per-criterion verifier that decides
  without the package: `accept` or `reject` for the run, `none` when the run
  ended before a verdict (cancelled, paused, or an orchestrator error).
- `reconciliation` compares the two for the run: `agree` (the package decided
  at least one criterion and the run verdict equals the legacy verdict),
  `package_accepted_over_legacy_reject`, `package_rejected_over_legacy_accept`,
  `fallback_to_legacy` (arm `on`, but the package decided no criterion:
  nothing admitted, indeterminate, or every criterion uncovered), `none`
  (arm `off` or package not run).
- `legacy_failure_class` is the worker failure class the legacy verifier
  recorded for the first rejected criterion in criterion order (the class
  names of the orchestrator's failure taxonomy, lower-cased); `accepted` when
  the legacy verifier accepted the run, `other` for a rejected criterion
  without one of these classes, `none` when there is no legacy verdict.
  `legacy_failure_class_count` buckets how many criteria the legacy verifier
  rejected. Both are recorded in both arms, so the `off` arm is the baseline.

### Changelog

- v1 (2026-08): initial contract.
- v2 (2026-08): removed install starts, polling/helper successes, request
  durations, tool names, provider details, Python version, frontdoor/onboarding
  attribution, recovery actions, and subagent dispatch data; added daily
  deduplication for retained command and service activity.
- v3 (2026-09): research-use purpose added; no new events or properties.
- v4 (2026-09): randomized product defaults. The `ooo run` check package
  boundary becomes a randomized default (50/50 by anonymous ID, `off` without
  telemetry). `workflow_outcome` for `command=run` gains `check_package_arm`,
  `check_package_assignment`, `check_package_status`, `package_verdict`,
  `legacy_verdict`, `reconciliation`, `legacy_failure_class`, and
  `legacy_failure_class_count` (closed values only). The first-run notice now
  names randomized defaults and research use, and `notice_version` in
  `telemetry.json` re-displays it once to installs that saw an earlier notice.
  CLI `ooo run` outcomes, which were recorded as `command=extension_job`, are
  now recorded as `command=run`.

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
`ouroboros uninstall` removes it entirely.

Telemetry controls and destination overrides are operator-owned. The real
process environment and `~/.ouroboros/.env` are trusted; a project-directory
`.env` cannot set `DO_NOT_TRACK`, `OUROBOROS_TELEMETRY`, `OUROBOROS_POSTHOG_HOST`,
`OUROBOROS_POSTHOG_API_KEY`, `CI`, or `GITHUB_ACTIONS` — the last two feed the
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
| `service_active` | The running MCP service receives its first tool request that day | service (`mcp`), runtime_backend, app_version, os, ci, `$insert_id` |
| `mcp_serve_started` | A host attaches the Ouroboros MCP server — at most one row per user/day/transport | transport (`stdio`/`sse`/`streamable-http`/`unknown`), runtime_backend, app_version, os, ci, `$insert_id` |
| `subagent_dispatch` | A session used subagent fan-out — at most one row per user/day/phase/fanout_kind | phase (`emitted`/`submitted`/`unknown`), fanout_kind, runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=mcp) | A retained lifecycle MCP command succeeds/is accepted, or any MCP command fails/is blocked | command, service, status (`succeeded`, `accepted`, `failed`, `rejected`, `blocked`), error_type (exception failures only), origin (`command=seed` only; closed enum, see below), runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=cli) | A direct non-internal `ooo <command>` is invoked | command, service (`cli`), status (`invoked`), app_version, os, ci, `$insert_id` |
| `workflow_outcome` | A background workflow, a terminal `ooo run`, or direct evaluation reaches a terminal result inside Ouroboros (a paused run is not terminal and emits nothing) | command, terminal_status, verified, failure_reason_code (non-success only), failure_cause (non-success `run` only; closed enum, see below), check_package_arm, check_package_assignment, check_package_status, package_verdict, legacy_verdict, reconciliation, legacy_failure_class, legacy_failure_class_count (`run` only; closed enums, see [Randomized defaults](#randomized-defaults)), runtime_backend, app_version, os, ci, `$insert_id` |
| `runtime_drift` | A frozen runtime authority input (Codex config, CLI executable, dispatch registry, profile routing) is observed to have changed after the runtime initialized; the run continues on the re-baselined input | kind (closed enum: `codex_config`/`cli_executable`/`skill_dispatcher`/`mcp_handler_registry`/`skill_dispatch_registry`/`profile_routing`/`baseline_unavailable`/`attestation_timeout`/`unknown`), runtime_backend, app_version, os, ci |
| `ac_verify_failed` | The orchestrator's deterministic AC verify gate rejects an attempt (`run_verify_commands` enabled) | cause (closed enum: `invalid_contract`/`artifacts_missing`/`artifacts_missing_found_elsewhere`/`environment_unverifiable`/`timeout`/`exit_nonzero`/`output_assertion_unmatched`/`workspace_mutated`/`unknown`), runtime_backend, app_version, os, ci |

Notes:

- `install_started` is excluded from the daily-deduplication contract on
  purpose: retries are the signal (a started-but-never-completed sequence is
  install drop-off), and installer volume is self-bounding.
- `mcp_serve_started` and `subagent_dispatch` are daily-deduplicated adoption
  signals (deterministic `$insert_id`, one row per user/day/dimension) — the
  per-session volume and rich per-dispatch properties removed by #2278 stay
  removed. `mcp_serve_started` is the top of the activation funnel ("MCP
  attached"), distinct from `service_active` ("made a tool request").

- `origin` on a `command=seed` row names which entrance produced the row:
  `interview` (a completed interview session), `session_context` (the
  interview-less path crystallized a Seed from session-settled material), or
  `session_context_gap` (the interview-less path returned gap questions
  instead of a Seed). It answers one adoption question — is the
  interview-less path used, and does it close — and carries none of the
  goal, criteria, or question text. Any other value is dropped.
- `cause` on `ac_verify_failed` names which structural branch of the
  deterministic verify gate rejected the attempt — e.g.
  `artifacts_missing_found_elsewhere` means the expected artifact exists in
  the workspace but not at the contract path (the worker-`cd` signature), and
  `workspace_mutated` means files changed while verification ran. It never
  carries the AC text, command, path, artifact name, or any output; those
  stay in the local event store (`execution.verify.failed`), which also
  records `verify_cause` and the local-only `verify_cwd` for per-session
  debugging.
- `ref` is one of `direct`, `readme`, `readme-hero`, `readme-ko`,
  `readme-hero-ko`, `readme-zh`, `readme-hero-zh`, or `docs-getting-started`.
  Every other value folds to `direct` before serialization.
- Successful polling and internal helper tools are not collected. Failures are
  retained so broken status, artifact, fan-out, and control paths remain visible.
- `service_active` is emitted from the tool-request boundary, not process startup.
  A bind, SDK startup, or PID-file failure therefore cannot count as service DAU.
- User lifecycle is derived by PostHog's lifecycle query over the stable random
  identity and daily `service_active` rows; no extra lifecycle-state property is sent.
- A logical tool result with `is_error=true` is recorded as `status=blocked`.
  This makes seed blocks and other validation stops distinct from exceptions.
- Registered non-product MCP tools are folded to `extension_tool` regardless of
  their textual prefix. Their successful requests contribute only to service
  activity; failures and logical blocks retain the fixed command token.
- `error_type` is only an audited exception class name, never a message or
  traceback. `failure_reason_code` is one of `config`, `auth`, `timeout`,
  `model`, `tool`, `validation`, `cancelled`, or `unknown`.
- `failure_cause` on a failed/cancelled `run` names which structural branch
  failed the run, derived only from the executor's durable machine-readable
  evidence (never prose, commands, paths, or output). Closed enum:
  `verify_<cause>` (the `ac_verify_failed` cause that rejected the run at
  final settlement or exhausted an AC's retries), `worker_evidence_missing`,
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
  carries the same closed values; a background `evaluate` job that fails
  carries only the branch-level `failure_reason_code` its handler already
  reports on the direct path (`validation`/`config`/`auth`/`timeout`/`model`),
  never a `failure_cause`.
- `command` values come only from static built-in command/tool/job registries.
- `$insert_id` on `command_run` and `service_active` is a SHA-256 digest of the
  anonymous ID, UTC day, event, and retained dimensions. Job-derived
  `workflow_outcome` uses a one-way job digest; direct evaluations are not deduplicated.
- PostHog may derive coarse country from the request IP for the country
  aggregate. Ouroboros does not include the IP address in event properties.
- Events use a public write-only project key and a fire-and-forget worker.
  Telemetry never blocks a command and silently drops events when offline.
  Detached job workers flush their terminal `workflow_outcome` before exit.

## Where the code lives

Serialization, allowlisted properties, identity, and transport live in
[`src/ouroboros/telemetry.py`](src/ouroboros/telemetry.py) (stdlib only) and
the installer helpers in [`scripts/install.sh`](scripts/install.sh).
Collection is triggered only at these audited call sites:

- [`src/ouroboros/cli/main.py`](src/ouroboros/cli/main.py) — direct CLI command
  and first-run notice;
- [`src/ouroboros/cli/commands/mcp.py`](src/ouroboros/cli/commands/mcp.py) — MCP
  serve attachment;
- [`src/ouroboros/mcp/server/adapter.py`](src/ouroboros/mcp/server/adapter.py) —
  exactly one MCP request outcome, including validation and security failures;
- [`src/ouroboros/mcp/job_manager.py`](src/ouroboros/mcp/job_manager.py) —
  durable background-job terminal outcomes;
- [`src/ouroboros/cli/commands/run.py`](src/ouroboros/cli/commands/run.py):
  the terminal `workflow_outcome` of a CLI `ooo run`;
- [`src/ouroboros/boundary/run_control.py`](src/ouroboros/boundary/run_control.py):
  builds the check-package dimensions that the CLI run and the MCP
  `execute_seed` result carry into `workflow_outcome` (values only), and
  [`src/ouroboros/boundary/rollout.py`](src/ouroboros/boundary/rollout.py)
  assigns the randomized arm;
- [`src/ouroboros/mcp/telemetry_boundary.py`](src/ouroboros/mcp/telemetry_boundary.py) —
  the shared boundary module: the adapter's per-request observation wrapper,
  the job-terminal observer `job_manager.py` calls into, and the direct
  (non-job) evaluation-outcome boundary described above;
- [`src/ouroboros/mcp/tools/evaluation_handlers.py`](src/ouroboros/mcp/tools/evaluation_handlers.py) —
  triggers the direct-evaluation `workflow_outcome` variant from
  `EvaluateHandler.handle()` (direct `ouroboros_evaluate`) and
  `ChecklistVerifyHandler`'s nested multi-AC delegation; suppresses it when
  the same handler runs behind the job-backed `ouroboros_start_evaluate` path;
- [`scripts/install.sh`](scripts/install.sh) — successful install completion.
  [`scripts/install.ps1`](scripts/install.ps1), the Windows installer, emits
  neither `install_started` nor `install_completed`. The `ouroboros setup`
  calls it makes are ordinary CLI invocations and produce the `command_run`
  (service=cli) rows above under the usual controls, as they do for
  `install.sh`.
