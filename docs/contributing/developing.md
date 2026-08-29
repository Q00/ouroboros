# The Development Loop

You cloned this repo to change something. This page is about the shortest path
from an edit to seeing that edit actually run.

Read it before the architecture docs. Knowing where a module lives does not
help if the code you are running is not the code you edited — which, by
default in this repository, it is not.

## The trap: by default you are not running your own code

The checked-in `.mcp.json` points an MCP client at the **published PyPI
package**, not at your working tree:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": [
        "--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]",
        "ouroboros", "mcp", "serve",
        "--runtime", "claude-cli", "--llm-backend", "claude_code"
      ]
    }
  }
}
```

So if you clone the repo, edit a handler, and open your agent client in the
project directory, the server that answers is the last release — your change
has no effect and nothing warns you. The isolated, distribution-qualified
command `uvx --isolated --from ouroboros-ai ouroboros ...` likewise runs the
published release on the command line instead of reusing an installed tool
environment.

Point the tooling at your working tree instead. There are two surfaces, and
which one you need depends on what you changed.

### Surface 1 — the CLI

The package installs three console scripts (see `[project.scripts]` in
[`pyproject.toml`](../../pyproject.toml)):

| Script | Entry point |
|---|---|
| `ooo` | `ouroboros.cli.main:app` |
| `ouroboros` | `ouroboros.cli.main:app` |
| `ozo` | `ouroboros.cli.commands.zcode:app` |

Inside the repo, `uv run` already resolves to your working tree — no install
step, no staleness:

```bash
uv run ouroboros --version
uv run ooo status
```

To make your working tree the `ooo` on your `PATH` everywhere (useful when a
client spawns the binary for you), install the local package with its isolated
MCP 2 dependency profile:

```bash
uv tool install --force --with 'mcp==2.0.0' --from . ouroboros-ai --python '>=3.12'
```

The exact `--with 'mcp==2.0.0'` pin supplies the separate MCP 2 SDK without
bypassing the repository's reviewed version. Keep it synchronized with the
`mcp` optional dependency and `mcp-test` group in `pyproject.toml`. Do not add
the MCP 1.x `[claude]`, `[claude-sdk]`, or `[all]` profiles to this
environment. Re-run the command after edits because a tool install is a
snapshot; use `uv run` below when every invocation should reflect the working tree.

The `--python '>=3.12'` option repeats the package's compatibility floor at
the global tool-install boundary. uv can discover or download a compatible
interpreter when needed, and this package declares `requires-python = ">=3.12"`.
The range does not pin an exact Python version. The project-scoped `uv run`
commands below resolve through the checkout's Python requirement and do not need
the same flag.

### Surface 2 — the MCP server

Most of this project's behavior reaches a user through MCP, so this is the
surface you will usually need. MCP 2 is intentionally a separate dependency
profile: the default `dev` group does not install it. Select the repository's
`mcp-test` group when running the server straight from your working tree:

```bash
uv run --directory /path/to/your/clone --group mcp-test \
  ouroboros mcp serve --runtime claude-cli --llm-backend claude_code
```

This executes the local package and supplies `mcp==2.0.0`. Do not combine this
profile with the MCP 1.x `[claude]`, `[claude-sdk]`, or `[all]` profiles. The
command is implemented by `serve()` in
[`src/ouroboros/cli/commands/mcp.py`](../../src/ouroboros/cli/commands/mcp.py).


To make Claude Code use it without editing the checked-in `.mcp.json`, add a
local-scoped server with the same name. Claude Code stores local scope under the
project entry in `~/.claude.json`, and local scope takes precedence over the
project-scoped server:

```bash
claude mcp add --scope local --transport stdio ouroboros -- \
  uv run --directory /path/to/your/clone --group mcp-test \
  ouroboros mcp serve --runtime claude-cli --llm-backend claude_code
```

Use `claude mcp get ouroboros` to inspect the registered command. Claude Code's
supported scopes and storage locations are documented in
[Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp).
For another client, use that client's documented MCP registration surface
rather than assuming Claude Code's file locations.

Connection and runtime selection are separate concepts. The example above
deliberately pins `--runtime` and `--llm-backend` so its smoke-test behavior is
deterministic. Remove those two option/value pairs if the server should inherit
the environment/config precedence documented below. Remove the local override
when testing ends so you do not silently keep running a stale branch weeks later.

**Restart the client after changing MCP config.** Nothing hot-reloads.

Before restarting it, run the exact serve command by hand. A successful startup
prints `MCP Server starting on stdio...` and `Registered ... tools` to stderr;
press Ctrl+C after those lines appear. An immediate `MCP dependencies not
installed` error means the launcher still omitted the MCP profile.

> If the server fails to start, suspect a *different* server first. One broken
> entry in the client's MCP config can take the whole startup down. Run the
> serve command by hand and read the first ~40 lines of output.

## Runtime selection is separate from the connection

Config file: `~/.ouroboros/config.yaml`.

```yaml
llm:
  backend: claude_code      # claude_code | codex | litellm | opencode
