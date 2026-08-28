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
| `install_completed` | `install.sh` finishes successfully | os, runtime, version, ref |
| `service_active` | The running MCP service receives its first tool request that day | service (`mcp`), runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=mcp) | A retained lifecycle MCP command succeeds/is accepted, or any MCP command fails/is blocked | command, service, status (`succeeded`, `accepted`, `failed`, `rejected`, `blocked`), error_type (exception failures only), runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=cli) | A direct non-internal `ooo <command>` is invoked | command, service (`cli`), status (`invoked`), app_version, os, ci, `$insert_id` |
| `workflow_outcome` | A background workflow or direct evaluation reaches a terminal result inside Ouroboros | command, terminal_status, verified, failure_reason_code (non-success only), runtime_backend, app_version, os, ci, `$insert_id` |
| `ac_verify_failed` | The orchestrator's deterministic AC verify gate rejects an attempt (`run_verify_commands` enabled) | cause (closed enum: `invalid_contract`/`artifacts_missing`/`artifacts_missing_found_elsewhere`/`environment_unverifiable`/`timeout`/`exit_nonzero`/`output_assertion_unmatched`/`workspace_mutated`/`unknown`), runtime_backend, app_version, os, ci |

Notes:

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
