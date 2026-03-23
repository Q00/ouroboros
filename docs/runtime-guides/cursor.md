# Running Ouroboros with Cursor IDE

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros works in **Cursor IDE** with or without the Claude Code extension. Cursor auto-loads MCP tools eagerly at server connection, so no `ToolSearch` step is needed.

## Prerequisites

- **Cursor IDE** installed
- **cursor-agent CLI** installed (`curl https://cursor.com/install -fsSL | bash`)
- **Python >= 3.12** (for the MCP server)
- **uvx** or **pip** to install the `ouroboros-ai` package

## Installation

### Option 1: Cursor standalone (no Claude Code needed)

```bash
# Install cursor-agent
curl https://cursor.com/install -fsSL | bash

# Install and configure ouroboros
pip install ouroboros-ai
ouroboros setup --runtime cursor
```

This automatically:
- Registers MCP server in `~/.cursor/mcp.json`
- No config.yaml needed — runtime is auto-detected from host environment
- Authentication is handled by cursor-agent ACP at runtime (no manual login needed)

Restart Cursor and you're ready to go.

### Option 2: Cursor + Claude Code extension

If the Claude Code extension is installed in Cursor:

```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

Then inside a Claude Code session:
```
ooo setup
```

> **Warning**: Do NOT register in both `~/.cursor/mcp.json` and the Claude Code plugin system. This causes duplicate MCP server instances. `ooo setup` detects and offers to resolve duplicates.

## How It Works

```
Cursor IDE (any model: GPT, Claude, Gemini, etc.)
    ↕ MCP protocol
Ouroboros MCP Server (Python backend)
    ↕ cursor-agent ACP (persistent session, shared process)
    Uses the model configured in your Cursor plan
```

Ouroboros uses the **Agent Client Protocol (ACP)** to communicate with
`cursor-agent`. A single `cursor-agent acp` process is kept alive and
shared across all LLM calls and agent executions — no per-call subprocess
overhead.

Key differences from Claude Code:

| Aspect | Claude Code | Cursor |
|--------|-------------|--------|
| Installation | `claude plugin install` | `ouroboros setup --runtime cursor` |
| MCP tool loading | Deferred (needs ToolSearch) | Eager (auto-loaded) |
| Question UI | AskUserQuestion tool | MCP Elicitation (native form) |
| Model | Claude only | Any model configured in Cursor |
| Sub-agent runtime | Claude Agent SDK | cursor-agent ACP |
| Plugin system | Native | Not required |

## Runtime Selection

The runtime is **auto-detected from the host environment**:
- Running in Cursor IDE → `cursor` runtime (uses cursor-agent ACP)
- Running in Claude Code → `claude` runtime (uses claude_agent_sdk)
- Running in Codex CLI → `codex` runtime

No config.yaml or manual selection needed. To override, set the environment variable:
```bash
export OUROBOROS_AGENT_RUNTIME=cursor
```

## MCP Tool Discovery

Cursor auto-loads all MCP tool schemas when the server connects. Unlike Claude Code, there is no need to call `ToolSearch` — all ouroboros tools (`ouroboros_interview`, `ouroboros_execute_seed`, etc.) are immediately available.

## Interview Question UI

Ouroboros uses **MCP Elicitation** to present interview questions as native forms in Cursor. The tool response also includes an `<ouroboros-meta>` JSON block with structured data:

```
<ouroboros-meta>
{
  "session_id": "interview_20260320_094856",
  "question": "What programming language?",
  "round_number": 1,
  "ambiguity_score": 0.91,
  "answer_mode": "freeform"
}
</ouroboros-meta>
```

## Verified Workflow

The following workflow has been tested end-to-end on Cursor:

| Step | Command | Result |
|------|---------|--------|
| Interview | `ooo interview "topic"` | Elicitation form + structured responses |
| Seed | `ooo seed` | Seed generation from interview |
| Run | `ooo run` | 8/8 AC pass, QA 0.92 |
| Evaluate | `ooo evaluate <session_id>` | Available |

## Known Limitations

### MCP Elicitation Intermittent

Cursor's elicitation support may intermittently fall back to text-based questions. Functionality is unaffected — only the UI presentation differs.

### Model Selection

Ouroboros reads the most recently used model from Cursor IDE's internal state
and sets it via ``session/set_config_option`` on the ACP session. This
usually matches the model you last used in the Cursor chat window. If
detection fails, the ACP session falls back to ``auto`` (cursor-agent's
default model).

### cursor-agent Authentication

Authentication is handled automatically by the ACP session at runtime.
If cursor-agent is not logged in, the ACP process will prompt for login.
No manual `cursor-agent login` step is required during setup.

## Troubleshooting

### MCP server not connecting

```bash
cat ~/.cursor/mcp.json | grep ouroboros
```

If missing, run: `ouroboros setup --runtime cursor`

### Duplicate server instances

If ouroboros appears twice in Cursor's MCP panel:
```bash
grep ouroboros ~/.cursor/mcp.json
grep ouroboros ~/.claude/plugins/installed_plugins.json
```

If both exist, remove the entry from `~/.cursor/mcp.json` — the plugin registration is sufficient.

### cursor-agent not found

```bash
which cursor-agent || ls ~/.local/bin/cursor-agent
```

If missing: `curl https://cursor.com/install -fsSL | bash`

### Tools not found

1. Check Cursor Settings > MCP — is the ouroboros server running?
2. Restart the MCP server from Cursor settings
3. Check server logs: `~/.ouroboros/logs/ouroboros.log`