orchestrator:
  runtime_backend: claude   # which agent runtime executes work
```

The agent runtime and the LLM backend are separate selectors with separate
precedence rules. The resolvers are `get_agent_runtime_backend()` and
`get_llm_backend()` in
[`src/ouroboros/config/loader.py`](../../src/ouroboros/config/loader.py).

Agent runtime precedence:

1. `OUROBOROS_AGENT_RUNTIME`
2. `OUROBOROS_RUNTIME`
3. `orchestrator.runtime_backend` in `config.yaml`
4. the built-in default, `claude`

LLM backend precedence:

1. `OUROBOROS_LLM_BACKEND`
2. `OUROBOROS_RUNTIME`, only when its value names a backend that implements the
   LLM adapter contract
3. `llm.backend` in `config.yaml`
4. the built-in default, `claude_code`

An explicit `mcp serve --runtime` or `--llm-backend` option selects that server
process directly. Otherwise, if a selection appears to ignore YAML, inspect the
client launcher's environment and your shell's `OUROBOROS_*` variables before
assuming the config loader is broken.

## Where state and output go

| What | Default or fallback | Override |
|---|---|---|
| Config | `~/.ouroboros/config.yaml` | No dedicated override; follows the effective home directory |
| Event database | Generated config: `~/.ouroboros/data/ouroboros.db`; legacy fallback: `~/.ouroboros/ouroboros.db` | `mcp serve --db PATH` for that server process; otherwise `persistence.database_path`, relative to the config directory unless absolute |
| Logs | `~/.ouroboros/logs/ouroboros.log` | No config-file path override; `logging.log_path` is persisted but is not consumed by the runtime logger |
| Worktrees created by runs | `~/.ouroboros/worktrees/` | `orchestrator.worktree_root` |

Event-store resolution is implemented by `resolve_event_store_path()` and
`event_store_path_from_config()` in
[`src/ouroboros/config/models.py`](../../src/ouroboros/config/models.py).
Managed worktrees resolve through `managed_worktree_root()` in
[`src/ouroboros/core/worktree.py`](../../src/ouroboros/core/worktree.py).
The event database and managed-worktree entries are defaults and compatibility
fallbacks, not invariant paths. For an MCP server, inspect the active client
launch command first: an explicit `mcp serve --db PATH` selects that process's
EventStore instead of the config-resolved path. Otherwise check `config.yaml`
before inspecting those resources. The runtime log destination is the fixed
path shown above.

The event database and its WAL can grow across runs. `ouroboros cleanup`
does not checkpoint, vacuum, truncate, or remove either file; database
maintenance is a separate operation and has no supported cleanup command in
this guide.

The built-in command prunes managed auto-session worktrees and their merged
branches, stale locks, and orphaned `auto_*.json` session state. It checks live
locks and dirty worktrees before removing anything:

```bash
uv run ouroboros cleanup --dry-run   # report only
uv run ouroboros cleanup --force     # also remove clean, unmerged worktrees
uv run ouroboros cleanup --state-all # include blocked/failed session state
```

Never delete the configured worktree root by hand — a live run may hold one.


## Fastest verification per change type

| You changed | Minimum to see it work | Client restart? |
|---|---|---|
| Pure Python (no MCP surface) | `uv run pytest tests/unit/<area>` | no |
| CLI command or flag | `uv run ooo <command>` | no |
| MCP tool handler | `uv run --group mcp-test pytest tests/unit/mcp -q`, then point the client at local source and call the tool | **yes** |
| `SKILL.md` / markdown | depends on dev-mode vs installed-plugin resolution | usually yes |

Scope your test runs while iterating:

```bash
uv run pytest tests/unit/<area> -q
```

For a broad local run, keep external MCP integration and end-to-end coverage
separate while retaining the hermetic MCP unit suite:

```bash
uv run --group mcp-test pytest tests/ --ignore=tests/integration/mcp \
  --ignore=tests/e2e -n auto --dist worksteal
```

`tests/conftest.py` redirects `$HOME` before collection and gives every test a
separate home, so `tests/unit/mcp` cannot write to your real config, event DB,
logs, or worktrees. Run that suite while changing MCP handlers; reserve the
external integration and end-to-end suites for environments that provide their
required services. See [Testing Guide](./testing-guide.md).


## Before you open the PR

Run the current, non-mutating PR-parity commands in
[Testing Guide](./testing-guide.md#before-opening-a-pr). Conditional gates,
branch protection and release behavior live in [CI Gates](./ci-gates.md).
Then read [Review Conventions](./review-conventions.md) before the first push.
