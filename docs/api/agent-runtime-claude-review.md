# Agent Runtime / LLM Abstraction Review Brief

## Purpose

This document summarizes how far the Claude/Codex abstraction work has been implemented in `ouroboros`, what is now backend-neutral, what remains intentionally incomplete, and where a reviewer should focus.

The original goal was to stop treating Claude as a hardcoded execution backend and make room for:

- Claude Code runtime
- Codex CLI runtime
- backend-neutral LLM-only paths
- future runtimes such as OpenCode

## Current Status

The core architecture has been split into two layers:

- `AgentRuntime`
  - autonomous execution with tools, streaming progress, session resume
  - used by orchestrator execution paths such as `run`, `resume`, parallel AC execution, MCP execution/evolution flows
- `LLMAdapter`
  - bounded completion tasks
  - used by interview, ambiguity scoring, seed generation, QA, semantic evaluation, dependency analysis, and similar paths

This split is implemented and wired through the major entry points.

## Implemented Scope

### 1. Runtime Abstraction

Implemented:

- `AgentRuntime` protocol
- `RuntimeHandle` for backend-neutral resume state
- `TaskResult` / normalized `AgentMessage`
- runtime factory for backend selection
- Codex CLI runtime implementation
- Claude runtime kept as the existing implementation behind the abstract contract

Key files:

- `src/ouroboros/orchestrator/adapter.py`
- `src/ouroboros/orchestrator/codex_cli_runtime.py`
- `src/ouroboros/orchestrator/runtime_factory.py`
- `src/ouroboros/orchestrator/runner.py`
- `src/ouroboros/orchestrator/parallel_executor.py`

Concrete behavior:

- session progress now persists a normalized `runtime` payload instead of only Claude-specific `agent_session_id`
- resume paths deserialize `RuntimeHandle` and pass it back into the runtime
- legacy Claude `agent_session_id` is still supported as a fallback for old persisted sessions

### 2. LLM-Only Abstraction

Implemented:

- provider factory for LLM-only flows
- backend resolution for `claude_code`, `codex`, `litellm`
- permission-mode resolution for LLM-only flows
- Codex CLI-backed `LLMAdapter`
- shared config/env-driven model lookup for several previously Claude-defaulted paths

Key files:

- `src/ouroboros/providers/factory.py`
- `src/ouroboros/providers/codex_cli_adapter.py`
- `src/ouroboros/providers/claude_code_adapter.py`
- `src/ouroboros/config/loader.py`
- `src/ouroboros/config/models.py`

Concrete behavior:

- `create_llm_adapter()` is now the central construction path
- Codex LLM flows can run without API keys through the local `codex` CLI
- Claude-specific fallback construction inside MCP handlers and analyzer code was removed in favor of injected/factory-created `LLMAdapter`

### 3. Permission Policy Cleanup

Implemented:

- shared Codex permission mapping
- config/env defaults for runtime and LLM permission modes
- removal of hardcoded Codex permission assumptions from most call sites

Key files:

- `src/ouroboros/codex_permissions.py`
- `src/ouroboros/config/loader.py`
- `src/ouroboros/config/models.py`

Current mapping:

- `default` -> `--sandbox read-only`
- `acceptEdits` -> `--full-auto`
- `bypassPermissions` -> `--dangerously-bypass-approvals-and-sandbox`

Config entry points:

- `orchestrator.permission_mode`
- `llm.permission_mode`
- `OUROBOROS_AGENT_PERMISSION_MODE`
- `OUROBOROS_LLM_PERMISSION_MODE`

### 4. Entry Points Migrated

Implemented:

- `ouroboros run --runtime codex`
- `ouroboros init start --runtime codex --llm-backend codex`
- `ouroboros mcp serve --runtime codex --llm-backend codex`
- MCP tool factories/backend injection for execution and LLM-only paths

Key files:

- `src/ouroboros/cli/commands/run.py`
- `src/ouroboros/cli/commands/init.py`
- `src/ouroboros/cli/commands/mcp.py`
- `src/ouroboros/mcp/server/adapter.py`
- `src/ouroboros/mcp/tools/definitions.py`
- `src/ouroboros/mcp/tools/qa.py`

### 5. Recent Contract-Alignment Fixes

The latest pass tightened several remaining asymmetries:

- `init` interview adapter creation now goes through the backend-neutral factory path for all backends
- `CodexCliLLMAdapter` now accepts interview/debug-oriented constructor inputs such as:
  - `allowed_tools`
  - `max_turns`
  - `on_message`
- Codex LLM calls now emit best-effort debug callbacks from JSON events
- `ClaudeAgentAdapter` now accepts `cwd` and `cli_path` through the same factory contract used by other runtimes
- package/module docs that still framed the system as Claude-only were updated to describe the abstract runtime layer instead

Key files:

