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
  `command=evaluate`, `verified=true`, and `ci!=true` that week.

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
| `service_active` | An MCP service starts | service (`mcp`), runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=mcp) | A retained lifecycle MCP command succeeds/is accepted, or any MCP command fails/is blocked | command, service, status (`succeeded`, `accepted`, `failed`, `rejected`, `blocked`), error_type (exception failures only), runtime_backend, app_version, os, ci, `$insert_id` |
| `command_run` (service=cli) | A direct non-internal `ooo <command>` is invoked | command, service (`cli`), status (`invoked`), app_version, os, ci, `$insert_id` |
| `workflow_outcome` | A background workflow or direct evaluation reaches a terminal result | command, terminal_status, verified, failure_reason_code (non-success only), runtime_backend, app_version, os, ci, `$insert_id` |

Notes:

- `ref` is a short install-channel token such as `readme` or `hellogithub`.
  Invalid or absent values become `direct`.
- Successful polling and internal helper tools are not collected. Failures are
  retained so broken status, artifact, fan-out, and control paths remain visible.
- A logical tool result with `is_error=true` is recorded as `status=blocked`.
  This makes seed blocks and other validation stops distinct from exceptions.
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
