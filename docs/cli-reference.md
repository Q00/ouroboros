<!--
doc_metadata:
  runtime_scope: [local, claude, codex]
-->

# CLI Reference

Complete command reference for the Ouroboros CLI.

## Installation

> For install instructions, onboarding, and first-run setup, see **[Getting Started](getting-started.md)**.

## Usage

```bash
ouroboros [OPTIONS] COMMAND [ARGS]...
```

### Global Options

| Option | Description |
|--------|-------------|
| `-V, --version` | Show version and exit |
| `--install-completion` | Install shell completion |
| `--show-completion` | Show shell completion script |
| `--help` | Show help message |

---

## Quick Start

> For the full first-run walkthrough (interview → seed → execute), see **[Getting Started](getting-started.md)**.

---

## Commands Overview

| Command | Description |
|---------|-------------|
| `setup` | Detect runtimes and configure Ouroboros for your environment |
| `init` | Start interactive interview to refine requirements |
| `run` | Execute Ouroboros workflows |
| `cancel` | Cancel stuck or orphaned executions |
| `config` | Manage Ouroboros configuration |
| `status` | Check Ouroboros system status |
| `tui` | Interactive TUI monitor for real-time workflow monitoring |
| `monitor` | Shorthand for `tui monitor` |
| `mcp` | MCP server commands for Claude Desktop and other MCP clients |

---

## `ouroboros setup`

Detect available runtime backends and configure Ouroboros for your environment.