- `src/ouroboros/cli/commands/init.py`
- `src/ouroboros/providers/codex_cli_adapter.py`
- `src/ouroboros/providers/factory.py`
- `src/ouroboros/orchestrator/adapter.py`
- `src/ouroboros/orchestrator/runtime_factory.py`
- `src/ouroboros/orchestrator/__init__.py`
- `src/ouroboros/plugin/__init__.py`
- `src/ouroboros/plugin/agents/__init__.py`
- `src/ouroboros/plugin/agents/pool.py`

## Validation Performed

The following have been exercised during this implementation:

- targeted unit suites for:
  - runtime factory
  - Claude runtime adapter
  - Codex runtime
  - provider factory
  - Codex LLM adapter
  - config helpers/models
  - init runtime forwarding
- MCP startup/integration suites
- `tests/e2e`
- local smoke checks for:
  - `python -m ouroboros --help`
  - `python -m ouroboros init start --help`
  - `python -m ouroboros mcp info --runtime codex --llm-backend codex`

Most recent high-signal results:

- targeted abstraction tests: passing
- MCP/CLI integration tests: passing
- `tests/e2e`: `72 passed`

Known warning noise still present:

- `litellm` deprecation warnings
- some test-only coroutine/resource warnings in CLI/e2e suites

## Important Design Choices

### Runtime Resume State

Resume state is now represented as a backend-neutral `RuntimeHandle`, not only a Claude session ID.

This means:

- Claude stores `native_session_id`
- Codex CLI stores `native_session_id`
- future Responses/Conversation backends can store `conversation_id` and `previous_response_id`

### Claude vs Codex Semantics

The abstraction now aims for contract compatibility, not identical native behavior.

Examples:

- Claude runtime uses the Agent SDK directly
- Codex runtime shells out to `codex exec`
- Claude LLM adapter supports SDK-native multi-turn/tool semantics
- Codex LLM adapter is still a one-shot CLI completion path with best-effort event/callback translation

That difference is intentional, but it is the main place where parity should be reviewed carefully.

## Known Gaps / Intentional Limitations

These items are not closed yet:

- No `OpenCodeRuntime` implementation yet
- No Codex-native conversation-state LLM adapter yet
  - current Codex LLM path is CLI-backed, not Responses/Conversations-backed
- Codex LLM debug callbacks are best-effort
  - they are derived from JSON event output
  - they are not guaranteed to match Claude SDK streaming semantics exactly
- The runtime protocol still carries legacy `resume_session_id`
  - this remains for compatibility with existing call sites and persisted state
- Documentation outside the touched modules may still contain Claude-specific language

## What Claude Should Review

Please review with the following questions in mind:

1. Is the `AgentRuntime` contract actually sufficient for both Claude and Codex?
2. Are `RuntimeHandle` semantics stable enough for future backends?
3. Do any execution paths still depend on Claude-specific assumptions in non-doc code?
4. Are `cwd`, `cli_path`, permission mode, and resume semantics now propagated consistently through factories?
5. Is the Codex CLI runtime event normalization coherent with how the runner and workflow-state tracker interpret messages?
6. Does the Codex LLM adapter over-promise parity with Claude in places where behavior is still only best-effort?
7. Is backward compatibility for existing Claude session persistence acceptable?

## Reviewer Focus Areas

Highest-value files to read first:

- `src/ouroboros/orchestrator/adapter.py`
- `src/ouroboros/orchestrator/codex_cli_runtime.py`
- `src/ouroboros/orchestrator/runtime_factory.py`
- `src/ouroboros/orchestrator/runner.py`
- `src/ouroboros/providers/factory.py`
- `src/ouroboros/providers/codex_cli_adapter.py`
- `src/ouroboros/cli/commands/init.py`
- `src/ouroboros/mcp/server/adapter.py`
- `src/ouroboros/mcp/tools/definitions.py`

## Suggested Review Commands

Helpful local commands for reviewing the abstraction:

```bash
rg -n "AgentRuntime|RuntimeHandle|create_agent_runtime|create_llm_adapter|CodexCliRuntime|CodexCliLLMAdapter" src tests
```

```bash
uv run pytest tests/unit/orchestrator/test_adapter.py tests/unit/orchestrator/test_runtime_factory.py tests/unit/orchestrator/test_codex_cli_runtime.py tests/unit/providers/test_factory.py tests/unit/providers/test_codex_cli_adapter.py tests/unit/cli/test_init_runtime.py
```

```bash
uv run pytest tests/unit/cli/test_mcp_startup_cleanup.py tests/integration/mcp/test_server_adapter.py tests/e2e
```

## Bottom Line

The system is no longer Claude-only in its core execution architecture.

What is already true:

- orchestrator core depends on abstract runtime interfaces
- Codex runtime support is wired through the major CLI/MCP entry points
- LLM-only flows can use Claude, Codex, or LiteLLM through the provider factory
- permission handling for Codex has been centralized

What should still be reviewed critically:

- semantic parity between Claude SDK and Codex CLI behavior
- whether the current abstraction is the right long-term contract for future runtimes
- whether any remaining backend-specific assumptions are hidden behind apparently generic APIs
