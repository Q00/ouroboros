# CLI Reference

Complete command reference for the Ouroboros CLI.

## Installation

```bash
pip install ouroboros-ai
# or
uv pip install ouroboros-ai
```

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

## Commands Overview

| Command | Description |
|---------|-------------|
| `init` | Start interactive interview to refine requirements |
| `run` | Execute Ouroboros workflows |
| `config` | Manage Ouroboros configuration |
| `status` | Check Ouroboros system status |
| `tui` | Interactive TUI monitor for real-time workflow monitoring |
| `mcp` | MCP server commands for Claude Desktop integration |

---

## `ouroboros init`

Start interactive interview to refine requirements (Big Bang phase).

### `init start`

Start an interactive interview to transform vague ideas into clear, executable requirements.

```bash
ouroboros init start [OPTIONS] [CONTEXT]
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
| `-o, --orchestrator` | Use Claude Code (Max Plan) instead of LiteLLM. No API key required |

**Examples:**

```bash
# Start with initial idea (LiteLLM - requires API key)
ouroboros init start "I want to build a task management CLI tool"

# Start with Claude Code (no API key needed)
ouroboros init start --orchestrator "Build a REST API"

# Resume an interrupted interview
ouroboros init start --resume interview_20260116_120000

# Interactive mode (prompts for input)
ouroboros init start
```

### `init list`

List all interview sessions.

```bash
ouroboros init list
```

---

## `ouroboros run`

Execute Ouroboros workflows.

### `run workflow`

Execute a workflow from a seed file.

```bash
ouroboros run workflow [OPTIONS] SEED_FILE
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `SEED_FILE` | Yes | Path to the seed YAML file |

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --orchestrator` | Use Claude Agent SDK for execution (Epic 8 mode) |
| `-r, --resume TEXT` | Resume a previous orchestrator session by ID |
| `-n, --dry-run` | Validate seed without executing |
| `-v, --verbose` | Enable verbose output |

**Examples:**

```bash
# Standard workflow execution
ouroboros run workflow seed.yaml

# Orchestrator mode (Claude Agent SDK)
ouroboros run workflow --orchestrator seed.yaml

# Dry run (validate only)
ouroboros run workflow --dry-run seed.yaml

# Resume a previous session
ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml

# Verbose output
ouroboros run workflow --orchestrator --verbose seed.yaml
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

> **Note:** For orchestrator sessions, use:
> ```bash
> ouroboros run workflow --orchestrator --resume <session_id> seed.yaml
> ```

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
┌───────────────┬─────────┐
│ Database      │   ok    │
│ Configuration │   ok    │
│ Providers     │ warning │
└───────────────┴─────────┘
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
| `-e, --execution-id TEXT` | Monitor a specific execution |
| `-s, --session-id TEXT` | Monitor a specific session |

**Examples:**

```bash
# Launch TUI monitor
ouroboros tui monitor

# Monitor specific execution
ouroboros tui monitor --execution-id exec_abc123

# Monitor specific session
ouroboros tui monitor --session-id sess_xyz789
```

**TUI Screens:**

| Key | Screen | Description |
|-----|--------|-------------|
| `1` | Dashboard | Overview with phase progress, drift meter, cost tracker |
| `2` | Logs | Filterable log viewer with level filtering |
| `3` | Execution | Execution details, timeline, phase outputs |
| `4` | Debug | State inspector, raw events, configuration |

**Keyboard Shortcuts:**

| Key | Action |
|-----|--------|
| `1-4` | Switch screens |
| `q` | Quit |
| `p` | Pause execution |
| `r` | Resume execution |
| `↑/↓` | Scroll |

---

## `ouroboros mcp`

MCP (Model Context Protocol) server commands for Claude Desktop integration.

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

### Using Claude Code (Recommended)

No API key required - uses your Claude Code Max Plan subscription.

```bash
# 1. Check system health
ouroboros status health

# 2. Start interview to create seed
ouroboros init start --orchestrator "Build a user authentication system"

# 3. Execute the generated seed
ouroboros run workflow --orchestrator seed.yaml
```

### Using LiteLLM (External API)

Requires API key (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, etc.)

```bash
# 1. Initialize configuration
ouroboros config init

# 2. Set your API key
ouroboros config set providers.openrouter.api_key $OPENROUTER_API_KEY

# 3. Start interview
ouroboros init start "Build a REST API for task management"

# 4. Execute workflow
ouroboros run workflow seed.yaml
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENROUTER_API_KEY` | OpenRouter API key for LiteLLM |
| `ANTHROPIC_API_KEY` | Anthropic API key for LiteLLM |
| `OPENAI_API_KEY` | OpenAI API key for LiteLLM |

---

## Configuration Files

Ouroboros stores configuration in `~/.ouroboros/`:

| File | Description |
|------|-------------|
| `config.yaml` | Main configuration |
| `credentials.yaml` | API keys (chmod 600) |
| `ouroboros.db` | SQLite database for event sourcing |

---

## Exit Codes

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | General error |
| `2` | Configuration error |
| `3` | Validation error |
