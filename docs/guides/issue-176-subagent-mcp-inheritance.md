## Issue 176: Delegated `ooo run` MCP Inheritance

### Problem
Delegated `ooo run` subagents were creating a fresh `OrchestratorRunner` inside the MCP
`ouroboros_execute_seed` / `ouroboros_start_execute_seed` handlers. That runner had no access
to the parent Claude session's already-merged effective MCP tool set, so delegated runs could
silently drop parent MCP tools, including session-bound tools such as Chrome DevTools MCP.

### Constraints
- Keep the current CLI and MCP tool surface unchanged
- Keep `orchestrator.session.*` event payloads unchanged
- Change only delegated subagent MCP inheritance behavior

### Design
1. In `ClaudeAgentAdapter.execute_task()`, register a `PreToolUse` hook for delegated
   execute-seed tool calls.
2. When the hook sees `ouroboros_execute_seed` or `ouroboros_start_execute_seed`, inject
   internal-only metadata into the tool input:
   - Parent Claude session ID
   - Parent transcript path / cwd / permission mode
   - Parent effective tool list used by the current orchestrator run
3. In `ExecuteSeedHandler.handle()`, read those internal fields only for new delegated
   executions. Build a `RuntimeHandle` with `metadata={"fork_session": True}` so the child
   Claude run forks from the parent session.
4. Pass the inherited runtime handle and inherited effective tool list into the delegated
   `OrchestratorRunner`.
5. Let the delegated runner merge inherited tools into its local tool set and pass the
   inherited runtime handle into direct execution, parallel AC execution, and coordinator
   review sessions.

### Why This Preserves Compatibility
- No public CLI parameters changed
- No MCP tool definitions changed
- No `orchestrator.session.*` event schema changed
- The inheritance metadata is internal-only and exists only on delegated tool calls

### Expected Result
Delegated `ooo run` subagents inherit the parent's effective MCP tool set after orchestrator
merging, and session-bound MCP servers remain available because child Claude sessions fork from
the parent session instead of starting from a blank runtime.
