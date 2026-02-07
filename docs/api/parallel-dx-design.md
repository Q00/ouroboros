# Parallel Execution DX Improvement Design

> Generated: 2026-02-06 by parallel-dx agent team
> Status: Design complete, ready for implementation

## Overview

Parallel execution currently shows only bare tool names (`Sub-AC 1 → Bash`).
This design adds tool input details, agent thinking, and TUI real-time activity.

## Architecture Flow

```
SDK Message (ToolUseBlock.input, ThinkingBlock)
    |
adapter._convert_message()  <-- [1] tool_input + thinking extraction
    |
AgentMessage.data = {"tool_input": {...}, "tool_detail": "Read: src/foo.py", "thinking": "..."}
    |
parallel_executor._execute_atomic_ac()
    |-- console.print("Sub-AC 1 -> Read: src/foo.py")  <-- [2] rich console output
    +-- event_store.append("execution.tool.started")    <-- [3] TUI event
    |
app.py -> create_message_from_event -> ToolCallStarted
    |
dashboard_v3 -> tree inline indicator + detail panel   <-- [4] TUI display
```

---

## Phase 0: Adapter Enrichment (adapter.py)

### Problem

`_convert_message()` discards `ToolUseBlock.input` (file paths, commands, patterns)
and ignores `ThinkingBlock`. It also `break`s on the first block, losing multi-block data.

### Solution

**No AgentMessage dataclass changes.** All new data goes into `data` dict via well-known keys:

| Key | Type | When Set |
|---|---|---|
| `data["tool_input"]` | `dict` | ToolUseBlock has input (raw input dict) |
| `data["tool_detail"]` | `str` | Formatted: "Read: /path/to/file" |
| `data["thinking"]` | `str` | ThinkingBlock text content |

### Tool Detail Extraction Map

```python
_TOOL_DETAIL_EXTRACTORS: dict[str, str] = {
    "Read": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Edit": "file_path",
    "Write": "file_path",
    "Bash": "command",
    "WebFetch": "url",
    "WebSearch": "query",
    "NotebookEdit": "notebook_path",
}
```

For MCP tools (`tool_name.startswith("mcp__")`): first non-empty value, truncated to 80 chars.

### Format Function (module-level in adapter.py)

```python
def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    key = _TOOL_DETAIL_EXTRACTORS.get(tool_name)
    if key:
        detail = str(tool_input.get(key, ""))
    elif tool_name.startswith("mcp__"):
        detail = next((str(v)[:80] for v in tool_input.values() if v), "")
    else:
        detail = ""
    if detail and len(detail) > 80:
        detail = detail[:77] + "..."
    return f"{tool_name}: {detail}" if detail else tool_name
```

### _convert_message() Changes

Key changes from current code:
- Remove `break` after TextBlock/ToolUseBlock -- iterate ALL blocks
- Accumulate all TextBlock text with `\n` join
- Extract `ToolUseBlock.input` into `data["tool_input"]` and `data["tool_detail"]`
- Capture `ThinkingBlock` content into `data["thinking"]`

```python
if class_name == "AssistantMessage":
    msg_type = "assistant"
    content_blocks = getattr(sdk_message, "content", [])
    text_parts: list[str] = []

    for block in content_blocks:
        block_type = type(block).__name__

        if block_type == "TextBlock" and hasattr(block, "text"):
            text_parts.append(block.text)

        elif block_type == "ToolUseBlock" and hasattr(block, "name"):
            tool_name = block.name
            tool_input = getattr(block, "input", {}) or {}
            data["tool_input"] = tool_input
            data["tool_detail"] = _format_tool_detail(tool_name, tool_input)

        elif block_type == "ThinkingBlock":
            thinking = getattr(block, "thinking", "") or getattr(block, "text", "")
            if thinking:
                data["thinking"] = thinking.strip()

    if text_parts:
        content = "\n".join(text_parts)
    elif tool_name:
        content = f"Calling tool: {data.get('tool_detail', tool_name)}"
```

### Backward Compatibility

Zero breaking changes:
- AgentMessage dataclass unchanged
- All new data in existing `data: dict` via new keys
- Existing consumers use `.get()` -- they won't see new keys until they opt in
- Only behavioral change: `content` may now contain multi-block joined text

---

## Phase 0: Console Output (parallel_executor.py)

### Before / After

```
# Before:
  Sub-AC 1 of AC 2 -> Bash

# After:
  Sub-AC 1 of AC 2 -> Bash: pytest tests/unit/
```

### Decision: Always Show Details (No New Flag)

Tool details cost 1 extra field extraction and 0 vertical space.
The existing `--debug` flag stays for structlog output, thinking text, raw SDK messages.

### Code Changes (~25 lines total)

**New static method in ParallelACExecutor:**

```python
@staticmethod
def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    detail = ""
    if tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", "")
    elif tool_name == "Bash":
        detail = tool_input.get("command", "")
    elif tool_name in ("Glob", "Grep"):
        detail = tool_input.get("pattern", "")
    elif tool_name.startswith("mcp__"):
        for v in tool_input.values():
            if v:
                detail = str(v)[:50]
                break
    if detail and len(detail) > 60:
        detail = detail[:57] + "..."
    return f"{tool_name}: {detail}" if detail else tool_name
```

**In _execute_atomic_ac (~line 765):**

```python
if message.tool_name:
    tool_input = message.data.get("tool_input", {})
    tool_detail = self._format_tool_detail(message.tool_name, tool_input)
    self._console.print(
        f"{indent}[yellow]{label} -> {tool_detail}[/yellow]"
    )
```

