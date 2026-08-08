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
CI runs are not users. Published numbers follow this rule, verbatim.

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
`.env` cannot set `OUROBOROS_TELEMETRY`, `OUROBOROS_POSTHOG_HOST`, or
`OUROBOROS_POSTHOG_API_KEY`. Invalid or unreadable user configuration disables
collection rather than silently restoring the default. An explicit
`OUROBOROS_TELEMETRY=1` is never an override: any disabling control above —
including a persisted `enabled: false` or malformed configuration — still
wins. The installer honors the same controls, reading `~/.ouroboros/.env`
before its first notice or event.

## What is sent

Identity is a random UUID (`~/.ouroboros/telemetry.json`) generated on first
use — it is not derived from your machine, account, or network.

| Event | When | Properties |
|---|---|---|
| `install_started` | `install.sh` begins | os, arch, version, is_local, pre |
| `install_completed` | `install.sh` finishes | os, arch, method (uv/pipx/pip), runtime, detected_runtimes (count), version |
| `command_run` (source=mcp) | An `ouroboros_*` MCP tool is invoked from any host CLI (Claude Code, Codex, OpenCode, …) | command (interview/seed/run/evolve/auto/evaluate/qa/…), tool, phase (`submission` or `completion`), accepted (submission only), ok (completion only), duration_ms, error_type, runtime_backend, execute_runtime_backend, interview_llm_backend, evaluate_llm_backend, frontdoor, app_version, os, python_version |
| `command_run` (source=cli) | A direct `ooo <subcommand>` invocation in a terminal | command, app_version, os, python_version |
| `workflow_outcome` | A background MCP job reaches a durable terminal event | command, phase (`terminal`), terminal_status, ok, verified, final_approved, `$insert_id` (one-way event deduplication digest), runtime/LLM context, app_version, os, python_version |
| `mcp_serve_started` | A host CLI attaches the Ouroboros MCP server for a session | transport, tool_count, frontdoor, app_version, os |

Notes:

- `source` separates the two entry surfaces cleanly: `cli` means the user typed
  the command in a terminal; `mcp` means it arrived from inside an AI agent
  session (`ooo …` keyword in Claude Code / Codex / any MCP host). Internal
  machinery (in-process servers, detached job workers, `ouroboros mcp serve`
  boots, `job`/`dispatch` plumbing) is either not captured or captured as its
  own event, so the cli-vs-agent ratio is not inflated by automation.
- High-frequency polling tools (`ouroboros_job_status`, `ouroboros_session_status`,
  HUD/projection queries, …) are sampled at 1/50 and carry a `sample_rate`
  property so counts can be re-weighted; everything else is captured 1:1.
- `error_type` is only the Python exception class name (e.g. `TimeoutError`),
  never a message or traceback.
- Start-tool `command_run` events are submission receipts. They intentionally
  have `accepted`, not `ok`; queue acceptance is never a completed or verified
  run. `workflow_outcome` comes from the durable job-terminal boundary. Only a
  completed formal evaluation with explicit `final_approved=true` sets
  `verified=true`.
- Events are sent to PostHog via a fire-and-forget background thread using a
  **public, write-only** project API key. Telemetry never blocks a command,
  never raises, and silently drops events when offline.

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
- [`scripts/install.sh`](scripts/install.sh) — disclosed install start/completion.
