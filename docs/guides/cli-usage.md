# CLI Usage Guide

Ouroboros provides a command-line interface built with Typer and Rich for interactive workflow management.

## Installation

The CLI is installed automatically with the Ouroboros package:

```bash
# Using uv (recommended)
uv sync
uv run ouroboros --help

# Using pip
pip install ouroboros
ouroboros --help
```

## Global Options

```bash
ouroboros [OPTIONS] COMMAND [ARGS]
```

| Option | Description |
|--------|-------------|
| `--version`, `-V` | Show version and exit |
| `--help` | Show help message |

---

## Commands Overview

| Command | Description |
|---------|-------------|
| `ouroboros init` | Start interactive interview (Big Bang phase) |
| `ouroboros run` | Execute workflows |
| `ouroboros config` | Manage configuration |
| `ouroboros status` | Check system status |
| `ouroboros tui` | Interactive TUI monitor |
| `ouroboros mcp` | MCP server commands |

---

## `ouroboros init` - Interview Commands

The `init` command group manages the Big Bang interview phase.

### `ouroboros init start`

Start an interactive interview to refine requirements.

```bash
ouroboros init [CONTEXT] [OPTIONS]
```

| Argument | Description |
|----------|-------------|
| `CONTEXT` | Initial context or idea (optional, prompts if not provided) |

| Option | Description |
|--------|-------------|
| `--resume`, `-r ID` | Resume an existing interview by ID |
| `--state-dir PATH` | Custom directory for interview state files |

#### Examples

```bash
# Start new interview with initial context
ouroboros init "I want to build a task management CLI tool"

# Start new interview interactively
ouroboros init

# Resume a previous interview
ouroboros init --resume interview_20260125_120000

# Use custom state directory
ouroboros init --state-dir /path/to/states "Build a REST API"
```

#### Interview Process

1. Ouroboros asks clarifying questions
2. You provide answers
3. After 3+ rounds, you can choose to continue or finish early
4. Interview completes when ambiguity score <= 0.2
5. State is saved for later seed generation

### `ouroboros init list`

List all interview sessions.

```bash
ouroboros init list [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--state-dir PATH` | Custom directory for interview state files |

#### Example

```bash
ouroboros init list
```

Output:
```
Interview Sessions:

interview_20260125_120000 completed (5 rounds)
  Updated: 2026-01-25 12:15:00

interview_20260124_090000 in_progress (3 rounds)
  Updated: 2026-01-24 09:30:00
```

---

## `ouroboros run` - Execution Commands

The `run` command group executes workflows.

### `ouroboros run workflow`

Execute a workflow from a seed file.

```bash
ouroboros run workflow SEED_FILE [OPTIONS]
```

| Argument | Description |
|----------|-------------|
| `SEED_FILE` | Path to the seed YAML file |

| Option | Description |
|--------|-------------|
| `--orchestrator`, `-o` | Use Claude Agent SDK for execution |
| `--resume`, `-r ID` | Resume a previous orchestrator session |
| `--mcp-config PATH` | Path to MCP client configuration YAML file |
| `--mcp-tool-prefix PREFIX` | Prefix to add to all MCP tool names (e.g., 'mcp_') |
| `--dry-run`, `-n` | Validate seed without executing |
| `--debug`, `-d` | Show logs and agent thinking (verbose output) |

#### Examples

```bash
# Standard workflow execution (placeholder)
ouroboros run workflow seed.yaml

# Orchestrator mode (Claude Agent SDK)
ouroboros run workflow --orchestrator seed.yaml

# Orchestrator with external MCP tools
ouroboros run workflow --orchestrator --mcp-config mcp.yaml seed.yaml

# With MCP tool prefix to avoid conflicts
ouroboros run workflow -o --mcp-config mcp.yaml --mcp-tool-prefix "ext_" seed.yaml

# Dry run to validate seed
ouroboros run workflow --dry-run seed.yaml

# Resume a previous orchestrator session
ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml

# Debug output (show logs and agent thinking)
ouroboros run workflow --orchestrator --debug seed.yaml
```

#### Orchestrator Mode

When using `--orchestrator`, the workflow is executed via Claude Agent SDK:

1. Seed is loaded and validated
2. ClaudeAgentAdapter initialized
3. If `--mcp-config` provided, connects to external MCP servers
4. OrchestratorRunner executes the seed with merged tools
5. Progress is streamed to console
6. Events are persisted to the event store

Session ID is printed for later resumption.

#### MCP Client Integration

The `--mcp-config` option enables integration with external MCP servers, making Ouroboros
a "hub" that both serves tools (via `ouroboros mcp serve`) AND consumes external tools.

