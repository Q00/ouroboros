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
| `--dry-run`, `-n` | Validate seed without executing |
| `--verbose`, `-v` | Enable verbose output |

#### Examples

```bash
# Standard workflow execution (placeholder)
ouroboros run workflow seed.yaml

# Orchestrator mode (Claude Agent SDK)
ouroboros run workflow --orchestrator seed.yaml

# Dry run to validate seed
ouroboros run workflow --dry-run seed.yaml

# Resume a previous orchestrator session
ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml

# Verbose output
ouroboros run workflow --orchestrator --verbose seed.yaml
```

#### Orchestrator Mode

When using `--orchestrator`, the workflow is executed via Claude Agent SDK:

1. Seed is loaded and validated
2. ClaudeAgentAdapter initialized
3. OrchestratorRunner executes the seed
4. Progress is streamed to console
5. Events are persisted to the event store

Session ID is printed for later resumption.

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

# Execute with verbose logging
OUROBOROS_LOG_LEVEL=DEBUG ouroboros run workflow --orchestrator seed.yaml
```
