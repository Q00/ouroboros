<!--
doc_maintenance:
  score: 43
  rank: 4
  scored_on: "2026-03-15"
  scoring_formula: "findings_count*2 + code_deps_count + downstream_dependents + open_findings*5"
  findings_total: 15
  findings_open: 0
  open_finding_ids: []
  # code_deps_count: DERIVED — computed from docs/claim-registry.yaml (schema v1.3, Sub-AC 2a)
  # Derivation: count unique code_deps entries across all CR-NNN claims whose locations[] include this doc.
  # Legacy value was 10; authoritative claim-level code_deps now live in claim-registry.yaml.
  downstream_dependents: 3
  review_trigger: "Any change to src/ouroboros/cli/commands/*.py or src/ouroboros/cli/main.py"
  registry_ref: "docs/contributing/findings-registry.md"
  ranking_ref: "docs/doc-maintenance-ranking.yaml"
  runtime_scope: [local, claude, codex, ci]

canonical_source:
  # [v1.2] Upgraded from claim_ownership schema v1.0 (2026-03-15).
  # defers_to now uses canonical_doc + claim_patterns[] with claim_pattern_ids from claim_patterns: registry.
  # Note: v1.0 "cli-commands" → v1.2 "cli-options"; "tui-shortcuts" merged into "cli-options".
  schema_version: "1.2"
  generated: "2026-03-15"
  # This guide adds narrative context to CLI usage.
  # ALL CLI option definitions, defaults, and package install commands DEFER to their canonical owners.
  canonical_for: []   # narrative guide; no shared claim pattern is authoritative here
  defers_to:
    - canonical_doc: docs/cli-reference.md
      claim_patterns: [cli-options, pkg-install]
      # cli-options: narrative option tables derived from cli-reference.md; must be kept in sync.
      #   claim_registry_refs: [CR-005–CR-015, CR-010 (TUI shortcuts), CR-014]
      # pkg-install: pip install commands repeated here for discoverability;
      #   claim_inventory_refs: [C-001, C-002]
    - canonical_doc: docs/config-reference.md
      claim_patterns: [install-paths, config-keys]
      # install-paths: path references (e.g., ~/.ouroboros/, ~/.claude/mcp.json)
      #   claim_inventory_refs: [A-001, A-002, A-007]; update config-reference.md first if paths change.
-->

# CLI Usage Guide

Ouroboros provides a command-line interface built with Typer and Rich for interactive workflow management.