Ouroboros supports multiple runtime backends via a pluggable `AgentRuntime` protocol. The `setup` command auto-detects
which runtimes are available in your PATH (currently: Claude Code, Codex CLI) and
configures `orchestrator.runtime_backend` accordingly. Additional runtimes can be registered
by implementing the protocol — see [Architecture](architecture.md#how-to-add-a-new-runtime-adapter).

```bash
ouroboros setup [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-r, --runtime TEXT` | Runtime backend to configure. Shipped values: `claude`, `codex`. Auto-detected if omitted |
| `--non-interactive` | Skip interactive prompts (for scripted installs) |

**Examples:**

```bash
# Auto-detect runtimes and configure interactively
ouroboros setup

# Explicitly select Codex CLI as runtime backend
ouroboros setup --runtime codex

# Explicitly select Claude Code as runtime backend
ouroboros setup --runtime claude

# Non-interactive setup (for CI or scripted installs)
ouroboros setup --non-interactive
```

**What setup does:**

- Scans PATH for `claude`, `codex`, and `opencode` CLI binaries
- Prompts you to select a runtime if multiple are found (or auto-selects if only one)
- Writes `orchestrator.runtime_backend` to `~/.ouroboros/config.yaml`
- For Claude Code: registers the MCP server in `~/.claude/mcp.json`
- For Codex CLI: sets `orchestrator.codex_cli_path` in config
- For Codex CLI: does **not** currently install global `~/.codex/` rules or skills

> **`opencode` caveat:** `setup` detects the `opencode` binary in PATH but cannot configure it — if `opencode` is your only installed runtime, `setup` exits with `Error: Unsupported runtime: opencode`. To use `opencode`, set `orchestrator.runtime_backend: opencode` manually in `~/.ouroboros/config.yaml`.

---

## `ouroboros init`

Start interactive interview to refine requirements (Big Bang phase).

**Shorthand:** `ouroboros init "context"` is equivalent to `ouroboros init start "context"`.
When the first argument is not a known subcommand (`start`, `list`), it is treated as the context for `init start`.

### `init start`

Start an interactive interview to transform vague ideas into clear, executable requirements.

```bash
ouroboros init [start] [OPTIONS] [CONTEXT]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `CONTEXT` | Initial context or idea (interactive prompt if not provided) |

**Options:**

| Option | Description |
|--------|-------------|
| `-r, --resume TEXT` | Resume an existing interview by ID |
| `--state-dir DIRECTORY` | Custom directory for interview state files |
| `-o, --orchestrator` | Use Claude Code for the interview/seed flow; combine with `--runtime` to choose the workflow handoff backend |
| `--runtime TEXT` | Agent runtime backend for the workflow execution step after seed generation. Shipped values: `claude`, `codex`. `opencode` appears in the CLI enum but is out of scope. Custom adapters registered in `runtime_factory.py` are also accepted. |
| `--llm-backend TEXT` | LLM backend for interview, ambiguity scoring, and seed generation (`claude_code`, `litellm`, `codex`). `opencode` appears in the CLI enum but is out of scope |
| `-d, --debug` | Show verbose logs including debug messages |

**Examples:**

```bash
# Shorthand (recommended) -- 'start' subcommand is implied
ouroboros init "I want to build a task management CLI tool"

# Explicit subcommand (equivalent)
ouroboros init start "I want to build a task management CLI tool"

# Start with Claude Code (no API key needed)
ouroboros init --orchestrator "Build a REST API"

# Specify runtime backend for the workflow step
ouroboros init --orchestrator --runtime codex "Build a REST API"

# Use Codex as the LLM backend for interview and seed generation
ouroboros init --llm-backend codex "Build a REST API"

# Resume an interrupted interview
ouroboros init start --resume interview_20260116_120000

# Interactive mode (prompts for input)
ouroboros init
```

### `init list`

List all interview sessions.

```bash
ouroboros init list [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--state-dir DIRECTORY` | Custom directory for interview state files |

---

## `ouroboros run`

Execute Ouroboros workflows.

**Shorthand:** `ouroboros run seed.yaml` is equivalent to `ouroboros run workflow seed.yaml`.
When the first argument is not a known subcommand (`workflow`, `resume`), it is treated as the seed file for `run workflow`.

**Default mode:** Orchestrator mode is enabled by default. `--no-orchestrator` exists for the legacy standard path, which is still placeholder-oriented.

### `run workflow`

Execute a workflow from a seed file.

```bash
ouroboros run [workflow] [OPTIONS] SEED_FILE
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `SEED_FILE` | Yes | Path to the seed YAML file |

**Options:**

| Option | Description |
|--------|-------------|
| `-o/-O, --orchestrator/--no-orchestrator` | Use the agent-runtime orchestrator for execution (default: enabled) |
| `--runtime TEXT` | Agent runtime backend override (`claude`, `codex`). Uses configured default if omitted. (`opencode` is in the CLI enum but out of scope) |
| `-r, --resume TEXT` | Resume a previous orchestrator session by ID |
| `--mcp-config PATH` | Path to MCP client configuration YAML file |
| `--mcp-tool-prefix TEXT` | Prefix to add to all MCP tool names (e.g., `mcp_`) |
| `-s, --sequential` | Execute ACs sequentially instead of in parallel |
| `-n, --dry-run` | Validate seed without executing. **Currently only takes effect with `--no-orchestrator`.** In default orchestrator mode this flag is accepted but has no effect — the full workflow executes |
| `--no-qa` | Skip post-execution QA evaluation |
| `-d, --debug` | Show logs and agent thinking (verbose output) |

**Examples:**

```bash
# Run a workflow (shorthand, recommended)
ouroboros run seed.yaml

# Explicit subcommand (equivalent)
ouroboros run workflow seed.yaml

# Use Codex CLI as the runtime backend
ouroboros run seed.yaml --runtime codex

# With MCP server integration
ouroboros run seed.yaml --mcp-config mcp.yaml

# Resume a previous session
ouroboros run seed.yaml --resume orch_abc123

# Skip post-execution QA
ouroboros run seed.yaml --no-qa

# Debug output
ouroboros run seed.yaml --debug

# Sequential execution (one AC at a time)
ouroboros run seed.yaml --sequential
```

### `run resume`

Resume a paused or failed execution.

> **Current state:** `run resume` is a placeholder helper. For real orchestrator sessions, use `ouroboros run seed.yaml --resume <session_id>`.

```bash
ouroboros run resume [EXECUTION_ID]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `EXECUTION_ID` | Execution ID to resume (uses latest if not specified) |

> **Note:** For orchestrator sessions, you can also use:
> ```bash
> ouroboros run seed.yaml --resume <session_id>
> ```

---

## `ouroboros cancel`

Cancel stuck or orphaned executions.

### `cancel execution`

Cancel a specific execution, all running executions, or interactively pick from active sessions.

```bash
ouroboros cancel execution [OPTIONS] [EXECUTION_ID]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `EXECUTION_ID` | Session/execution ID to cancel. If omitted, enters interactive mode |

**Options:**

| Option | Description |
|--------|-------------|
| `-a, --all` | Cancel all running/paused executions |
| `-r, --reason TEXT` | Reason for cancellation (default: "Cancelled by user via CLI") |

**Examples:**

```bash
# Interactive mode - list active executions and pick one
ouroboros cancel execution

# Cancel a specific execution by session ID
ouroboros cancel execution orch_abc123def456

# Cancel all running executions
ouroboros cancel execution --all

# Cancel with a custom reason
ouroboros cancel execution orch_abc123 --reason "Stuck for 2 hours"
```

---

## `ouroboros config`

Manage Ouroboros configuration.

> **Current state:** the `config` subcommands are scaffolding. They currently print placeholder output and do not mutate `~/.ouroboros/config.yaml`. Use `ouroboros setup` for initial runtime setup, then edit `~/.ouroboros/config.yaml` directly for manual changes.

### `config show`

Display current configuration.

```bash
ouroboros config show [SECTION]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `SECTION` | Configuration section to display (e.g., `providers`) |

**Examples:**

```bash
# Show all configuration
ouroboros config show

# Show only providers section
ouroboros config show providers
```

### `config init`

Initialize Ouroboros configuration.

```bash
ouroboros config init
```

Creates `~/.ouroboros/config.yaml` and `~/.ouroboros/credentials.yaml` with default templates. Sets `chmod 600` on `credentials.yaml`. If the files already exist they are not overwritten.

### `config set`

Set a configuration value.

```bash
ouroboros config set KEY VALUE
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY` | Yes | Configuration key (dot notation) |
| `VALUE` | Yes | Value to set |

**Examples:**

```bash
# Placeholder command surface (does not yet write files)
ouroboros config set orchestrator.runtime_backend codex
```

### `config validate`

Validate current configuration.

```bash
ouroboros config validate
```

---

## `ouroboros status`

Check Ouroboros system status.

> **Current state:** the `status` subcommands return lightweight placeholder summaries. They are useful for smoke testing the command surface, but should not be treated as authoritative orchestration state.

### `status health`

Check system health. Verifies database connectivity, provider configuration, and system resources.

```bash
ouroboros status health
```

**Representative Output:**

```
+---------------+---------+
| Database      |   ok    |
| Configuration |   ok    |
| Providers     | warning |
+---------------+---------+
```

### `status executions`

List recent executions with status information.

```bash
ouroboros status executions [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-n, --limit INTEGER` | Number of executions to show (default: 10) |
| `-a, --all` | Show all executions |

**Examples:**

```bash
# Show last 10 executions
ouroboros status executions

# Show last 5 executions
ouroboros status executions -n 5

# Show all executions
ouroboros status executions --all
```

### `status execution`

Show details for a specific execution.

```bash
ouroboros status execution [OPTIONS] EXECUTION_ID
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `EXECUTION_ID` | Yes | Execution ID to inspect |

**Options:**

| Option | Description |
|--------|-------------|
| `-e, --events` | Show execution events |

**Examples:**

```bash
# Show execution details
ouroboros status execution exec_abc123

# Show execution with events
ouroboros status execution --events exec_abc123
```

---

## `ouroboros tui`

Interactive TUI monitor for real-time workflow monitoring.

> **Equivalent invocations:** `ouroboros tui` (no subcommand), `ouroboros tui monitor`, and `ouroboros monitor` are all equivalent — they all launch the TUI monitor.

### `tui monitor`

Launch the interactive TUI monitor to observe workflow execution in real-time.

```bash
ouroboros tui [monitor] [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--db-path PATH` | Path to the Ouroboros database file (default: `~/.ouroboros/ouroboros.db`) |
| `--backend TEXT` | TUI backend to use: `textual` (default) or `slt` (native Rust) |

**Examples:**

```bash
# Launch TUI monitor (default Textual backend)
ouroboros tui monitor

# Monitor with a specific database file
ouroboros tui monitor --db-path ~/.ouroboros/ouroboros.db

# Use the native SLT backend (requires ouroboros-tui binary)
ouroboros tui monitor --backend slt
```

> **Note:** The `slt` backend requires the `ouroboros-tui` binary in your PATH. Install it with:
> ```bash
> cd crates/ouroboros-tui && cargo install --path .
> ```

**TUI Screens:**

| Key | Screen | Description |
|-----|--------|-------------|
| `1` | Dashboard | Overview with phase progress, drift meter, cost tracker |
| `2` | Execution | Execution details, timeline, phase outputs |
| `3` | Logs | Filterable log viewer with level filtering |
| `4` | Debug | State inspector, raw events, configuration |
| `s` | Session Selector | Browse and switch between monitored sessions |
| `e` | Lineage | View evolutionary lineage across generations (evolve/ralph) |

**Keyboard Shortcuts:**

| Key | Action |
|-----|--------|
| `1-4` | Switch to numbered screen |
| `s` | Session Selector |
| `e` | Lineage view |
| `q` | Quit |
| `p` | Pause execution |
| `r` | Resume execution |
| Up/Down | Scroll |

---

## `ouroboros mcp`

MCP (Model Context Protocol) server commands for Claude Desktop and other MCP-compatible clients.

### `mcp serve`

Start the MCP server to expose Ouroboros tools to Claude Desktop or other MCP clients.

```bash
ouroboros mcp serve [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-h, --host TEXT` | Host to bind to (default: localhost) |
| `-p, --port INTEGER` | Port to bind to (default: 8080) |
| `-t, --transport TEXT` | Transport type: `stdio` or `sse` (default: stdio) |
| `--db TEXT` | Path to the EventStore database file |
| `--runtime TEXT` | Runtime backend for orchestrator-driven tools (`claude`, `codex`). (`opencode` is in the CLI enum but out of scope) |
| `--llm-backend TEXT` | LLM backend for interview/seed/evaluation tools (`claude_code`, `litellm`, `codex`). (`opencode` is in the CLI enum but out of scope) |

**Examples:**

```bash
# Start with stdio transport (for Claude Desktop)
ouroboros mcp serve

# Start with SSE transport on custom port
ouroboros mcp serve --transport sse --port 9000

# Start with Codex-backed orchestrator tools
ouroboros mcp serve --runtime codex --llm-backend codex

# Start on specific host
ouroboros mcp serve --host 0.0.0.0 --port 8080 --transport sse
```

**Startup behavior:**

On startup, `mcp serve` automatically cancels any sessions left in `RUNNING` or `PAUSED` state for more than 1 hour. These are treated as orphaned from a previous crash. Cancelled sessions are reported on stderr (or console when using SSE transport). This cleanup is best-effort and does not prevent the server from starting if it fails.

**Claude Desktop / Claude Code CLI Integration:**

`ouroboros setup --runtime claude` writes this automatically to `~/.claude/mcp.json`.
To register manually, add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
      "timeout": 600
    }
  }
}
```

If Ouroboros is installed directly (not via `uvx`), use:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "ouroboros",
      "args": ["mcp", "serve"],
      "timeout": 600
    }
  }
}
```

