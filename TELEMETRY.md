# Telemetry

Ouroboros collects **anonymous usage data** to understand how the tool is used
(install funnel, interview → seed → run → evolve conversion, success rates per
runtime backend) and to decide what to improve next.

**We never collect:** code, prompts, seed content, file contents, file paths,
tool arguments, environment variables, or anything that could identify you or
your project.

## How the data is used

Both uses are declared here up front — there are no undisclosed uses:

1. **Product improvement** — funnel drop-offs and per-backend failure rates
   decide what gets fixed next.
2. **Public aggregate stats** — adoption numbers (e.g. weekly active users)
   may be published and used to promote the project. Only aggregates are ever
   published, never per-user data, and any aggregate cell covering fewer than
   10 users is withheld.

**Counting rule (fixed):** one "active user" = one distinct anonymous ID with
at least one `workflow_outcome` where `command=evaluate`, `verified=true`, and
`ci!=true` that week. Submission receipts, installs, reinstalls, retries, and
CI runs are not users. Published numbers follow this rule, verbatim. An
`ouroboros_evaluate` completion that OpenCode's plugin mode delegates out
(`delegated_to_plugin`) finishes outside the Ouroboros process and cannot be
observed by it, so it is never counted toward a verified outcome or the
active-user rule — that's an undercount, never an overcount.

**Identity honesty:** the ID in `~/.ouroboros/telemetry.json` is a random UUID
— pseudonymous, stable across sessions so retention can be measured, derived
from nothing about you or your machine. Delete the file to reset it; opt out
to stop it being used at all.

**Change policy:** this document is append-only — collection scope or use
changes are recorded in the changelog below, ship in a new minor/major version
with a fresh first-run notice, and any *new* data category defaults to off.

### Changelog

- v1 (2026-08): initial contract — events listed below, uses (1) and (2).

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
use — it is not derived from your machine, account, or network.

Each row below is the *exact* property set that event can carry — not "these
plus whatever base/context happens to be set". `src/ouroboros/telemetry.py`
enforces this per event (and, for `command_run`, per `source`) at
serialization time; a property not listed for a row is dropped before the
event is queued, even if some other event's row does carry it. The table and
the code's allowlist constants are one contract in two places — edited
together, always.

| Event | When | Properties (exact set) |
|---|---|---|
| `install_started` | `install.sh` begins | source, os, arch, version, is_local, pre, ref |
| `install_completed` | `install.sh` finishes | source, os, arch, method (uv/pipx/pip), runtime, detected_runtimes (count), version, ref |
| `command_run` (source=mcp) | An `ouroboros_*` MCP tool is invoked from any host CLI (Claude Code, Codex, OpenCode, …) | command (interview/seed/run/evolve/auto/evaluate/qa/…), tool, source, is_funnel, phase (`submission` or `completion`), accepted (submission only), ok (completion only), duration_ms, error_type, sample_rate (polling tools only), runtime_backend, execute_runtime_backend, interview_llm_backend, evaluate_llm_backend, frontdoor, first_command_surface, app_version, os, python_version, ci |
| `command_run` (source=cli) | A direct `ooo <subcommand>` invocation in a terminal | command, source, is_funnel, app_version, os, python_version, frontdoor, ci — no tool, phase, duration/outcome fields, or backend context: those are mcp-only |
| `workflow_outcome` | Two producers (never both for the same evaluation — see Notes): a background MCP job reaches a durable terminal event, **or** a direct (non-job) `ouroboros_evaluate` / `ouroboros_checklist_verify` completion | command, phase (`terminal`), terminal_status, ok, verified, final_approved, failure_reason_code (failed/cancelled/interrupted outcomes only), recovery_action (failed/cancelled/interrupted outcomes only), `$insert_id` (job-derived variant only — one-way event deduplication digest), runtime_backend, execute_runtime_backend, interview_llm_backend, evaluate_llm_backend, app_version, os, python_version, frontdoor, ci |
| `mcp_serve_started` | A host CLI attaches the Ouroboros MCP server for a session | transport, tool_count, frontdoor, first_command_surface, app_version, os, ci — no python_version, no backend/provider context |

Notes:

- `ref` on the install events is a short opaque channel token (`hellogithub`,
  `readme`, `guide-zh`, …) that a docs page or listing prepends to the install
  command as `OUROBOROS_INSTALL_REF=<channel>`. It says which of OUR surfaces
  the command was copied from; it is chosen by us, never derived from the
  machine, defaults to `direct`, and is discarded unless it matches
  `[A-Za-z0-9._-]{1,32}`.
- `first_command_surface` is a fixed enum (`readme_quickstart`,
  `getting_started`, `setup_complete`, or `unknown`) carried only on MCP
  session/command events. README and Getting Started installers persist the
  enum locally before setup; the hint remains preferred after setup so the
  first-command cohort still represents the page that brought the user in.
  A missing hint with an existing `~/.ouroboros/config.yaml` is attributed to
  `setup_complete`. It contains no URL, prompt, path, or user identifier.