> **Maintenance Warning — Score 43/100 (Rank #4 of 42, scored 2026-03-15)**
> This guide has the highest per-document finding count in the corpus: **15
> audit findings** (all resolved). It tracks **10 source files** and mirrors
> [`docs/cli-reference.md`](../cli-reference.md) — **both files must be updated
> together** whenever CLI options change. Any change to
> `src/ouroboros/cli/commands/*.py` or `src/ouroboros/cli/main.py` **must**
> trigger a review of this file. See
> [`docs/doc-maintenance-ranking.yaml`](../doc-maintenance-ranking.yaml) for
> the full scoring breakdown.

## Installation

The CLI is installed automatically with the Ouroboros package:

```bash
# Using uv (recommended)
uv sync
uv run ouroboros --help

# Using pip
pip install ouroboros-ai
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
| `ouroboros setup` | Detect runtimes and configure Ouroboros for your environment (one-time). Supports `claude` and `codex`; `opencode` is detected but cannot be configured via `setup` — see [CLI Reference: setup](../cli-reference.md#ouroboros-setup) |
| `ouroboros init` | Start interactive interview (Big Bang phase) |
| `ouroboros run` | Execute workflows |
| `ouroboros cancel` | Cancel stuck or orphaned executions |
| `ouroboros config` | Manage configuration (scaffolding — placeholder output only) |
| `ouroboros status` | Check system status (placeholder output only) |
| `ouroboros tui` | Interactive TUI monitor |
| `ouroboros monitor` | Shorthand for `tui monitor` |
| `ouroboros mcp` | MCP server commands |

### Shortcuts (v0.8.0+)

Common operations have shorter forms:

```bash
# These pairs are equivalent:
ouroboros run seed.yaml          # = ouroboros run workflow seed.yaml
ouroboros init "Build an API"    # = ouroboros init start "Build an API"
ouroboros monitor                # = ouroboros tui monitor
```

Orchestrator mode (runtime backend execution) is now the default for `run workflow`.

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
| `-o, --orchestrator` | Use Claude Code (Max Plan) for the interview/seed flow — no API key required |
| `--runtime TEXT` | Agent runtime backend for the workflow execution step after seed generation. Shipped values: `claude`, `codex`. (`opencode` is in the CLI enum but out of scope.) Custom adapters registered in `runtime_factory.py` are also accepted. |
| `--llm-backend TEXT` | LLM backend for interview, ambiguity scoring, and seed generation (`claude_code`, `litellm`, `codex`). (`opencode` is in the CLI enum but out of scope) |
| `-d, --debug` | Show verbose logs including debug messages |

#### Examples

```bash
# Start new interview with initial context
ouroboros init "I want to build a task management CLI tool"

# Start new interview interactively
ouroboros init

# Start with Claude Code (no API key needed)
ouroboros init --orchestrator "Build a REST API"

# Specify runtime backend for the workflow execution step
ouroboros init --orchestrator --runtime codex "Build a REST API"

# Use Codex as the LLM backend for interview and seed generation
ouroboros init --llm-backend codex "Build a REST API"

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

#### Error Handling

| Situation | Behavior |
|-----------|----------|
| API key missing or invalid | Command exits with error code 1. Set `ANTHROPIC_API_KEY` or use `--orchestrator`. |
| LLM rate limit during a question | Error is shown with a `Retry? [Y/n]` prompt. Session state is preserved. |
| State save fails mid-interview | Warning printed; interview continues. Progress not persisted. Fix directory permissions. |
| Empty response given | Rejected immediately; the same question is re-displayed. |
| Ambiguity score > 0.2 at generation time | Presents three choices: continue the interview, force-generate, or cancel. |
| Seed generation LLM failure | "Failed to generate Seed" error. Resume the session to retry generation. |
| Seed file write fails | "Failed to save Seed" error. Fix disk space or permissions, then resume. |
| Ctrl+C at any time | Progress saved; exits with code 0. Resume with `--resume`. |

For a detailed walkthrough of each failure mode, see [Seed Authoring — Failure Modes & Troubleshooting](./seed-authoring.md#failure-modes--troubleshooting).

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

### `ouroboros run [workflow]`

Execute a workflow from a seed file. The `workflow` subcommand is optional --
`ouroboros run seed.yaml` is equivalent to `ouroboros run workflow seed.yaml`.

```bash
ouroboros run [workflow] SEED_FILE [OPTIONS]
```

| Argument | Description |
|----------|-------------|
| `SEED_FILE` | Path to the seed YAML file |

| Option | Description |
|--------|-------------|
| `-o/-O, --orchestrator/--no-orchestrator` | Use runtime backend execution (default: enabled) |
| `--runtime TEXT` | Agent runtime backend override (`claude`, `codex`). Uses configured default if omitted. (`opencode` is in the CLI enum but out of scope) |
| `--resume`, `-r ID` | Resume a previous orchestrator session |
| `--mcp-config PATH` | Path to MCP client configuration YAML file |
| `--mcp-tool-prefix PREFIX` | Prefix to add to all MCP tool names (e.g., 'mcp_') |
| `--sequential`, `-s` | Execute ACs sequentially instead of in parallel |
| `--no-qa` | Skip post-execution QA evaluation |
| `--dry-run`, `-n` | Validate seed without executing. **Currently only takes effect with `--no-orchestrator`.** In default orchestrator mode this flag is accepted but has no effect — the full workflow executes |
| `--debug`, `-d` | Show logs and agent thinking (verbose output) |

#### Examples

```bash
# Run a workflow (shorthand, recommended)
ouroboros run seed.yaml

# Explicit subcommand (equivalent)
ouroboros run workflow seed.yaml

# With external MCP tools
ouroboros run seed.yaml --mcp-config mcp.yaml

# With MCP tool prefix to avoid conflicts
ouroboros run seed.yaml --mcp-config mcp.yaml --mcp-tool-prefix "ext_"

# Dry run to validate seed
ouroboros run seed.yaml --dry-run

# Resume a previous orchestrator session
ouroboros run seed.yaml --resume orch_abc123

# Debug output (show logs and agent thinking)
ouroboros run seed.yaml --debug
```

#### Orchestrator Mode

Orchestrator mode is now the default. The workflow is executed via the configured runtime backend:

1. Seed is loaded and validated
2. The configured runtime adapter is initialized
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

> **Current state:** this helper is still placeholder-oriented. Prefer `ouroboros run seed.yaml --resume <session_id>` for real orchestrator sessions.

```bash
ouroboros run resume [EXECUTION_ID]
```

| Argument | Description |
|----------|-------------|
| `EXECUTION_ID` | Execution ID to resume (uses latest if not specified) |

#### Example

```bash
# Preferred pattern for real orchestrator sessions
ouroboros run seed.yaml --resume orch_abc123
```

---

## `ouroboros config` - Configuration Commands

The `config` command group manages Ouroboros configuration.

> **Current state:** these commands are scaffolding. They print placeholder output and do not yet update `~/.ouroboros/config.yaml`.

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

Creates `~/.ouroboros/config.yaml` and `~/.ouroboros/credentials.yaml` with default templates. Sets `chmod 600` on `credentials.yaml`. If the files already exist they are not overwritten.

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
# Placeholder command surface
ouroboros config set orchestrator.runtime_backend codex
```

> **Note:** Sensitive values (API keys) should be set via environment variables.

### `ouroboros config validate`

Validate current configuration.

```bash
ouroboros config validate
```

Checks configuration files for errors and missing required values.
Currently this command is informational only.

---

## `ouroboros status` - Status Commands

The `status` command group checks system status and execution history.

> **Current state:** these commands return lightweight placeholder summaries. Use them as smoke checks only, not as authoritative workflow state.

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

The table below lists the most commonly used variables. For the full list (including all per-model overrides such as `OUROBOROS_QA_MODEL`, `OUROBOROS_SEMANTIC_MODEL`, etc.), see the [Configuration Reference](../config-reference.md#environment-variables).

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `OPENAI_API_KEY` | OpenAI API key for Codex / LiteLLM-backed flows |
| `OPENROUTER_API_KEY` | OpenRouter API key for consensus and LiteLLM-backed flows |
| `OUROBOROS_AGENT_RUNTIME` | Override `orchestrator.runtime_backend` (`claude`, `codex`) |
| `OUROBOROS_AGENT_PERMISSION_MODE` | Override `orchestrator.permission_mode` |
| `OUROBOROS_LLM_BACKEND` | Override `llm.backend` |
| `OUROBOROS_CLI_PATH` | Override `orchestrator.cli_path` (path to Claude CLI binary) |
| `OUROBOROS_CODEX_CLI_PATH` | Override `orchestrator.codex_cli_path` |

---

## Configuration File

Default location: `~/.ouroboros/config.yaml`

For all available options, see the [Configuration Reference](../config-reference.md). A minimal example:

```yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex  # optional if already on PATH

llm:
  backend: codex

logging:
  level: info
```

---

## Examples

### Complete Workflow Example

```bash
# 1. Configure a runtime
ouroboros setup --runtime codex

# 2. Start an interview
ouroboros init "Build a Python library for parsing markdown"

# 3. (Answer questions interactively)

# 4. Execute the generated seed (replace with the path printed by the interview)
ouroboros run seed.yaml

# 5. Monitor progress
ouroboros monitor

# 6. Check specific execution
ouroboros status execution exec_abc123 --events
```

### Resuming Interrupted Work

```bash
# Resume interrupted interview
ouroboros init list
ouroboros init start --resume interview_20260125_120000

# Resume interrupted orchestrator session
ouroboros status executions
ouroboros run seed.yaml --resume orch_abc123
```

### CI/CD Usage

```bash
# Non-interactive dry-run validation
ouroboros run seed.yaml --dry-run

# Execute with debug output (shows logs and agent thinking)
ouroboros run seed.yaml --debug
```

> **Note:** `OUROBOROS_LOG_LEVEL` is **not** a recognized environment variable. To control log verbosity, set `logging.level: debug` in `~/.ouroboros/config.yaml` or use `--debug` on the CLI.

---

## `ouroboros tui` - Interactive TUI Monitor

The `tui` command group provides an interactive terminal user interface for monitoring workflow execution in real-time.

> **Equivalent invocations:** `ouroboros tui` (no subcommand), `ouroboros tui monitor`, and `ouroboros monitor` are all equivalent — they all launch the TUI monitor.

### `ouroboros tui monitor`

Launch the interactive TUI monitor.

```bash
ouroboros tui [monitor] [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--db-path PATH` | Path to the Ouroboros database file (default: `~/.ouroboros/ouroboros.db`) |
| `--backend TEXT` | TUI backend: `textual` (default) or `slt` (native Rust) |

#### Examples

```bash
# Launch TUI monitor (default Textual backend)
ouroboros tui monitor

# Monitor with a specific database file
ouroboros tui monitor --db-path ~/.ouroboros/ouroboros.db

# Use the native SLT backend
ouroboros tui monitor --backend slt
```

> **Note:** The `slt` backend requires the `ouroboros-tui` binary. Install with:
> `cd crates/ouroboros-tui && cargo install --path .`

#### TUI Screens

The TUI provides 6 screens / views:

| Key | Screen | Description |
|-----|--------|-------------|
| `1` | Dashboard | Overview with phase progress, drift meter, cost tracker |
| `2` | Execution | Execution details, timeline, phase outputs |
| `3` | Logs | Filterable log viewer with level filtering |
| `4` | Debug | State inspector, raw events, configuration |
| `s` | Session Selector | Browse and switch between monitored sessions |
| `e` | Lineage | View evolutionary lineage across generations (evolve/ralph) |

#### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1-4` | Switch to numbered screen |
| `s` | Session Selector |
| `e` | Lineage view |
| `q` | Quit |
| `p` | Pause execution |
| `r` | Resume execution |
| `↑/↓` | Scroll |
| `Tab` | Next widget |

#### Dashboard Widgets

- **Phase Progress**: Double Diamond visualization of 4 sub-phases (Discover, Define, Design, Deliver)
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
| `--db TEXT` | Path to the EventStore database file (default: `~/.ouroboros/ouroboros.db`) |
| `--runtime TEXT` | Runtime backend for orchestrator-driven tools (`claude`, `codex`). (`opencode` is in the CLI enum but out of scope) |
| `--llm-backend TEXT` | LLM backend for interview/seed/evaluation tools (`claude_code`, `litellm`, `codex`). (`opencode` is in the CLI enum but out of scope) |

#### Examples

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

#### Startup behavior

On startup, `mcp serve` automatically cancels any sessions left in `RUNNING` or `PAUSED` state for more than 1 hour. These are treated as orphaned from a previous crash. Cancelled sessions are reported on stderr (or console when using SSE transport).

#### Claude Desktop / Claude Code CLI Integration

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

If Ouroboros is installed directly (not via `uvx`), replace the `command`/`args` block with:

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

### `ouroboros mcp info`

Show MCP server information and available tools.

```bash
ouroboros mcp info [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--runtime TEXT` | Agent runtime backend for orchestrator-driven tools (`claude`, `codex`). Affects which tool variants are instantiated |
| `--llm-backend TEXT` | LLM backend for interview/seed/evaluation tools (`claude_code`, `litellm`, `codex`). Affects which tool variants are instantiated |

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

Enable verbose logging to see MCP communication. Use the `--debug` flag (there is no `OUROBOROS_LOG_LEVEL` environment variable):

```bash
ouroboros run seed.yaml --mcp-config mcp.yaml --debug
```

This will show:
- MCP server connection attempts
- Tool discovery from each server
- Tool name conflict resolution
- Tool call attempts and responses