**Runtime selection** is configured in `~/.ouroboros/config.yaml` (written by `ouroboros setup`):

```yaml
orchestrator:
  runtime_backend: claude   # or "codex"
```

Override per-session with the `OUROBOROS_AGENT_RUNTIME` environment variable if needed.

### `mcp info`

Show MCP server information and available tools.

```bash
ouroboros mcp info [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--runtime TEXT` | Agent runtime backend for orchestrator-driven tools (`claude`, `codex`). Affects which tool variants are instantiated |
| `--llm-backend TEXT` | LLM backend for interview/seed/evaluation tools (`claude_code`, `litellm`, `codex`). Affects which tool variants are instantiated |

**Available Tools:**

| Tool | Description |
|------|-------------|
| `ouroboros_execute_seed` | Execute a seed specification |
| `ouroboros_session_status` | Get the status of a session |
| `ouroboros_query_events` | Query event history |

---

## Typical Workflows

> For first-time setup and the complete onboarding flow, see **[Getting Started](getting-started.md)**.
> For runtime-specific configuration, see the [Claude Code](runtime-guides/claude-code.md) and [Codex CLI](runtime-guides/codex.md) runtime guides.

### Cancelling Stuck Executions

```bash
# Interactive: list and pick
ouroboros cancel execution

# Cancel all at once
ouroboros cancel execution --all
```

