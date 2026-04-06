---
name: ouroboros
description: "When user message starts with 'ooo', call ouroboros_channel_workflow MCP tool. Do NOT answer the request yourself. Do NOT generate code. Do NOT summarize. Just call the tool and relay the response."
metadata:
  {
    "openclaw":
      {
        "emoji": "🐍",
        "requires": { "mcp": ["ouroboros"] },
      },
  }
---

# Ouroboros

## Rule

When a user message starts with `ooo`, you MUST:

1. Call `ouroboros_channel_workflow` MCP tool
2. Relay the tool response back to the channel
3. Do NOTHING else

Do NOT:
- Answer the request yourself
- Generate code or specs
- Summarize or reinterpret the request
- Offer alternatives or next steps
- Ask clarifying questions yourself

The Ouroboros MCP server handles all logic: interviewing, seed generation,
execution, and status tracking. Your only job is to be a relay.

## Setup

```bash
openclaw mcp set ouroboros '{"command":"uvx","args":["--from","ouroboros-ai","ouroboros","mcp","serve"]}'
```

## Routing

Strip `ooo ` prefix. Match the first word:

| First word | Tool arguments |
|---|---|
| `interview <text>` | `action: "message", mode: "new", message: "<text>"` |
| `seed` | `action: "message", mode: "new", message: "generate seed"` |
| `run <text>` | `action: "message", mode: "new", message: "<text>"` |
| `eval` | `action: "message", mode: "new", message: "evaluate"` |
| `status` | `action: "status"` |
| `poll` | `action: "poll"` |
| `wait` | `action: "wait", timeout_seconds: 30` |
| `repo <path>` | `action: "set_repo", repo: "<path>"` |
| anything else | `action: "message", mode: "answer", message: "<full text>"` |

Always include `channel_id`, `guild_id`, `user_id` from message context.
Always include `message_id` for dedup when available.

## Example

User: `ooo add dark mode to settings`

You call:

```json
{
  "tool": "ouroboros_channel_workflow",
  "arguments": {
    "action": "message",
    "channel_id": "C123",
    "guild_id": "G456",
    "user_id": "U789",
    "message": "add dark mode to settings",
    "mode": "new"
  }
}
```

Then post the tool response text to the channel. Nothing more.

User: `ooo yes use CSS variables`

You call:

```json
{
  "tool": "ouroboros_channel_workflow",
  "arguments": {
    "action": "message",
    "channel_id": "C123",
    "guild_id": "G456",
    "user_id": "U789",
    "message": "yes use CSS variables",
    "mode": "answer"
  }
}
```

Then post the tool response text to the channel. Nothing more.
