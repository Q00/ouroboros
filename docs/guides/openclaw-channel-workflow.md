# Channel Workflow Integration

This guide explains how to connect a chat platform (OpenClaw, Slack, Discord,
or any MCP-capable client) to the Ouroboros `ouroboros_channel_workflow` MCP
tool for channel-native workflow orchestration.

## What this tool does

`ouroboros_channel_workflow` provides a transport-agnostic orchestration layer for:

- per-channel queueing
- default repository mapping per channel
- input-detected entry points (interview vs. direct execution)
- in-channel interview bridging
- change-driven execution waiting and result reporting

## Setup

### 1. Install Ouroboros

```bash
pip install ouroboros-ai
# or
uv tool install ouroboros-ai
```

### 2. Register Ouroboros MCP server in OpenClaw

```bash
openclaw mcp set ouroboros '{"command":"uvx","args":["--from","ouroboros-ai","ouroboros","mcp","serve"]}'
```

Verify the registration:

```bash
openclaw mcp list
openclaw mcp show ouroboros
```

### 3. Install the OpenClaw skill

From ClawHub:

```bash
clawhub install ouroboros
```

Or manually copy `skills/ouroboros/SKILL.md` into your OpenClaw skills directory.

### 4. Test the connection

```bash
# Using mcporter (ad-hoc MCP tool call)
mcporter call ouroboros.ouroboros_channel_workflow \
  action=status channel_id=test

# Or via OpenClaw agent — prefix with ooo in any channel:
# ooo interview add dark mode to settings
```

### Example: Interview in a channel

![OpenClaw ooo interview example](../assets/openclaw-ooo.png)

A user types `ooo interview` followed by their request, and the bot responds with a Socratic interview question — all within the chat channel.

### Usage

All commands are prefixed with `ooo`:

```
ooo interview <request>    Start a requirements interview
ooo seed                   Generate seed from current interview
ooo run <description>      Start execution
ooo eval                   Evaluate current execution
ooo status                 Check workflow state
ooo poll                   Poll execution progress
ooo wait                   Long-poll for changes
ooo repo <path>            Set default repo for channel
ooo <answer text>          Answer an interview question
```

## Supported actions

### `action="set_repo"`

Configure the default repo for a channel.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `channel_id` | yes | Channel identifier |
| `guild_id` | no | Guild/server identifier |
| `repo` | yes | Repository path |

### `action="message"`

Handle a user message — starts interview or execution based on input detection.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `channel_id` | yes | Channel identifier |
| `message` | yes | User message content |
| `guild_id` | no | Guild/server identifier |
| `user_id` | no | Caller identifier |
| `repo` | no | Explicit repo override |
| `seed_content` | no | Inline seed/spec YAML |
| `seed_path` | no | Path to seed file |
| `mode` | no | `auto`, `new`, or `answer` |
| `message_id` | no | Transport message ID for dedup |
| `event_id` | no | Transport event ID for dedup |

### `action="status"`

Inspect current channel workflow state.

### `action="poll"`

Read the current active workflow state immediately.

### `action="wait"`

Wait for execution state to change (long-poll).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `timeout_seconds` | no | Max wait duration (default: 30) |

## Entry point detection

The tool automatically detects the appropriate starting stage:

- **Vague natural-language request** → starts an interview
- **Issue / feature discussion** → starts an interview
- **Seed/spec-like YAML payload** → skips to execution

## Queue behavior

- One active workflow per channel
- Additional requests are queued
- Workflows in other channels remain independent
- Queue advances automatically when a workflow completes or fails

## Response metadata

Every response includes a stable metadata envelope:

| Key | Description |
|-----|-------------|
| `action` | Action that produced the response |
| `channel_key` | Normalized channel key |
| `workflow_id` | Workflow identifier |
| `stage` | Current workflow stage |
| `entry_point` | Detected entry point (`interview` / `execution`) |
| `repo` | Target repository |
| `session_id` | Interview/runtime session ID |
| `job_id` | Background job ID |
| `job_status` | Underlying job status |
| `duplicate_delivery` | True if message was a redelivery |