---

## Environment Variables

The table below covers the most commonly used variables. For the full list — including all per-model overrides (e.g., `OUROBOROS_QA_MODEL`, `OUROBOROS_SEMANTIC_MODEL`, `OUROBOROS_CONSENSUS_MODELS`, etc.) — see [config-reference.md](config-reference.md#environment-variables).

| Variable | Overrides config key | Description |
|----------|----------------------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key for Claude models |
| `OPENAI_API_KEY` | — | OpenAI API key for LiteLLM / Codex CLI |
| `OPENROUTER_API_KEY` | — | OpenRouter API key for consensus and LiteLLM |
| `OUROBOROS_AGENT_RUNTIME` | `orchestrator.runtime_backend` | Override the runtime backend (`claude`, `codex`) |
| `OUROBOROS_AGENT_PERMISSION_MODE` | `orchestrator.permission_mode` | Permission mode for non-OpenCode runtimes |
| `OUROBOROS_LLM_BACKEND` | `llm.backend` | Override the LLM-only flow backend |
| `OUROBOROS_CLI_PATH` | `orchestrator.cli_path` | Path to the Claude CLI binary |
| `OUROBOROS_CODEX_CLI_PATH` | `orchestrator.codex_cli_path` | Path to the Codex CLI binary |

---

## Configuration Files

Ouroboros stores configuration in `~/.ouroboros/`:

| File | Description |
|------|-------------|
| `config.yaml` | Main configuration — see [config-reference.md](config-reference.md) for all options |
| `credentials.yaml` | API keys (chmod 600; created by `ouroboros config init`) |
| `ouroboros.db` | SQLite database for event sourcing (actual path: `~/.ouroboros/ouroboros.db`; the `persistence.database_path` config key is currently not honored — see [config-reference.md](config-reference.md#persistence)) |
| `logs/ouroboros.log` | Log output (path configurable via `logging.log_path`) |

---

## Exit Codes

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | General error |