### Rich Live Display: NOT Recommended

- Destroys scrollback history
- TUI already provides rich real-time view
- `console.print()` is append-only, greppable, pipeable
- If needed later, separate `--live` flag and different code path

### Interleaving: No Fix Needed

- Rich `Console.print()` acquires internal lock (thread-safe)
- Each line self-labeled (`AC 3 ->` / `Sub-AC 1 of AC 2 ->`)
- Interleaved concurrent output is expected (like `docker compose logs`)

---

## Phase 1: TUI Events (events.py, app.py)

### New Message Types

```python
class ToolCallStarted(Message):
    def __init__(self, execution_id, ac_id, tool_name, tool_input, call_index): ...

class ToolCallCompleted(Message):
    def __init__(self, execution_id, ac_id, tool_name, tool_input,
                 call_index, duration_seconds, success): ...

class AgentThinkingUpdated(Message):
    def __init__(self, execution_id, ac_id, thinking_text): ...
```

Event type strings for `create_message_from_event()`:
- `"execution.tool.started"` -> `ToolCallStarted`
- `"execution.tool.completed"` -> `ToolCallCompleted`
- `"execution.agent.thinking"` -> `AgentThinkingUpdated`

### TUIState Extensions

```python
@dataclass
class TUIState:
    # ... existing fields ...
    active_tools: dict[str, dict[str, str]] = field(default_factory=dict)
    tool_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    thinking: dict[str, str] = field(default_factory=dict)
```

### App Handlers

- `on_tool_call_started`: Update `_state.active_tools[ac_id]`, notify dashboard
- `on_tool_call_completed`: Remove from active, append to `tool_history`, notify
- `on_agent_thinking_updated`: Update `_state.thinking[ac_id]`, forward to dashboard

### Data Flow

```
parallel_executor._execute_atomic_ac()
    |-- message.tool_name? --> emit "execution.tool.started"
    |-- message.is_final?  --> emit "execution.tool.completed"
    +-- message.thinking?  --> emit "execution.agent.thinking"
    |
EventStore -> app._subscribe_to_events() (0.5s poll) -> post_message
    |
app.on_tool_call_started -> _state.active_tools -> _notify_ac_tree_updated
    |
DashboardScreenV3 -> tree inline indicator + detail panel + activity bar
```

---

## Phase 2: TUI Dashboard Enhancements (dashboard_v3.py)

### Enhanced Layout Mockup

```
+---------------------------------------------------------------------------------+
|  * Discover  ->  # Define  ->  * Design  ->  # Deliver    [3/5 AC] 2m34s $0.12 |
+--------------------------------------+------------------------------------------+
|  == AC EXECUTION TREE ==             |  == NODE DETAIL ==                        |
|  +-O Seed                            |  ID: ac_1                                 |
|    +-# AC1: Setup project    [OK]    |  Status: EXECUTING                        |
|    +-@ AC2: Implement auth   [3s]    |  Depth: 1                                 |
|    | +-# Sub1: Create model  [OK]    |  Children: 3                              |
|    | +-@ Sub2: Add routes    [2s]    |  ---                                      |
|    | |    Write -> src/routes.py      |  Content:                                 |
|    | +-O Sub3: Write tests           |  Implement user authentication with       |
|    +-@ AC3: Build frontend   [1s]    |  JWT tokens and session management...     |
|    |    Bash -> npm install           |  ---                                      |
|    +-O AC4: Add monitoring           |  Thinking:                                |
|    +-O AC5: Deploy config            |  "I need to create the auth middleware    |
|                                      |   first, then wire up the JWT..."         |
|  == LIVE ACTIVITY ==                 |  ---                                      |
|  +-----------------------------+     |  Recent Tool Calls:                       |
|  | AC2/Sub2  Write src/routes  |     |  1. Read src/auth/models.py     OK 0.3s  |
|  | AC3       Bash  npm install |     |  2. Write src/auth/middleware.py OK 0.5s  |
|  +-----------------------------+     |  3. Read src/routes/index.py    OK 0.2s   |
|                                      |  4. Write src/routes/auth.py    @ ...     |
+--------------------------------------+------------------------------------------+
|  p Pause  r Resume  t Tree  l Logs  d Debug                                     |
+---------------------------------------------------------------------------------+
```

### Widget Changes

**SelectableACTree**: Add `_active_tools` dict, `update_node_activity()`, `clear_node_activity()`.
Inline tool indicator on executing nodes: `Write -> src/routes.py`

**NodeDetailPanel**: Add thinking section + tool history list (last 8 calls with timing).

**LiveActivityBar** (new): Compact bar showing all active parallel agents.

**DoubleDiamondBar**: Add progress counter `[3/5 AC]`, elapsed time, cost.

---

## Implementation Priority

| Phase | Work | Files | LOC |
|-------|------|-------|-----|
| **P0** | adapter `_convert_message()` enrichment | adapter.py | ~40 |
| **P0** | console output `_format_tool_detail()` | parallel_executor.py | ~25 |
| **P1** | events.py new message types (3) | events.py | ~60 |
| **P1** | parallel_executor tool event emission | parallel_executor.py | ~30 |
| **P1** | app.py handlers + TUIState extensions | app.py, events.py | ~50 |
| **P2** | SelectableACTree inline activity | dashboard_v3.py | ~40 |
| **P2** | NodeDetailPanel thinking + tool history | dashboard_v3.py | ~60 |
| **P3** | LiveActivityBar widget | dashboard_v3.py | ~50 |

**P0 alone (~65 LOC) gives immediate DX improvement in console output.**