- `source` separates the two entry surfaces cleanly: `cli` means the user typed
  the command in a terminal; `mcp` means it arrived from inside an AI agent
  session (`ooo …` keyword in Claude Code / Codex / any MCP host). Internal
  machinery (in-process servers, detached job workers, `ouroboros mcp serve`
  boots, `job`/`dispatch` plumbing) is either not captured or captured as its
  own event, so the cli-vs-agent ratio is not inflated by automation.
- High-frequency polling tools (`ouroboros_job_status`, `ouroboros_session_status`,
  HUD/projection queries, …) are sampled at 1/50 via an independent random
  draw on every call — not a per-process counter — so the probability stays
  1/50 regardless of how long a given process lives; sampled events carry a
  `sample_rate` property so counts can be re-weighted. Everything else is
  captured 1:1.
- `tool`/`command` on an MCP `command_run` event only ever carry a name from
  the audited, static list of built-in Ouroboros tools: an unrecognized
  lookup appears as `ouroboros_unknown_tool`, and a registered third-party
  or custom tool (extensions can register arbitrary names) appears as
  `ouroboros_extension_tool` — the identifying name itself is never sent.
  `command` on a job-derived `workflow_outcome` is the same contract for
  background-job types: only the five funnel stages and Ouroboros' own
  internal diagnostic job types are ever forwarded; anything else (a
  third-party job type registered against the same job manager) appears as
  the fixed `extension_job` value. `command` on a `command_run` (source=cli)
  event is the same contract again for direct `ooo <subcommand>` runs:
  built-in subcommands are forwarded verbatim, and a dynamically installed
  plugin command (`ooo <plugin-name> ...`) appears as the fixed
  `extension_command` value.
- `error_type` is only the Python exception class name (e.g. `TimeoutError`),
  never a message or traceback.
- `failure_reason_code` and `recovery_action` are fixed enums emitted only for
  non-success terminal outcomes. The classifier reads terminal status and
  structured machine metadata only; it never reads exception messages, result
  text, prompts, paths, or identifiers. Current reason codes are `config`,
  `auth`, `timeout`, `model`, `tool`, `validation`, `cancelled`, and `unknown`.
  Current recovery actions are `retry`, `setup`, `login`, `update`,
  `inspect_logs`, and `none`. An unclassified failure is always `unknown` with
  `inspect_logs`, so the failure denominator remains measurable.
- Start-tool `command_run` events are submission receipts. They intentionally
  have `accepted`, not `ok`; queue acceptance is never a completed or verified
  run. `workflow_outcome` is the durable/direct terminal boundary described
  below. Only a completed formal evaluation with explicit `final_approved=true`
  sets `verified=true`.
- `workflow_outcome` has two producers, and exactly one fires per evaluation:
  the durable job-terminal boundary (`JobTelemetryBoundary`, stamping
  `$insert_id` so a retried/redelivered terminal event dedupes) covers
  `ouroboros_start_evaluate`'s background job; the direct-evaluation boundary
  (`record_direct_evaluation_outcome`, no `$insert_id` — each invocation is
  its own outcome, there is no durable job row to replay against) covers
  `ouroboros_evaluate` and `ouroboros_checklist_verify`, both of which never
  create a job. When a direct evaluation runs *inside* a background job (the
  `ouroboros_start_evaluate` path reuses the same handler internally), the
  direct boundary is explicitly suppressed for that call so the job-derived
  event is the only one emitted — one evaluation, one `workflow_outcome`.
- Events are sent to PostHog via a fire-and-forget background thread using a
  **public, write-only** project API key. Telemetry never blocks a command,
  never raises, and silently drops events when offline. The worker is a
  daemon thread, so process exit never waits on it either — events queued or
  in flight when a process terminates are dropped, not delivered, with one
  exception: the detached background-job worker explicitly flushes (bounded,
  5s) before it exits, so the durable-job `workflow_outcome` that the
  counting rule above depends on survives even though that worker is a
  short-lived process with no interactive command left to keep responsive.

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
- [`src/ouroboros/mcp/telemetry_boundary.py`](src/ouroboros/mcp/telemetry_boundary.py) —
  the shared boundary module: the adapter's per-request observation wrapper,
  the job-terminal observer `job_manager.py` calls into, and the direct
  (non-job) evaluation-outcome boundary described above;
- [`src/ouroboros/mcp/tools/evaluation_handlers.py`](src/ouroboros/mcp/tools/evaluation_handlers.py) —
  triggers the direct-evaluation `workflow_outcome` variant from
  `EvaluateHandler.handle()` (direct `ouroboros_evaluate`) and
  `ChecklistVerifyHandler`'s nested multi-AC delegation; suppresses it when
  the same handler runs behind the job-backed `ouroboros_start_evaluate` path;
- [`scripts/install.sh`](scripts/install.sh) — disclosed install start/completion.
