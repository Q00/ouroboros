# Running Ouroboros with Claude Code

Ouroboros can leverage your **Claude Code Max Plan** subscription to execute workflows without requiring a separate API key.

## Prerequisites

- Claude Code CLI installed and authenticated (Max Plan)
- Python 3.14+

## Installation

```bash
pip install ouroboros-ai
# or
uv pip install ouroboros-ai
```

### From Source (Development)

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync
```

## Quick Start

### 2. Check System Health

```bash
uv run ouroboros status health
```

Expected output:
```
┌───────────────┬─────────┐
│ Database      │   ok    │
│ Configuration │   ok    │
│ Providers     │ warning │  # OK - we'll use Claude Code instead
└───────────────┴─────────┘
```

## Two Ways to Use

### Option A: Create Seed via Interview (Recommended)

Don't know how to write a Seed file? Use the interactive interview:

```bash
uv run ouroboros init start --orchestrator "Build a REST API for task management"
```

This will:
1. Ask clarifying questions (Socratic method)
2. Reduce ambiguity through dialogue
3. Generate a Seed file automatically

### Option B: Write Seed Manually

### 3. Create a Seed File

Create a YAML file describing your task. Example `my-task.yaml`:

```yaml
goal: "Implement a user authentication module"
constraints:
  - "Python 3.14+"
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

### 4. Run with Orchestrator Mode

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
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Seed YAML     │ ──▶ │   Orchestrator   │ ──▶ │  Claude Code    │
│  (your task)    │     │   (adapter.py)   │     │  (Max Plan)     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                │
                                ▼
                        ┌──────────────────┐
                        │  Tools Available │
                        │  - Read          │
                        │  - Write         │
                        │  - Edit          │
                        │  - Bash          │
                        │  - Glob          │
                        │  - Grep          │
                        └──────────────────┘
```

The orchestrator uses `claude-agent-sdk` which connects directly to your authenticated Claude Code session. No API key required!

## CLI Options

### Interview Commands

```bash
# Start interactive interview (Claude Code)
uv run ouroboros init start --orchestrator "Your idea here"

# Start interactive interview (LiteLLM - needs API key)
uv run ouroboros init start "Your idea here"

# Resume an interrupted interview
uv run ouroboros init start --resume interview_20260127_120000

# List all interviews
uv run ouroboros init list
```

### Workflow Commands

```bash
# Execute workflow (Claude Code)
uv run ouroboros run workflow --orchestrator seed.yaml

# Dry run (validate seed without executing)
uv run ouroboros run workflow --dry-run seed.yaml

# Verbose output
uv run ouroboros run workflow --orchestrator --verbose seed.yaml

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
```bash
cd /path/to/ouroboros
uv run ouroboros run workflow --orchestrator seed.yaml
```

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Example Output

```
╭───────────── Success ─────────────╮
│ Execution completed successfully! │
╰───────────────────────────────────╯
╭──────────── Info ─────────────╮
│ Session ID: orch_4734421f92cf │
╰───────────────────────────────╯
╭───────── Info ─────────╮
│ Messages processed: 20 │
╰────────────────────────╯
╭───── Info ──────╮
│ Duration: 25.2s │
╰─────────────────╯
```

## Cost

Using orchestrator mode with Claude Code Max Plan means:
- **No additional API costs** - uses your subscription
- Execution time varies by task complexity
- Typical simple tasks: 15-30 seconds
- Complex multi-file tasks: 1-3 minutes
