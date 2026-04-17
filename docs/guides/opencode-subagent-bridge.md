<!--
doc_metadata:
  runtime_scope: [opencode]
-->

# OpenCode Subagent Bridge Plugin

> OpenCode plugin that routes Ouroboros MCP tool calls into native OpenCode
> Task panes backed by **independent child sessions**, giving each subagent
> a clean LLM context while the main session stays uncluttered.

## What it does

When the main LLM invokes an Ouroboros MCP tool that emits a `_subagent` /
`_subagents` envelope, the bridge dispatches the work into a child session.
Tools that dispatch via the plugin when `runtime_backend=opencode` and
`opencode_mode=plugin` (default):

| Tool | Envelope | Child role |
|------|----------|-----------|
| `ouroboros_qa` | `_subagent` | QA judge |
| `ouroboros_lateral_think` (`persona=all`) | `_subagents` | one child per persona |
| `ouroboros_interview` | `_subagent` | Socratic interviewer |
| `ouroboros_pm_interview` | `_subagent` | PM interviewer |
| `ouroboros_generate_seed` | `_subagent` | seed architect |
| `ouroboros_execute_seed` | `_subagent` | executor |
| `ouroboros_start_execute_seed` | `_subagent` | executor (background job) |
| `ouroboros_evolve_step` | `_subagent` | evolution generation |
| `ouroboros_start_evolve_step` | `_subagent` | evolution (background job) |
| `ouroboros_evaluate` | `_subagent` | evaluator |

For each payload the bridge:

1. Parses the envelope in the `tool.execute.after` hook.
2. For each payload, spawns a **new child session** (`client.session.create`).
3. Prompts the child with the subagent prompt (`client.session.prompt`, sync).
4. Patches the original tool's assistant-message part with a `subtask` part
   via direct HTTP PATCH to `/session/{parent}/message/{mid}/part/{pid}` so
   the Task pane renders **inline** under the tool call, not in the main
   message stream.
5. Rewrites the tool output to a short `[Ouroboros] Dispatched ...` line.

End result:

- Main LLM sees a short dispatch notification.
- Each subagent runs in its own child session — independent context, no
  cross-contamination.
- Task panes appear inline in the parent session's UI.
- Subagent results stay in their panes.

## Why a bridge

Ouroboros MCP tools need isolated reasoning space for operations like QA
judgment, Socratic interview, multi-persona lateral thinking, and
evolutionary evaluation. Directly returning the subagent's work to the main
LLM pollutes its context and forces anchoring bias (especially for
multi-persona fan-out where each persona must think independently). The
bridge moves that work into child sessions while keeping the UI inline via
subtask parts.

## How it works

```
+--------------------+     +----------------------+     +---------------------+
| Main LLM turn      |     | Ouroboros MCP tool   |     | Bridge plugin       |
| calls ouroboros_*  | --> | emits _subagent(s)   | --> | tool.execute.after  |
+--------------------+     +----------------------+     +----------+----------+
                                                                   |
                       +-------------------------------------------+
                       |  for each payload:                        |
                       v                                           |
          +-----------------------------+       +------------------+-------+
          | client.session.create       |       | PATCH session/{parent}/  |
          | -> new childID              |       | message/{mid}/part/{pid} |
          | client.session.prompt       | ----> | body { type:"subtask",   |
          | -> waits for child result   |       |        sessionID:child } |
          +-----------------------------+       +--------------------------+
                       |
                       v
          +-----------------------------+
          | Inline Task pane renders    |
          | under the tool call         |
          +-----------------------------+
```

### Fan-out model (multi-persona)

`ouroboros_lateral_think` (and any tool emitting `_subagents: [...]`) spawns
**N independent child sessions** in parallel — one per persona. Each child
receives only its own prompt, so the five lateral-thinking personas produce
**unconflicted** angles with no anchoring bias.

| Aspect               | Behaviour |
|----------------------|-----------|
| Dispatch model       | One child session per subagent payload |
| Parallelism          | All children spawn concurrently; patches serialized by API |
| Max fan-out          | `MAX_FANOUT = 10` per tool call |
| Dedupe               | FNV-1a hash of sorted payloads, 5 s window |
| Child context        | Fresh session — no inherited main-LLM context |
| Result surfacing     | Parent patched with `subtask` part pointing at child |

## Robustness

### Retries and respawn

Every child dispatch wraps `create → prompt → PATCH` in a retry ladder:

| Layer           | Retries                                | Behaviour on failure |
|-----------------|----------------------------------------|----------------------|
| Child prompt    | `SUB_RETRIES` (default 2, total 3)     | New child session per retry (no stale state) |
| Part PATCH      | `PATCH_RETRIES = 3`                    | Exponential backoff (`BACKOFF_MS = 100`) |
| Part resolve    | `RESOLVE_RETRIES = 5`                  | Poll parent message for the tool part |
| Child timeout   | `CHILD_TIMEOUT_MS` (default 20 min)    | Abort child, retry or fall through to error |

### Canonical output

Successful dispatch writes this to the parent tool output (so the main LLM
can cite the child and read its final text):

```
task_id: {childID}

<task_result>
{last assistant text from child}
</task_result>
```

Errors surface in `metadata.ouroboros_dispatch_errors`. One failed payload
does **not** abort the rest of a fan-out batch.

### Environment knobs

| Variable                         | Default   | Purpose |
|----------------------------------|-----------|---------|
| `OUROBOROS_CHILD_TIMEOUT_MS`     | 1 200 000 | Per-child overall timeout (ms) |
| `OUROBOROS_SUB_RETRIES`          | 2         | Extra retries after first child attempt |

## Installation

Run `ouroboros setup` and select the OpenCode runtime. Install is
**atomic, idempotent, and content-hashed** — reruns are a no-op when the
plugin source is unchanged.

| Platform | Plugin directory |
|----------|------------------|
| Linux    | `~/.config/opencode/plugins/ouroboros-bridge/` (respects `$XDG_CONFIG_HOME`) |
| macOS    | `~/Library/Application Support/OpenCode/plugins/ouroboros-bridge/` |
| Windows  | `%APPDATA%\OpenCode\plugins\ouroboros-bridge\` |

What setup guarantees:

- Plugin source copied to the platform directory via `os.replace` (atomic).
- `opencode.json` `plugin` array deduped — stale entries from XDG shifts,
  sudo migrations, or legacy paths are removed, then the canonical path is
  appended.
- SHA-256 content hash compared before writing — identical content is left
  untouched (mtime preserved).

Restart OpenCode after setup. Verify by checking
`<plugin-dir>/bridge.log` — you should see an `INIT` line.

### Manual install (advanced)

Copy `ouroboros-bridge.ts` into the platform plugin directory and add its
path to `opencode.json`:

```json
{
  "plugin": ["/path/to/plugins/ouroboros-bridge/ouroboros-bridge.ts"]
}
```

## Verifying the plugin

In an OpenCode session with Ouroboros MCP tools available:

```
> run ouroboros_qa against a sample artifact
```

Expected:

1. Tool returns with `task_id: ses_... <task_result>...</task_result>`.
2. A Task pane opens inline under the tool call and streams child work.
3. `bridge.log` gains `DISPATCH tool=ouroboros_qa child=ses_...` lines.

Multi-persona:

```
> ouroboros_lateral_think with persona="all"
```

Expected: five inline Task panes, five distinct `child=ses_...` IDs, and
five independent `<task_result>` blocks — no shared context.

## Troubleshooting

### No `DISPATCH` in log, no Task pane
- Confirm the MCP tool name is prefixed `ouroboros_`.
- Confirm the tool output is valid JSON with `_subagent` or `_subagents`.
- Confirm the plugin path in `opencode.json` resolves to an existing file.

### `ERR` lines in log
Common causes:
- SDK older than v1.4.3. Run `opencode upgrade`.
- Unknown `agent` name — bridge falls back to `general` automatically, but
  a named agent must exist in the roster.
- Child timed out — raise `OUROBOROS_CHILD_TIMEOUT_MS`.

### Raw JSON envelope leaks to main LLM
The plugin hook did not run. Confirm `INIT` line exists in `bridge.log`
and that OpenCode was restarted after install.

### Task panes not inline
If the `subtask` part fails to patch, output falls back to a plain
dispatch note. Check `ERR PATCH part=... status=...` in `bridge.log`.

## Source

Plugin source: `src/ouroboros/opencode/plugin/ouroboros-bridge.ts`.
`ouroboros setup` deploys it to the platform plugin directory and keeps it
in sync on every run via content-hash comparison.

## See also

- [Running Ouroboros with OpenCode](../runtime-guides/opencode.md)
- [MCP API reference](../api/mcp.md)
