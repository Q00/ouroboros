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
{ "command": "uvx",
  "args": ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]",
           "ouroboros", "mcp", "serve", "..."] }
```

So if you clone the repo, edit a handler, and open your agent client in the
project directory, the server that answers is the last release — your change
has no effect and nothing warns you. Same for `uvx ouroboros ...` on the
command line.

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

The `--python '>=3.12'` matters. `uvx`/`uv tool` otherwise resolve against the
machine's default interpreter, and on a 3.11 box the MCP server dies before it
can answer `initialize`. Any launcher you generate must carry the same floor.

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


To make a client use it, replace the `ouroboros` entry in your client's MCP
config — `~/.claude/mcp.json` for Claude Code, or the project `.mcp.json` —
with the local form, and **preserve every other server entry in the file**:

```json
"ouroboros": {
  "command": "uv",
  "args": ["run", "--directory", "/path/to/your/clone", "--group", "mcp-test", "ouroboros", "mcp", "serve", "--runtime", "claude-cli", "--llm-backend", "claude_code"],
  "timeout": 600
}
```

Connection and runtime selection are separate concepts. The example above
deliberately pins `--runtime` and `--llm-backend` so its smoke-test behavior is
deterministic. Remove those two option/value pairs if the server should inherit
the environment/config precedence documented below. Back up the client file
before editing it, preserve every unrelated server, and restore it when testing
ends so you do not silently keep running a stale branch weeks later.

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
| Event database | Generated config: `~/.ouroboros/data/ouroboros.db`; legacy fallback: `~/.ouroboros/ouroboros.db` | `persistence.database_path`, relative to the config directory unless absolute |
| Logs | `~/.ouroboros/logs/ouroboros.log` | No config-file path override; `logging.log_path` is persisted but is not consumed by the runtime logger |
| Worktrees created by runs | `~/.ouroboros/worktrees/` | `orchestrator.worktree_root` |

Event-store resolution is implemented by `resolve_event_store_path()` and
`event_store_path_from_config()` in
[`src/ouroboros/config/models.py`](../../src/ouroboros/config/models.py).
Managed worktrees resolve through `managed_worktree_root()` in
[`src/ouroboros/core/worktree.py`](../../src/ouroboros/core/worktree.py).
The event database and managed-worktree entries are defaults and compatibility
fallbacks, not invariant paths; check `config.yaml` before inspecting or cleaning
those resources. The runtime log destination is the fixed path shown above.

This state accumulates and it is not small — the event DB and its WAL grow
across runs, and abandoned run worktrees are the usual cause of a full disk.
Clean up with the built-in command, which checks locks and dirty trees:

```bash
uv run ouroboros cleanup --dry-run   # report only
uv run ouroboros cleanup --force
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

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/ --fix
uv run mypy src/ouroboros
uv run pytest
```

Then read [Review Conventions](./review-conventions.md) — the reviewer is
strict and predictable, and most rounds are lost to objections you can
preempt. Gate details are in [CI Gates](./ci-gates.md).
