<!--
doc_metadata:
  runtime_scope: [claude]
-->

# Running Ouroboros with Claude Code

Ouroboros can use **Claude Code** as a runtime backend, leveraging your **Claude Code Max Plan** subscription to execute workflows without requiring a separate API key.

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

> **Command context guide:** This page contains commands for two different contexts:
> - **Terminal** -- commands you run in your regular shell (bash, zsh, etc.)
> - **Inside Claude Code session** -- `ooo` skill commands that only work inside an active Claude Code session (start one with `claude`)
>
> Each code block is labeled to indicate where to run it.

## Prerequisites

- Claude Code CLI installed and authenticated (Max Plan)
- Python >= 3.12
- Ouroboros installed (see [Getting Started](../getting-started.md) for install options)

> The `[claude]` extra (`pip install ouroboros-ai[claude]`) installs `claude-agent-sdk` and `anthropic` -- required for Claude Code runtime integration. The base `ouroboros-ai` package does not include these.

## Configuration

To select Claude Code as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: claude
```

When using the `--orchestrator` CLI flag, Claude Code is the default runtime backend.

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

## Cost

Using Claude Code as the runtime backend with a Max Plan means:
- **No additional API costs** -- uses your subscription
- Execution time varies by task complexity
- Typical simple tasks: 15-30 seconds
- Complex multi-file tasks: 1-3 minutes
