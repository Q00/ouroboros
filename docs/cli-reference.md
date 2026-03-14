# CLI Reference

Complete command reference for the Ouroboros CLI.

## Installation

```bash
pip install ouroboros-ai              # Base (core engine)
pip install ouroboros-ai[claude]      # + Claude Code runtime deps
pip install ouroboros-ai[litellm]     # + LiteLLM multi-provider support
pip install ouroboros-ai[all]         # Everything (claude + litellm + dashboard)
```

> **Codex CLI** is an external prerequisite installed separately (`npm install -g @openai/codex`). No Python extras are required for Codex -- the base `ouroboros-ai` package is sufficient.

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

```bash
# Set up Ouroboros (detects available runtimes)
ouroboros setup

# Start an interview to create a seed specification
ouroboros init "Build a REST API for task management"

# Execute the generated seed
ouroboros run seed.yaml

# Monitor execution in real-time
ouroboros monitor
```

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

Ouroboros supports multiple runtime backends. The `setup` command auto-detects
which runtimes are available in your PATH (Claude Code, Codex CLI) and
configures `orchestrator.runtime_backend` accordingly.

```bash
ouroboros setup [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-r, --runtime TEXT` | Runtime backend to configure (`claude`, `codex`). Auto-detected if omitted |
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

- Scans PATH for `claude` and `codex` CLI binaries
- Prompts you to select a runtime if multiple are found (or auto-selects if only one)
- Writes `orchestrator.runtime_backend` to `~/.ouroboros/config.yaml`
- For Claude Code: registers the MCP server in `~/.claude/mcp.json`
- For Codex CLI: sets `orchestrator.codex_cli_path` in config

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
| `-o, --orchestrator` | Use the configured runtime backend (Claude Code or Codex CLI) instead of LiteLLM |
| `--runtime TEXT` | Agent runtime backend for the workflow execution step after seed generation (`claude`, `codex`) |
| `--llm-backend TEXT` | LLM backend for interview, ambiguity scoring, and seed generation (`claude_code`, `litellm`, `codex`) |
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
ouroboros init list
```

---

## `ouroboros run`

Execute Ouroboros workflows.

**Shorthand:** `ouroboros run seed.yaml` is equivalent to `ouroboros run workflow seed.yaml`.
When the first argument is not a known subcommand (`workflow`, `resume`), it is treated as the seed file for `run workflow`.

**Default mode:** Orchestrator mode is enabled by default. Use `--no-orchestrator` for legacy standard mode.

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
| `--orchestrator/--no-orchestrator` | Use the agent-runtime orchestrator for execution (default: enabled) |
| `--runtime TEXT` | Agent runtime backend override (`claude`, `codex`). Uses configured default if omitted |
| `-r, --resume TEXT` | Resume a previous orchestrator session by ID |
| `--mcp-config PATH` | Path to MCP client configuration YAML file |
| `--mcp-tool-prefix TEXT` | Prefix to add to all MCP tool names (e.g., `mcp_`) |
| `-s, --sequential` | Execute ACs sequentially instead of in parallel |
| `-n, --dry-run` | Validate seed without executing |
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

# Legacy standard mode (placeholder)
ouroboros run seed.yaml --no-orchestrator

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

Initialize Ouroboros configuration. Creates default configuration files if they don't exist.

```bash
ouroboros config init
```

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
# Set the runtime backend
ouroboros config set orchestrator.runtime_backend codex

# Set API key for a provider
ouroboros config set providers.openai.api_key sk-xxx

# Set nested configuration
ouroboros config set execution.max_retries 5
```

### `config validate`

Validate current configuration.

```bash
ouroboros config validate
```

---

## `ouroboros status`

Check Ouroboros system status.

### `status health`

Check system health. Verifies database connectivity, provider configuration, and system resources.

```bash
ouroboros status health
```

**Example Output:**

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

### `tui monitor`

Launch the interactive TUI monitor to observe workflow execution in real-time.

```bash
ouroboros tui monitor [OPTIONS]
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

**Keyboard Shortcuts:**

| Key | Action |
|-----|--------|
| `1-4` | Switch screens |
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

**Examples:**

```bash
# Start with stdio transport (for Claude Desktop)
ouroboros mcp serve

# Start with SSE transport on custom port
ouroboros mcp serve --transport sse --port 9000

# Start on specific host
ouroboros mcp serve --host 0.0.0.0 --port 8080 --transport sse
```

**Claude Desktop Integration:**

Add to `~/.config/claude/config.json`:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "ouroboros",
      "args": ["mcp", "serve"]
    }
  }
}
```

### `mcp info`

Show MCP server information and available tools.

```bash
ouroboros mcp info
```

**Available Tools:**

| Tool | Description |
|------|-------------|
| `ouroboros_execute_seed` | Execute a seed specification |
| `ouroboros_session_status` | Get the status of a session |
| `ouroboros_query_events` | Query event history |

---

## Typical Workflows

### First-Time Setup

```bash
# 1. Set up Ouroboros (auto-detects Claude Code or Codex CLI)
ouroboros setup

# 2. Check system health
ouroboros status health

# 3. Start interview to create seed
ouroboros init "Build a user authentication system"

# 4. Execute the generated seed
ouroboros run seed.yaml

# 5. Monitor in real-time
ouroboros monitor
```

### Using Claude Code Runtime

No API key required -- uses your Claude Code Max Plan subscription.

```bash
ouroboros setup --runtime claude
ouroboros init --orchestrator "Build a REST API"
ouroboros run seed.yaml
```

### Using Codex CLI Runtime

Requires an OpenAI API key (set via `OPENAI_API_KEY`).

```bash
ouroboros setup --runtime codex
ouroboros init "Build a REST API"
ouroboros run seed.yaml --runtime codex
```

### Using LiteLLM (External API)

Requires API key (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, etc.)

```bash
# 1. Initialize configuration
ouroboros config init

# 2. Set your API key
ouroboros config set providers.openrouter.api_key $OPENROUTER_API_KEY

# 3. Start interview
ouroboros init "Build a REST API for task management"

# 4. Execute workflow (use --no-orchestrator for LiteLLM path)
ouroboros run seed.yaml --no-orchestrator
```

### Cancelling Stuck Executions

```bash
# Interactive: list and pick
ouroboros cancel execution

# Cancel all at once
ouroboros cancel execution --all
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENROUTER_API_KEY` | OpenRouter API key for LiteLLM |
| `ANTHROPIC_API_KEY` | Anthropic API key for LiteLLM |
| `OPENAI_API_KEY` | OpenAI API key for LiteLLM / Codex CLI |

---

## Configuration Files

Ouroboros stores configuration in `~/.ouroboros/`:

| File | Description |
|------|-------------|
| `config.yaml` | Main configuration (includes `orchestrator.runtime_backend`) |
| `credentials.yaml` | API keys (chmod 600) |
| `ouroboros.db` | SQLite database for event sourcing |

---

## Exit Codes

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | General error |
