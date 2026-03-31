# Runtime Compatibility Guide

> How Ouroboros works across Claude Code, Cursor, Codex, and generic MCP clients.
> Updated 2026-03-31 for native subagent architecture (PR #266).

---

## 1. Execution Modes

Ouroboros supports two execution modes, selected by `OUROBOROS_AGENT_MODE`:

| Mode | Env value | MCP behavior | Who drives LLM | When to use |
|------|-----------|-------------|-----------------|-------------|
| **Native** | `native` (default) | State-only CRUD | Host runtime | Runtimes with subagent support |
| **Internal** | `internal` | State + internal LLM | MCP server | Generic MCP clients without subagents |

### Native mode (default)

```
Host LLM → MCP (action=prepare → state → record_result)
    ↕
@ac-executor × N  (parallel, one per AC per stage)
```

MCP owns state only. The host LLM reads state, orchestrates execution via subagents (or sequentially), and records results back. This eliminates redundant LLM calls and gives users full visibility.

### Internal mode

```
Host LLM → MCP tool call → internal LLM orchestration → result
```

MCP spawns its own LLM sessions for interviews, seed generation, and execution. Any MCP client gets the full workflow regardless of capabilities — but execution is opaque and doubles LLM costs.

### Selecting the mode

The `action` parameter is the API boundary:
- **Explicit `action=prepare/state/record_result`** → always uses native flow
- **No `action` parameter** → uses internal/background execution for `execute_seed`. Other tools (`interview`, `generate_seed`) may auto-route based on `OUROBOROS_AGENT_MODE`.

Existing callers continue to work without changes. Native callers opt in explicitly.

---

## 2. Runtime Capability Matrix

| Capability | Claude Code | Cursor (v2.4+) | Codex CLI | Generic MCP |
|:-----------|:----------:|:------:|:-----:|:-----------:|
| Spawn subagents | ✅ `Agent` tool | ✅ `Task` tool | ✅ `spawn_agent` | ❌ |
| Parallel AC execution | ✅ multiple `Agent` calls | ✅ async + worktrees (8x) | ✅ concurrent threads (6x) | ❌ |
| Agent definitions | ✅ `.claude/agents/*.md` | ✅ `.cursor/agents/*.md` | ⚠️ TOML (`.codex/agents/`) | ❌ |
| MCP support | ✅ STDIO | ✅ STDIO | ✅ STDIO + HTTP | varies |
| Deferred tool loading | ✅ `ToolSearch` | ❌ (pre-loaded) | ❌ (pre-loaded) | ❌ |
| Structured user questions | ✅ `AskUserQuestion` | ✅ Q&A tool (non-blocking) | ❌ (free-form TUI) | ❌ |
| File watching / IDE | ✅ | ✅ | ✅ VS Code extension | ❌ |
| Nested subagents | ❌ (one level) | ✅ (tree structure) | ✅ (max_depth config) | ❌ |
| Background / CI mode | ✅ `Ctrl+B` | ✅ `is_background` | ✅ `codex exec` + resume | ❌ |
| Worktree isolation | ✅ `isolation: worktree` | ✅ up to 8 parallel | ❌ | ❌ |

---

## 3. Runtime Details

### Claude Code — Full Native Support ✅

`ooo run` spawns `@ac-executor` subagents in parallel via the `Agent` tool. Agent definitions are `.md` files in `.claude/agents/` with YAML frontmatter. This is the primary development target.

- `ToolSearch` defers tool definitions (~85% context reduction)
- `AskUserQuestion` for structured multi-choice UI (main conversation only)
- Plugin system: `.claude-plugin/plugin.json` + marketplace

### Cursor (v2.4+) — MCP + Skill Support ✅

`Task` tool spawns subagents from `.cursor/agents/*.md` definitions. Supports parallel execution via async subagents and worktree parallelism (up to 8 concurrent).

- No `ToolSearch` → MCP tools pre-loaded via `~/.cursor/mcp.json`
- Q&A tool is non-blocking (agent continues working while waiting for user)
- Tree-structured nesting (subagents can spawn sub-subagents)

### Codex CLI — MCP Works, Native Integration Pending ⚠️

MCP tools work after `~/.codex/config.toml` registration. Internal mode (background execution) is fully functional. However, **native subagent integration is not yet implemented** due to format differences:

| Area | Ouroboros Format | Codex Format | Gap |
|------|-----------------|-------------|-----|
| Agent definitions | `.md` with YAML frontmatter | TOML in `.codex/agents/` | Incompatible |
| Subagent spawning | `Agent` tool (SKILL.md) | `spawn_agent` + `wait_agent` | Different API |
| Skill triggering | `ooo run` → Skill tool | `$run` (native matching) | Different convention |
| MCP tool naming | `mcp__plugin_ouroboros_ouroboros__*` | `mcp__<server>__*` | Different prefix |
| Plugin packaging | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` | Not created |

**What works today on Codex:**
- All MCP tool calls via internal mode (no `action=` parameter)
- Background execution with `ouroboros_start_execute_seed`
- Job polling via `ouroboros_job_wait` / `ouroboros_job_result`

### Generic MCP Clients — Internal Mode Only

Any MCP-compatible client can call `ouroboros_execute_seed` without `action=` and get the full background execution flow. No host-side orchestration or subagent support required.

---

## 4. Hybrid Architecture Decision

This is an **intentional hybrid**, not an accidental fallback.

**Why not native-only?** Runtime independence matters. A generic MCP client that calls `ouroboros_interview` should get the full Socratic interview loop without needing SKILL.md instructions or subagents.

**Why not internal-only?** Native mode eliminates redundant LLM calls, gives users visibility, and enables parallel AC execution on capable runtimes.

**Maintenance cost:** Both paths share the same state layer (EventStore, SessionRepository, seed parsing). The divergence is only at the orchestration level. Handler changes that affect state or validation propagate to both modes automatically.

### Deprecation policy

No deprecation planned. Both modes serve different audiences:
- **Native**: IDE/CLI users (Claude Code, Cursor) who want transparency and efficiency
- **Internal**: Codex (pending native), generic MCP, and custom integrations

---

## 5. Future: Codex Native Support

Codex has full subagent and parallel execution capabilities. The gap is integration packaging, not runtime features.

### Required work

1. **Plugin manifest**: Create `.codex-plugin/plugin.json` with MCP server config
2. **Agent format adapter**: Convert `agents/*.md` (YAML frontmatter) to Codex TOML, or build a loader that reads our `.md` files and registers them as Codex agents
3. **SKILL.md Codex branch**: Add `spawn_agent`/`wait_agent` pattern alongside existing `Agent`/`Task` patterns
4. **Skill trigger convention**: Map `ooo X` → `$X` for Codex native skill matching
5. **MCP tool name abstraction**: Make tool references in skills runtime-aware (different prefix per runtime)

### Suggested approach

Start with MCP-only internal mode (works today, zero changes). Add `.codex-plugin/` manifest for marketplace discovery. Incrementally add native skill branches as the Codex plugin ecosystem matures.

---

## 6. Technical Notes

### MCP Server Configuration

All runtimes use the same MCP server binary. Mode is selected by environment variable:

**Claude Code** (`~/.claude/mcp.json`):
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

**Cursor** (`~/.cursor/mcp.json`): Same JSON format as Claude Code.

**Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.ouroboros]
command = "uvx"
args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]

[mcp_servers.ouroboros.env]
OUROBOROS_AGENT_MODE = "internal"
OUROBOROS_AGENT_RUNTIME = "codex"
OUROBOROS_LLM_BACKEND = "codex"
```

For internal mode, omit `OUROBOROS_AGENT_MODE` or set to `"internal"`.

### Local Development

Use `scripts/dev-sync.sh` to sync workspace changes to the plugin cache:
```bash
./scripts/dev-sync.sh          # sync skills/ agents/ hooks/ to all cached versions
./scripts/dev-sync.sh 0.26.6   # sync to a specific version only
```

---

## 7. History

| PR | Branch | Status | Description |
|----|--------|--------|-------------|
| #182 | `feat/cursor-platform-support-v2` | Closed | ACP-based runtime attempt. Failed: architecture change + ACP bugs |
| #266 | `refactor/native-subagent` | Current | Hybrid native/internal architecture with `action=` API boundary |
