<!--
doc_metadata:
  runtime_scope: [claude]
-->

# Running Ouroboros with Claude Code

> 한국어: [claude-code.ko.md](./claude-code.ko.md)

Ouroboros can use **Claude Code** as a runtime backend, leveraging your **Claude Code Pro or Max Plan** subscription to execute workflows without requiring a separate API key.

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

> **Command context guide:** This page contains commands for two different contexts:
> - **Terminal** -- commands you run in your regular shell (bash, zsh, etc.)
> - **Inside Claude Code session** -- `ooo` skill commands that only work inside an active Claude Code session (start one with `claude`)
>
> Each code block is labeled to indicate where to run it.

## Prerequisites

- Claude Code CLI installed and authenticated (Pro or Max Plan)
- **`uvx`** (ships with uv) if you use the marketplace plugin. The plugin's MCP
  manifest launches the server with `uvx`
  ([`.claude-plugin/.mcp.json`](../../.claude-plugin/.mcp.json)), so a host with
  only Claude Code cannot start it. Install uv with `pipx install uv`,
  `pip install --user uv`, or `brew install uv`.
- **`python3` on `PATH`** for the marketplace plugin as well. The bundled skills
  shell out to it directly (`skills/setup/SKILL.md:98`,
  `skills/welcome/SKILL.md:68`). `uvx --python '>=3.12'` supplies an interpreter
  to the isolated MCP process only and does not create a global `python3`, so a
  host with uv but no system Python fails during the first setup/welcome flow.
  Tracked in #2001.
- Python >= 3.12 specifically, **for the standalone CLI**.
- Ouroboros installed, for the standalone CLI (see [Getting Started](../getting-started.md) for install options)

> Install `ouroboros-ai[claude]` for the default in-process SDK runtime on MCP
> 1.x. The marketplace plugin launches the MCP 2 server from an isolated
> `ouroboros-ai[mcp]` environment and selects the `[claude-cli]` worker. Never
> combine `[mcp]` with `[claude]`, `[claude-sdk]`, or `[all]` in one interpreter.

## Configuration

To select Claude Code as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: claude  # written by `ouroboros setup --runtime claude`
```

When using the `--orchestrator` CLI flag, Claude Code is the default runtime backend.

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |  Claude Code    |
|  (your task)    |     |   (adapter.py)   |     |  (Pro/Max Plan) |
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

The default profile uses the Agent SDK and its bundled/authenticated Claude Code
transport. The SDK remains on MCP 1.x. The plugin-owned MCP 2 server is a
separate `uvx` process and uses `--runtime claude-cli`, so no interpreter loads
both MCP majors. For LiteLLM consensus models, see [`credentials.yaml`](../config-reference.md#credentialsyaml).

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Claude Code-Specific Strengths

- **Zero API key management** -- uses your Pro or Max Plan subscription directly
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

The database will be created automatically at the active path shown by `ouroboros config show`.

## Cost

Using Claude Code as the runtime backend with a Pro or Max Plan means:
- **No additional API costs** -- uses your subscription
- Execution time varies by task complexity
- Typical simple tasks: 15-30 seconds
- Complex multi-file tasks: 1-3 minutes
> **Note:** Pro plan ($20/month) works but has lower usage limits. For long agentic workflows, **Max plan is recommended** to avoid hitting limits mid-session.

## Active Conductor and Synapse

Claude Agent SDK and persisted Claude worker sessions are proven Synapse
`inform`/`after_turn` transports. Delivery resumes the same native session only
after the current turn; resumability is not presented as live checkpoint
`redirect`, and hard `replace` remains unsupported.

The main Claude conversation delegates exactly one read-only observer, stays
available to the user, and relays current runtime/model, efficiency assurance,
bounded Discover targets, dependency/parallel levels, first scheduled ACs,
attention, and terminal assurance. It chooses an AC semantically without asking
for internal IDs. Guidance is canonical English; the host responds naturally in
the user's current conversation language.
