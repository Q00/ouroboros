# Cursor Platform Support — Design Notes

> Updated 2026-03-31 for native subagent architecture (PR #266)

---

## 1. Goal

Enable Ouroboros across multiple runtimes (Claude Code, Cursor, Codex, generic MCP clients) with a single codebase. Each runtime gets the best experience its capabilities allow, without forcing the lowest-common-denominator.

---

## 2. Execution Modes

Ouroboros supports two execution modes, selected by `OUROBOROS_AGENT_MODE`:

| Mode | Env value | MCP behavior | Who drives LLM | When to use |
|------|-----------|-------------|-----------------|-------------|
| **Native** | `native` (default) | State-only CRUD | Host runtime (Claude Code, Cursor) | Runtimes with tool-calling LLMs |
| **Internal** | `internal` | State + internal LLM orchestration | MCP server | Generic MCP clients, Codex sandbox |

### Native mode (default)

```
Host LLM → MCP (action=prepare → state → record_result)
    ↕
@ac-executor × N  (parallel, one per AC per stage)
```

MCP owns state only. The host LLM reads state, orchestrates execution via subagents (or sequentially), and records results back. This eliminates redundant LLM calls and gives users full visibility.

### Internal mode (legacy)

```
Host LLM → MCP tool call → internal LLM orchestration → result
```

MCP spawns its own LLM sessions for interviews, seed generation, and execution. Any MCP client gets the full workflow regardless of capabilities — but execution is opaque and doubles LLM costs.

### Selecting the mode

The `action` parameter is the API boundary:
- **Explicit `action=prepare/state/record_result`** → always uses native flow
- **No `action` parameter** → always uses internal/background execution

This means existing callers (Codex, generic MCP) continue to work without changes, while native callers opt in explicitly.

---

## 3. Runtime Capability Matrix

| Capability | Claude Code | Cursor (v2.4+) | Codex CLI | Generic MCP |
|:-----------|:----------:|:------:|:-----:|:-----------:|
| Spawn subagents | ✅ `Agent` tool | ✅ `Task` tool | ✅ max 6 threads | ❌ |
| Parallel AC execution | ✅ multiple `Agent` calls | ✅ async + worktrees (8x) | ✅ concurrent threads | ❌ |
| Load agent `.md` definitions | ✅ `.claude/agents/` | ✅ `.cursor/agents/` | ✅ `AGENTS.md` (open std) | ❌ |
| MCP support | ✅ STDIO | ✅ STDIO | ✅ STDIO + HTTP | varies |
| Deferred tool loading | ✅ `ToolSearch` | ❌ (pre-loaded) | ❌ | ❌ |
| Structured user questions | ✅ `AskUserQuestion` | ✅ Q&A tool (non-blocking) | ❌ (free-form TUI) | ❌ |
| File watching / IDE | ✅ | ✅ | ✅ VS Code extension | ❌ |
| Nested subagents | ❌ (one level) | ✅ (tree structure) | ✅ (max_depth config) | ❌ |
| Background / CI mode | ✅ `Ctrl+B` | ✅ `is_background` | ✅ `codex exec` + resume | ❌ |
| Sandbox | ✅ Bash sandbox | ❌ | ✅ OS-level (seatbelt/bwrap) | N/A |
| Worktree isolation | ✅ `isolation: worktree` | ✅ up to 8 parallel | ❌ | ❌ |
| Recommended mode | native | native | native | internal |

### Claude Code

Full native subagent support. `ooo run` spawns `@ac-executor` subagents in parallel via the `Agent` tool. Subagents are `.md` files in `.claude/agents/` with YAML frontmatter.

Key features:
- `ToolSearch` defers tool definitions (~85% context reduction)
- `AskUserQuestion` for structured multi-choice UI (main conversation only)
- One level of subagent nesting (no sub-subagents)

### Cursor (v2.4+)

Full native subagent support via `Task` tool. Comparable to Claude Code's `Agent` tool — spawns subagents from `.cursor/agents/*.md` definitions. Supports parallel execution via async subagents and worktree parallelism (up to 8 concurrent agents).

Key differences from Claude Code:
- `Task` tool (equivalent to `Agent`) with tree-structured nesting
- No `ToolSearch` → MCP tools pre-loaded via `~/.cursor/mcp.json`
- Q&A tool is non-blocking (agent continues working while waiting for user)
- `SKILL.md` and `AGENTS.md` for skill/agent loading

### Codex CLI

Full native subagent support with up to 6 concurrent threads (`agents.max_threads`). First-class MCP integration (STDIO + streamable HTTP). Runs in OS-level sandbox (seatbelt on macOS, bwrap on Linux).

Key features:
- `AGENTS.md` open standard (shared with Cursor, Amp, Jules) for agent definitions
- `codex exec` non-interactive mode with resume capability for CI/CD
- Configurable sandbox modes: `read-only`, `workspace-write` (default), `danger-full-access`
- MCP server registration via `~/.codex/config.toml` or `codex mcp add`

### Generic MCP clients

Any MCP-compatible client can call `ouroboros_execute_seed` without `action=` and get the full background execution flow via internal mode. No host-side orchestration required. This covers custom scripts, API integrations, and lightweight MCP consumers that only implement the tool-call protocol.

---

## 4. Hybrid Architecture Decision

This is an **intentional hybrid**, not an accidental fallback.

**Why not native-only?** Runtime independence matters. A generic MCP client (custom script, API integration) that calls `ouroboros_interview` should get the full Socratic interview loop without needing to understand SKILL.md instructions or spawn subagents.

**Why not internal-only?** Native mode eliminates redundant LLM calls (no double-LLM), gives users visibility into execution, and enables parallel AC execution on capable runtimes.

**Maintenance cost:** Both paths share the same state layer (EventStore, SessionRepository, seed parsing). The divergence is only at the orchestration level — native mode returns state for the host to orchestrate, internal mode orchestrates within MCP. Handler changes that affect state or validation propagate to both modes automatically.

### Deprecation policy

No deprecation planned. Both modes serve different audiences:
- **Native**: IDE/CLI users (Claude Code, Cursor, Codex) who want transparency and efficiency
- **Internal**: Generic MCP consumers and custom integrations that want fire-and-forget simplicity

---

## 5. Technical Notes

### 5.1 MCP Server Configuration

All runtimes use the same MCP server binary. Mode is selected by environment variable:

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["--from", "ouroboros-ai[claude]", "ouroboros", "mcp", "serve"],
      "env": { "OUROBOROS_AGENT_MODE": "native" }
    }
  }
}
```

For internal mode (Codex, generic), omit the `env` block or set `"internal"`.

### 5.2 Cursor-Specific Setup

Cursor MCP registration goes to `~/.cursor/mcp.json` (not `~/.claude/mcp.json`). The `ooo setup` skill handles both paths automatically.

Known Cursor issues:
- Environment variable conflict: `CURSOR_EXTENSION_HOST_ROLE=user` + `CLAUDE_AGENT_SDK_VERSION` can confuse runtime auto-detection
- Plugin path duplication when both Claude Code extension and direct MCP registration are active

### 5.3 Local Development

Use `scripts/dev-sync.sh` to sync workspace changes to the plugin cache:
```bash
./scripts/dev-sync.sh          # sync skills/ agents/ hooks/ to all cached versions
./scripts/dev-sync.sh 0.26.6   # sync to a specific version only
```

---

## 6. Previous Attempts

### PR #182 (feat/cursor-platform-support-v2) — Closed

Attempted ACP-based runtime (`CursorACPRuntime`) with Cursor-specific LLM adapter. Failed due to architecture change (no LLM inside MCP) and ACP serialization bugs.

### PR #266 (refactor/native-subagent) — Current

Native subagent architecture with hybrid mode support. MCP = state CRUD in native mode, full orchestration in internal mode. Action parameter as the explicit API boundary.
