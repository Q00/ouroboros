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
at least one successful verified run that week. Installs, reinstalls, retries,
and CI runs are not users. Published numbers follow this rule, verbatim.

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

## What is sent

Identity is a random UUID (`~/.ouroboros/telemetry.json`) generated on first
use — it is not derived from your machine, account, or network.

| Event | When | Properties |
|---|---|---|
| `install_started` | `install.sh` begins | os, arch, version, is_local, pre |
| `install_completed` | `install.sh` finishes | os, arch, method (uv/pipx/pip), runtime, detected_runtimes (count), version |
| `command_run` (source=mcp) | An `ouroboros_*` MCP tool is invoked from any host CLI (Claude Code, Codex, OpenCode, …) | command (interview/seed/run/evolve/auto/evaluate/qa/…), tool, ok, duration_ms, error_type, runtime_backend, execute_runtime_backend, interview_llm_backend, evaluate_llm_backend, frontdoor, app_version, os, python_version |
| `command_run` (source=cli) | A direct `ooo <subcommand>` invocation in a terminal | command, app_version, os, python_version |
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
- Events are sent to PostHog via a fire-and-forget background thread using a
  **public, write-only** project API key. Telemetry never blocks a command,
  never raises, and silently drops events when offline.

## Where the code lives

All collection logic is in [`src/ouroboros/telemetry.py`](src/ouroboros/telemetry.py)
(one file, stdlib only) and the `_telemetry_ping` function in
[`scripts/install.sh`](scripts/install.sh). If it's not in those two places,
it isn't collected.
