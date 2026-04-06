# OpenClaw Channel Workflow Integration

This guide explains how an OpenClaw/Discord adapter can drive the
`ouroboros_channel_workflow` MCP tool for channel-native workflow orchestration.

## What this tool does

`ouroboros_channel_workflow` provides a transport-agnostic orchestration layer for:

- per-channel queueing
- default repository mapping per channel
- input-detected entry points
- in-channel interview bridging
- change-driven execution waiting and result reporting

It is designed to sit **between** an OpenClaw message adapter and the existing
Ouroboros interview / seed / execution pipeline.

## Boundary models

Use the OpenClaw contracts in:

- `src/ouroboros/openclaw/contracts.py`

Key models:

- `OpenClawChannelEvent`
- `OpenClawWorkflowCommand`
- `OpenClawWorkflowAdapter`

These models help normalize inbound channel events before translating them into
flat MCP tool arguments.

## Typical adapter flow

### 1. Configure a default repo for a channel

```python
from ouroboros.openclaw.contracts import OpenClawWorkflowCommand

command = OpenClawWorkflowCommand.set_repo(
    channel_id="1234567890",
    guild_id="guild-1",
    repo="/workspace/my-project",
)

tool_args = command.to_tool_arguments()
# -> {"action": "set_repo", "channel_id": "...", "guild_id": "...", "repo": "..."}
```

### 2. Convert an incoming channel message into a workflow command

```python
from ouroboros.openclaw.contracts import (
    OpenClawChannelEvent,
    OpenClawWorkflowCommand,
)

event = OpenClawChannelEvent(
    channel_id="1234567890",
    guild_id="guild-1",
    user_id="user-42",
    message="work on issue #320",
)

command = OpenClawWorkflowCommand.from_event(event)
tool_args = command.to_tool_arguments()
```

### 3. Call the MCP tool

```python
result = await mcp_client.call_tool("ouroboros_channel_workflow", tool_args)
channel_reply = result.text_content
```

### 4. Wait for execution changes

Long-running execution should use the wait-style action rather than tight polling:

```python
wait_args = OpenClawWorkflowCommand.wait(
    channel_id="1234567890",
    guild_id="guild-1",
    timeout_seconds=30,
).to_tool_arguments()

result = await mcp_client.call_tool("ouroboros_channel_workflow", wait_args)
```

## Thin adapter example

If you want the transport layer to stay minimal, use the adapter scaffold in:

- `src/ouroboros/openclaw/adapter.py`
- `src/ouroboros/openclaw/ux.py`

Example:

```python
from ouroboros.openclaw import (
    OpenClawChannelEvent,
    OpenClawWorkflowAdapter,
)

adapter = OpenClawWorkflowAdapter(client=mcp_client)

event = OpenClawChannelEvent(
    channel_id="1234567890",
    guild_id="guild-1",
    user_id="user-42",
    message="/ouro new work on issue #320",
)

result = await adapter.handle_event(event)
reply_text = result.value.reply_text
```

That adapter will:

1. parse explicit `/ouro ...` commands when present
2. fall back to plain-message workflow submission otherwise
3. call `ouroboros_channel_workflow`
4. return normalized reply text + metadata for the channel transport

Supported explicit commands:

- `/ouro repo set <repo>`
- `/ouro status`
- `/ouro queue`
- `/ouro poll`
- `/ouro wait`
- `/ouro new <message>`
- `/ouro answer <message>`

## Supported actions

### `action="set_repo"`
Configure the default repo for a channel.

Required:
- `channel_id`
- `repo`

Optional:
- `guild_id`

### `action="status"`
Inspect the current channel workflow state.

Required:
- `channel_id`

Optional:
- `guild_id`

### `action="message"`
Handle a user message in the channel.

Required:
- `channel_id`
- `message`

Optional:
- `guild_id`
- `user_id`
- `repo`
- `seed_content`
- `seed_path`
- `mode`

### `action="poll"`
Return the current active workflow state immediately.

Required:
- `channel_id`

Optional:
- `guild_id`

### `action="wait"`
Wait for execution state to change using the underlying job wait mechanism.

Required:
- `channel_id`

Optional:
- `guild_id`
- `timeout_seconds`

## Entry point behavior

The tool uses input detection:

- vague natural-language request -> interview
- issue / feature discussion -> interview
- seed/spec-like YAML payload -> execution

This means the adapter usually does **not** need to decide the starting stage itself.

## Queue behavior

- one active workflow per channel
- additional requests in the same channel are queued
- workflows in other channels remain independent

## Current expectations for adapters

An adapter should:

1. normalize inbound message events
2. call `ouroboros_channel_workflow`
3. post the returned text back into the originating channel
4. wait for execution changes while a job is active

An adapter does **not** need to:

- implement its own queue
- generate its own stage machine
- manually decide interview vs execution for most inputs

## Recommended runtime shape

Use a hybrid structure:

- **inbound messages** -> event-driven
- **execution updates** -> change-driven waiting

In practice:

1. inbound Discord/OpenClaw message arrives
2. adapter calls `ouroboros_channel_workflow(action="message", ...)`
3. if execution starts, adapter repeatedly calls `action="wait"`
4. adapter posts updates only when the returned text/meta actually change

This gives users an event-driven experience without forcing the transport layer
to implement its own workflow state machine.

## Recommended channel / thread policy

For the best Discord UX, prefer:

- parent channel: brief intake / completion notifications
- per-request thread: interview, execution progress, and terminal output

Recommended approach:

1. user posts a request in the parent channel
2. transport creates or chooses a workflow thread
3. all `ouroboros_channel_workflow` replies for that request go into the thread
4. parent channel only receives short lifecycle summaries when needed

This keeps interview noise and execution updates from overwhelming the main channel.

## Current limitation

This layer currently provides the Ouroboros-side orchestration contract and state model.
It does not yet include a concrete OpenClaw transport adapter in this repository.
