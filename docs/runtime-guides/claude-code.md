# Running Ouroboros with Claude Code

Ouroboros can use **Claude Code** as a runtime backend, leveraging your **Claude Code Max Plan** subscription to execute workflows without requiring a separate API key.

> **Command context guide:** This page contains commands for two different contexts:
> - **Terminal** -- commands you run in your regular shell (bash, zsh, etc.)
> - **Inside Claude Code session** -- `ooo` skill commands that only work inside an active Claude Code session (start one with `claude`)
>
> Each code block is labeled to indicate where to run it.

## Prerequisites

- Claude Code CLI installed and authenticated (Max Plan)
- Python >= 3.12

## Installation

**Terminal:**
```bash
pip install ouroboros-ai[claude]
# or
uv pip install "ouroboros-ai[claude]"
```

The `[claude]` extra installs `claude-agent-sdk` and `anthropic` -- required for Claude Code runtime integration. The base `ouroboros-ai` package does not include these.

### From Source (Development)

**Terminal:**
```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync
```

## Configuration

To select Claude Code as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: claude
```

When using the `--orchestrator` CLI flag, Claude Code is the default runtime backend.

## Quick Start

### Check System Health

**Terminal:**
```bash
uv run ouroboros status health
```

Expected output:
```
+---------------+---------+
| Database      |   ok    |
| Configuration |   ok    |
| Providers     | warning |  # OK - we'll use Claude Code instead
+---------------+---------+
```

## Two Ways to Use

### Option A: Create Seed via Interview (Recommended)

Don't know how to write a Seed file? Use the interactive interview:

**Terminal:**
```bash
uv run ouroboros init start --orchestrator "Build a REST API for task management"
```

This will:
1. Ask clarifying questions (Socratic method)
2. Reduce ambiguity through dialogue
3. Generate a Seed file automatically

### Option B: Write Seed Manually

Create a YAML file describing your task. Example `my-task.yaml`:

```yaml
goal: "Implement a user authentication module"
constraints:
  - "Python >= 3.12"
  - "Use bcrypt for password hashing"
  - "Follow existing project patterns"
acceptance_criteria:
  - "Create auth/models.py with User model"
  - "Create auth/service.py with login/register functions"
  - "Add unit tests with pytest"
ontology_schema:
  name: "AuthModule"
  description: "User authentication system"
  fields:
    - name: "users"
      field_type: "object"
      description: "User data structure"
      required: true
evaluation_principles:
  - name: "security"
    description: "Code follows security best practices"
    weight: 1.0
  - name: "testability"
    description: "Code is well-tested"
    weight: 0.8
exit_conditions:
  - name: "all_tests_pass"
    description: "All acceptance criteria met and tests pass"
    evaluation_criteria: "pytest returns 0"
metadata:
  ambiguity_score: 0.15
```

### Run with Orchestrator Mode

**Terminal:**
```bash
uv run ouroboros run workflow --orchestrator my-task.yaml
```

This will:
1. Parse your seed file
2. Connect to Claude Code using your Max Plan authentication
3. Execute the task autonomously
4. Report progress and results

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |  Claude Code    |
|  (your task)    |     |   (adapter.py)   |     |  (Max Plan)     |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Tools Available |
                        |  - Read          |
                        |  - Write         |
                        |  - Edit          |
                        |  - Bash          |
                        |  - Glob          |
                        |  - Grep          |
                        +------------------+
```

The orchestrator uses `claude-agent-sdk` which connects directly to your authenticated Claude Code session. No API key required.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Claude Code-Specific Strengths

- **Zero API key management** -- uses your Max Plan subscription directly
- **Rich tool access** -- full suite of file, shell, and search tools via Claude Code
- **Session continuity** -- resume interrupted workflows with `--resume`

## CLI Options

All commands in this section run in your **regular terminal** (shell), not inside a Claude Code session.

### Interview Commands

**Terminal:**
```bash
# Start interactive interview (Claude Code runtime)
uv run ouroboros init start --orchestrator "Your idea here"

# Start interactive interview (LiteLLM - needs API key)
uv run ouroboros init start "Your idea here"

# Resume an interrupted interview
uv run ouroboros init start --resume interview_20260127_120000

# List all interviews
uv run ouroboros init list
```

### Workflow Commands

**Terminal:**
```bash
# Execute workflow (Claude Code runtime)
uv run ouroboros run workflow --orchestrator seed.yaml

# Dry run (validate seed without executing)
uv run ouroboros run workflow --dry-run seed.yaml

# Debug output (show logs and agent thinking)
uv run ouroboros run workflow --orchestrator --debug seed.yaml

# Resume a previous session
uv run ouroboros run workflow --orchestrator --resume <session_id> seed.yaml
```

## Seed File Reference

| Field | Required | Description |
|-------|----------|-------------|
| `goal` | Yes | Primary objective |
| `constraints` | No | Hard constraints to satisfy |
| `acceptance_criteria` | No | Specific success criteria |
| `ontology_schema` | Yes | Output structure definition |
| `evaluation_principles` | No | Principles for evaluation |
| `exit_conditions` | No | Termination conditions |
| `metadata.ambiguity_score` | Yes | Must be <= 0.2 |

## Troubleshooting

### "Providers: warning" in health check

This is normal when not using LiteLLM providers. The orchestrator mode uses Claude Code directly.

### Session fails with empty error

Ensure you're running from the project directory:

**Terminal:**
```bash
cd /path/to/ouroboros
uv run ouroboros run workflow --orchestrator seed.yaml
```

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Example Output

```
+------------- Success -------------+
| Execution completed successfully! |
+-----------------------------------+
+------------ Info -------------+
| Session ID: orch_4734421f92cf |
+-------------------------------+
+--------- Info ---------+
| Messages processed: 20 |
+------------------------+
+----- Info ------+
| Duration: 25.2s |
+-----------------+
```

## Cost

Using Claude Code as the runtime backend with a Max Plan means:
- **No additional API costs** -- uses your subscription
- Execution time varies by task complexity
- Typical simple tasks: 15-30 seconds
- Complex multi-file tasks: 1-3 minutes