**Tool Precedence Rules:**
- Built-in tools (Read, Write, Edit, Bash, Glob, Grep) always take priority
- When MCP tools conflict with built-in tools, the MCP tool is skipped with a warning
- When multiple MCP servers provide the same tool, the first server in config wins

**Example MCP Config File (`mcp.yaml`):**

```yaml
mcp_servers:
  - name: "filesystem"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-filesystem", "/workspace"]

  - name: "github"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"  # Uses environment variable

connection:
  timeout_seconds: 30
  retry_attempts: 3
  health_check_interval: 60

# Optional: prefix all MCP tool names
tool_prefix: ""
```

**Security Notes:**
- Credentials must be passed via environment variables (not plaintext in config)
- Config files with world-readable permissions trigger a warning
- Server names are sanitized in logs to prevent credential leakage

See [MCP Client Configuration](#mcp-client-configuration) for full schema details.

### `ouroboros run resume`

Resume a paused or failed execution.

```bash
ouroboros run resume [EXECUTION_ID]
```

| Argument | Description |
|----------|-------------|
| `EXECUTION_ID` | Execution ID to resume (uses latest if not specified) |

#### Example

```bash
# Resume specific execution
ouroboros run resume exec_abc123

# Resume most recent execution
ouroboros run resume
```

---

## `ouroboros config` - Configuration Commands

The `config` command group manages Ouroboros configuration.

### `ouroboros config show`

Display current configuration.

```bash
ouroboros config show [SECTION]
```

| Argument | Description |
|----------|-------------|
| `SECTION` | Configuration section to display (e.g., 'providers') |

#### Examples

```bash
# Show all configuration
ouroboros config show

# Show specific section
ouroboros config show providers
```

Output:
```
Current Configuration
+-------------+---------------------------+
| Key         | Value                     |
+-------------+---------------------------+
| config_path | ~/.ouroboros/config.yaml  |
| database    | ~/.ouroboros/ouroboros.db |
| log_level   | INFO                      |
+-------------+---------------------------+
```

### `ouroboros config init`

Initialize Ouroboros configuration.

```bash
ouroboros config init
```

Creates default configuration files at `~/.ouroboros/` if they don't exist.

### `ouroboros config set`

Set a configuration value.

```bash
ouroboros config set KEY VALUE
```

| Argument | Description |
|----------|-------------|
| `KEY` | Configuration key (dot notation) |
| `VALUE` | Value to set |

#### Examples

```bash
# Set log level
ouroboros config set logging.level DEBUG

# Set default provider
ouroboros config set providers.default anthropic/claude-3-5-sonnet
```

> **Note:** Sensitive values (API keys) should be set via environment variables.

### `ouroboros config validate`

Validate current configuration.

```bash
ouroboros config validate
```

Checks configuration files for errors and missing required values.

---

## `ouroboros status` - Status Commands

The `status` command group checks system status and execution history.

### `ouroboros status executions`

List recent executions.

```bash
ouroboros status executions [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--limit`, `-n NUM` | Number of executions to show (default: 10) |
| `--all`, `-a` | Show all executions |

#### Example

```bash
ouroboros status executions --limit 5
```

Output:
```
Recent Executions
+-----------+----------+
| Name      | Status   |
+-----------+----------+
| exec-001  | complete |
| exec-002  | running  |
| exec-003  | failed   |
+-----------+----------+

Showing last 5 executions. Use --all to see more.
```

### `ouroboros status execution`

Show details for a specific execution.

```bash
ouroboros status execution EXECUTION_ID [OPTIONS]
```

| Argument | Description |
|----------|-------------|
| `EXECUTION_ID` | Execution ID to inspect |

| Option | Description |
|--------|-------------|
| `--events`, `-e` | Show execution events |

#### Example

```bash
# Show execution details
ouroboros status execution exec-001

# Include event history
ouroboros status execution exec-001 --events
```

### `ouroboros status health`

Check system health.

```bash
ouroboros status health
```

Verifies database connectivity, provider configuration, and system resources.

#### Example

```bash
ouroboros status health
```

Output:
```
System Health
+---------------+---------+
| Component     | Status  |
+---------------+---------+
| Database      | ok      |
| Configuration | ok      |
| Providers     | warning |
+---------------+---------+
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (see error message) |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `OPENAI_API_KEY` | OpenAI API key |
| `OUROBOROS_CONFIG` | Path to config file (default: `~/.ouroboros/config.yaml`) |
| `OUROBOROS_LOG_LEVEL` | Log level override |

---

## Configuration File

Default location: `~/.ouroboros/config.yaml`

```yaml
# LLM Provider Settings
providers:
  default: anthropic/claude-3-5-sonnet
  frugal: anthropic/claude-3-haiku
  standard: anthropic/claude-3-5-sonnet
  frontier: anthropic/claude-3-opus

# Database Settings
database:
  path: ~/.ouroboros/ouroboros.db

# Logging Settings
logging:
  level: INFO
  format: json  # or "text"

# Interview Settings
interview:
  max_rounds: 10
  ambiguity_threshold: 0.2

# Orchestrator Settings
orchestrator:
  permission_mode: acceptEdits
  default_tools:
    - Read
    - Write
    - Edit
    - Bash
    - Glob
    - Grep
```

---

## Examples

### Complete Workflow Example

```bash
# 1. Initialize configuration
ouroboros config init

# 2. Validate configuration
ouroboros config validate

# 3. Check system health
ouroboros status health

# 4. Start an interview
ouroboros init "Build a Python library for parsing markdown"

# 5. (Answer questions interactively)

# 6. Execute the generated seed
ouroboros run workflow --orchestrator ~/.ouroboros/seeds/latest.yaml

# 7. Monitor progress
ouroboros status executions

# 8. Check specific execution
ouroboros status execution exec_abc123 --events
```

### Resuming Interrupted Work

```bash
# Resume interrupted interview
ouroboros init list
ouroboros init --resume interview_20260125_120000

# Resume interrupted orchestrator session
ouroboros status executions
ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml
```

### CI/CD Usage

```bash
# Non-interactive execution with dry-run validation
ouroboros run workflow --dry-run seed.yaml

# Execute with debug output (shows logs and agent thinking)
ouroboros run workflow --orchestrator --debug seed.yaml

# Execute with full debug logging via environment variable
OUROBOROS_LOG_LEVEL=DEBUG ouroboros run workflow --orchestrator seed.yaml
```

---

## `ouroboros tui` - Interactive TUI Monitor

The `tui` command group provides an interactive terminal user interface for monitoring workflow execution in real-time.

### `ouroboros tui monitor`

Launch the interactive TUI monitor.

```bash
ouroboros tui monitor [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--execution-id`, `-e ID` | Monitor a specific execution |
| `--session-id`, `-s ID` | Monitor a specific session |

#### Examples

```bash
# Launch TUI monitor
ouroboros tui monitor

# Monitor specific execution
ouroboros tui monitor --execution-id exec_abc123

# Monitor specific session
ouroboros tui monitor --session-id sess_xyz789
```

#### TUI Screens

The TUI provides 5 screens, accessible via number keys:

| Key | Screen | Description |
|-----|--------|-------------|
| `1` | Dashboard | Overview with phase progress, drift meter, cost tracker |
| `2` | Logs | Filterable log viewer with level filtering |
| `3` | Execution | Execution details, timeline, phase outputs |
| `4` | Debug | State inspector, raw events, configuration |
| `5` | Help | Keyboard shortcuts and help |

#### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1-5` | Switch screens |
| `q` | Quit |
| `r` | Refresh |
| `↑/↓` | Scroll |
| `Tab` | Next widget |

#### Dashboard Widgets

- **Phase Progress**: Double Diamond visualization of 6 phases
- **Drift Meter**: Shows drift score with weighted formula
- **Cost Tracker**: Token usage and cost in USD
- **AC Tree**: Acceptance criteria hierarchy

---

## `ouroboros mcp` - MCP Server Commands

The `mcp` command group manages the Model Context Protocol server, allowing Claude Desktop and other MCP clients to interact with Ouroboros.

### `ouroboros mcp serve`

Start the MCP server.

```bash
ouroboros mcp serve [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--host`, `-h HOST` | Host to bind to (default: localhost) |
| `--port`, `-p PORT` | Port to bind to (default: 8080) |
| `--transport`, `-t TYPE` | Transport type: `stdio` or `sse` (default: stdio) |

#### Examples

```bash
# Start with stdio transport (for Claude Desktop)
ouroboros mcp serve

# Start with SSE transport on custom port
ouroboros mcp serve --transport sse --port 9000

# Start on specific host
ouroboros mcp serve --host 0.0.0.0 --port 8080 --transport sse
```

#### Claude Desktop Integration

Add to your Claude Desktop config (`~/.config/claude/config.json`):

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

### `ouroboros mcp info`

Show MCP server information and available tools.

```bash
ouroboros mcp info
```

#### Example

```bash
ouroboros mcp info
```

Output:
```
MCP Server Information
  Name: ouroboros-mcp
  Version: 1.0.0

Capabilities
  Tools: True
  Resources: False
  Prompts: False

Available Tools
  ouroboros_execute_seed
    Execute a seed specification
    Parameters:
      - seed_yaml*: YAML content of the seed specification
      - dry_run: Whether to validate without executing

  ouroboros_session_status
    Get the status of a session
    Parameters:
      - session_id*: Session ID to query

  ouroboros_query_events
    Query event history
    Parameters:
      - aggregate_id: Filter by aggregate ID
      - event_type: Filter by event type
      - limit: Maximum events to return
```

---

## MCP Client Configuration

When using `--mcp-config` with the orchestrator, you can connect to external MCP servers
to provide additional tools to the Claude Agent during workflow execution.

### Configuration File Schema

```yaml
# MCP Server Configurations
mcp_servers:
  # Stdio transport (for local processes)
  - name: "filesystem"           # Unique server name (required)
    transport: "stdio"           # Transport type: stdio, sse, streamable-http
    command: "npx"               # Command to execute (required for stdio)
    args:                        # Command arguments
      - "-y"
      - "@anthropic/mcp-server-filesystem"
      - "/workspace"
    env:                         # Environment variables (optional)
      DEBUG: "true"
    timeout: 30.0                # Connection timeout in seconds

  # With environment variable substitution
  - name: "github"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-github"]
    env:
      # Use ${VAR_NAME} syntax for environment variables
      # NEVER put credentials directly in the config file
      GITHUB_TOKEN: "${GITHUB_TOKEN}"

  # SSE transport (for HTTP servers)
  - name: "remote-tools"
    transport: "sse"
    url: "https://tools.example.com/mcp"  # Required for sse transport
    headers:
      Authorization: "Bearer ${API_TOKEN}"

# Connection Settings (optional)
connection:
  timeout_seconds: 30        # Default timeout for operations
  retry_attempts: 3          # Number of retry attempts on failure
  health_check_interval: 60  # Seconds between health checks

# Tool Naming (optional)
tool_prefix: ""              # Prefix to add to all MCP tool names
```

### Transport Types

| Transport | Description | Required Fields |
|-----------|-------------|-----------------|
| `stdio` | Runs a local process, communicates via stdin/stdout | `command` |
| `sse` | Connects to an HTTP server using Server-Sent Events | `url` |
| `streamable-http` | HTTP with streaming support | `url` |

### Environment Variable Substitution

For security, credentials should be passed via environment variables:

```yaml
env:
  GITHUB_TOKEN: "${GITHUB_TOKEN}"    # Reads GITHUB_TOKEN from environment
  API_KEY: "${MY_API_KEY}"           # Reads MY_API_KEY from environment
```

The config loader will:
1. Check if the environment variable is set
2. Replace `${VAR_NAME}` with the actual value
3. Error if the variable is not set

### Tool Precedence Rules

When multiple tools have the same name:

1. **Built-in tools always win**: Read, Write, Edit, Bash, Glob, Grep
   - MCP tools with these names are skipped with a warning

2. **First server wins**: If multiple MCP servers provide the same tool name,
   the server listed first in the config file takes precedence
   - Later servers' tools are skipped with a warning

3. **Use tool_prefix to avoid conflicts**: Setting `tool_prefix: "mcp_"` converts
   tool names like `read` to `mcp_read`, avoiding conflicts with built-in `Read`

### Security Considerations

1. **Credentials**: Never put credentials in the config file
   - Use `${VAR_NAME}` syntax for secrets
   - Set environment variables before running

2. **File Permissions**: The loader warns if config files are world-readable
   - Recommended: `chmod 600 mcp.yaml`

3. **Server Names**: Server names are sanitized in logs to prevent credential leakage

### Troubleshooting

#### MCP Server Connection Issues

**Server fails to connect:**
```
Failed to connect to 'filesystem': Connection refused
```
- Verify the command exists: `which npx`
- Check if the server package is installed
- Try running the command manually to see error output

**Environment variable not set:**
```
Environment variable not set: GITHUB_TOKEN
```
- Export the variable: `export GITHUB_TOKEN=ghp_...`
- Or set it inline: `GITHUB_TOKEN=ghp_... ouroboros run workflow ...`

**Tool name conflicts:**
```
MCP tool 'Read' shadowed by built-in tool
```
- Use `--mcp-tool-prefix mcp_` to namespace MCP tools
- Or rename the tool in the MCP server configuration

**Timeout during tool execution:**
```
Tool call timed out after 3 retries: file_read
```
- Increase `connection.timeout_seconds` in config
- Check network connectivity to remote servers
- Verify the MCP server is healthy

#### Debugging

Enable verbose logging to see MCP communication:

```bash
OUROBOROS_LOG_LEVEL=DEBUG ouroboros run workflow -o --mcp-config mcp.yaml seed.yaml
```

This will show:
- MCP server connection attempts
- Tool discovery from each server
- Tool name conflict resolution
- Tool call attempts and responses
