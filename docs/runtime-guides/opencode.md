<!--
doc_metadata:
  runtime_scope: [opencode]
-->

# Running Ouroboros with OpenCode

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros integrates with **OpenCode** ([opencode.ai](https://opencode.ai)) — an open-source multi-provider AI coding agent — via two complementary paths:

1. **Subagent Bridge Plugin (primary, recommended):** Runs inside an interactive OpenCode session. Ouroboros `ouroboros_*` MCP tools that emit a `_subagent` envelope (e.g. `ouroboros_qa`, `ouroboros_lateral_think persona="all"`) fan out into native OpenCode **Task panes** — one child session per subagent, rendered inline under the tool call. Zero session-picker pollution, parallel multi-persona dispatch, fresh LLM context per child.
2. **Subprocess Runtime (fallback, headless/CI):** Ouroboros launches `opencode run --format json` as a non-interactive subprocess per task execution. Useful for CLI-driven workflows, batch runs, and environments without an attached OpenCode session.

Both paths share the same specification-first harness (seeds, acceptance criteria, evaluation principles, deterministic exit conditions). Pick the plugin for day-to-day interactive work; pick the subprocess runtime for automation.

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** OpenCode supports any model available through your configured provider. For best results with Ouroboros workflows, use a frontier-class model (Claude Opus, GPT-5.4, or equivalent) that handles multi-step agentic coding tasks well.

## Prerequisites

- **OpenCode** installed, configured, and on your `PATH` (see [install steps](#installing-opencode) below)
- A **provider configured in OpenCode** (run `opencode` and complete the first-run setup, or use `opencode providers auth <provider>`)
- **Python >= 3.12**

> **Note:** OpenCode manages its own provider authentication. You do not need to set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` environment variables for Ouroboros — OpenCode handles provider credentials internally via its own configuration at `~/.config/opencode/opencode.jsonc` (or `opencode.json`).

## Installing OpenCode

OpenCode is distributed as a standalone binary. Install via the official installer script or npm:

```bash
# Recommended: official installer
curl -fsSL https://opencode.ai/install | bash

# Alternative: npm
npm i -g opencode-ai@latest
```

Verify the installation:

```bash
opencode --version
```

After install, run `opencode` once to complete first-run provider setup (select a provider and authenticate).

For alternative install methods, see the [OpenCode documentation](https://opencode.ai/docs).

## Installing Ouroboros

> For all installation options (pip, one-liner, from source) and first-run onboarding, see **[Getting Started](../getting-started.md)**.
> The base `ouroboros-ai` package includes the OpenCode runtime adapter — no extras are required.

## Platform Notes

The OpenCode runtime adapter targets Linux, macOS, and Windows via WSL 2. OpenCode itself supports macOS and native Windows; Ouroboros path handling and subprocess dispatch are portable.

| Platform | Status |
|----------|--------|
| Linux (x86_64 / ARM64) | Supported |
| macOS (Apple Silicon / Intel) | Supported |
| Windows (WSL 2) | Supported |
| Windows (native) | Best-effort — run inside WSL 2 for the subprocess fallback path |

## Configuration

`ouroboros setup --runtime opencode` configures OpenCode integration. At setup time, pick one of two **mutually exclusive** modes:

| Mode | What it does | Use when |
|------|--------------|----------|
| `plugin` (default) | Install bridge plugin + register MCP in `opencode.jsonc` | You drive work from inside OpenCode — inline Task panes via `_subagents` dispatch |
| `subprocess` | Write subprocess runtime into `~/.ouroboros/config.yaml` | Headless `ouroboros run`, CI, scripted pipelines, no interactive OpenCode session |

Why mutually exclusive: if an Ouroboros MCP tool is called inside a `opencode run` subprocess, the globally registered plugin also fires — duplicate subagent dispatch, wasted tokens. Pick one. To wire both deliberately on the same machine, run `ouroboros setup` twice with different `--opencode-mode` values and accept the token cost.

```bash
ouroboros setup --runtime opencode                              # interactive picker
ouroboros setup --runtime opencode --opencode-mode plugin       # inside-OpenCode default
ouroboros setup --runtime opencode --opencode-mode subprocess   # headless CI
ouroboros setup --runtime opencode --non-interactive            # accepts default (plugin)
```

What each mode installs:

**plugin**
- Bridge plugin at `<opencode_config_dir>/plugins/ouroboros-bridge/ouroboros-bridge.ts` (atomic write, content-hashed — no-op if unchanged)
- Plugin entry in `~/.config/opencode/opencode.jsonc` or `opencode.json` (dedupes stale entries)
- Ouroboros MCP server in the same file
- Claude Code MCP sidecar entry (if `~/.claude/` exists) — MCP is runtime-independent

**subprocess**
- `orchestrator.runtime_backend: opencode` in `~/.ouroboros/config.yaml`
- `orchestrator.opencode_cli_path: <auto-detected path>` in the same file
- `llm.backend: opencode` in the same file

> The `.jsonc` file is rewritten as plain JSON (comments stripped) for compatibility.

### Where things live

| Concern | File |
|---------|------|
| Ouroboros runtime settings (backend, CLI path) | `~/.ouroboros/config.yaml` |
| OpenCode provider / model / MCP / plugins | `~/.config/opencode/opencode.jsonc` (or `.json`) |
| Bridge plugin source | `<opencode_config_dir>/plugins/ouroboros-bridge/ouroboros-bridge.ts` |

Model selection for OpenCode-backed workflows is configured in OpenCode itself, not in `config.yaml`.

## Path 1 — Subagent Bridge Plugin [recommended]

The plugin hooks OpenCode's `tool.execute.after` event. When an Ouroboros MCP tool returns a `_subagent` / `_subagents` envelope, the plugin:

1. Spawns one independent **child session** per subagent (`client.session.create` + `client.session.prompt`)
2. Patches a `subtask` part into the parent message so the child renders as a native **Task pane** inline under the original tool call
3. Fans out up to `MAX_FANOUT = 10` children concurrently — each with fresh LLM context (no cross-persona anchoring bias)

Multi-persona example:

```
ouroboros_lateral_think persona="all"
  → hacker     (child session, Task pane)
  → researcher (child session, Task pane)
  → simplifier (child session, Task pane)
  → architect  (child session, Task pane)
  → contrarian (child session, Task pane)
```

### Environment tunables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_CHILD_TIMEOUT_MS` | `1200000` (20 min) | Per-child wall clock |
| `OUROBOROS_SUB_RETRIES` | `2` | Retry count on spawn failure |

See the full plugin guide: **[OpenCode Subagent Bridge](../guides/opencode-subagent-bridge.md)**.

## Path 2 — Subprocess Runtime (fallback)

For headless, CI, or scripted workflows where no interactive OpenCode session is running, select the subprocess runtime explicitly:

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: opencode
  opencode_cli_path: /usr/local/bin/opencode   # omit if on PATH

llm:
  backend: opencode
```

Or per-invocation:

```bash
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

The `OpenCodeRuntime` adapter launches `opencode run --format json` as a subprocess, pipes the prompt via stdin, and parses the structured JSON event stream from stdout. `orchestrator.opencode_permission_mode` defaults to `bypassPermissions` since `opencode run` is non-interactive.

### When to use subprocess over plugin

| Scenario | Path |
|----------|------|
| Interactive OpenCode session, want Task panes | Plugin |
| Parallel multi-persona dispatch (`lateral_think`, `qa`) | Plugin |
| CI / headless automation, no attached session | Subprocess |
| Scripted `ouroboros run workflow` invocation | Subprocess |
| Debug / reproduce one-shot from terminal | Subprocess |

### Could subprocess do parallel subagent dispatch without the plugin?

Yes, in theory. The orchestrator could spawn **N parallel `opencode run --format json` subprocesses**, one per subagent envelope entry, pipe each persona prompt via stdin, collect the stdout JSON event streams, and union-merge the results back into a single envelope.

Sketch:

```
parent = subprocess(opencode run --format json) ← seed prompt
         └─ hits MCP tool returning _subagents=[hacker, researcher, ...]
orchestrator
  ├─ subprocess(opencode run --format json) ← hacker prompt
  ├─ subprocess(opencode run --format json) ← researcher prompt
  └─ subprocess(opencode run --format json) ← simplifier prompt
         ↓ stdout JSON per child
      merge → parent envelope
```

Why we do **not** ship this path:

| Concern | Plugin | Subprocess fan-out |
|---------|--------|--------------------|
| Inline Task pane rendering under parent message | Yes (PATCH `subtask` part into parent) | No — each child is a top-level session |
| Session picker pollution | 1 parent session, N hidden children | N+1 visible sessions on every dispatch |
| Reparenting of child under parent message id | Yes (direct PATCH against `session._client`) | Not possible without plugin hook |
| Cold-start latency per child | One in-process `client.session.create` | Full CLI boot + TUI init per spawn |
| Live progress visible during run | Yes (native OpenCode rendering) | No — child output only surfaces after merge |
| Shared MCP/provider config inheritance | Automatic (same process) | Re-resolved per subprocess |
| Works headless / no attached session | No (needs running session) | Yes |

The subprocess runtime stays scoped to its strength — single-shot headless execution. Parallel subagent fan-out is a plugin-only feature by design; emulating it via subprocesses is feasible but strictly worse UX in every attached scenario.

## `ooo` Skill Availability on OpenCode

After running `ouroboros setup --runtime opencode`, the Ouroboros MCP server is registered in OpenCode's config. The `ooo` skills are available via MCP tool dispatch within OpenCode sessions.

| `ooo` Skill | OpenCode session | CLI equivalent (Terminal) |
|-------------|------------------|--------------------------|
| `ooo interview` | Yes | `ouroboros init start --llm-backend opencode "your idea"` |
| `ooo seed` | Yes | *(bundled in `ouroboros init start`)* |
| `ooo run` | Yes | `ouroboros run workflow --runtime opencode seed.yaml` |
| `ooo status` | Yes | `ouroboros status execution <execution_id>` |
| `ooo evaluate` | Yes | *(MCP only)* |
| `ooo evolve` | Yes | *(MCP only)* |
| `ooo ralph` | Yes | *(MCP only)* |
| `ooo cancel` | Yes | `ouroboros cancel execution <execution_id>` |
| `ooo unstuck` | Yes | *(MCP only)* |
| `ooo tutorial` | Yes | *(MCP only)* |
| `ooo welcome` | Yes | *(MCP only)* |
| `ooo update` | Yes | `pip install --upgrade ouroboros-ai` |
| `ooo help` | Yes | `ouroboros --help` |
| `ooo qa` | Yes | *(MCP only)* |
| `ooo setup` | Yes | `ouroboros setup --runtime opencode` |
| `ooo publish` | Yes | *(no direct `ouroboros publish` subcommand; skill/runtime flow uses `gh` CLI)* |

> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills with separate roles. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation.

## Quick Start

> For the full first-run onboarding flow (interview -> seed -> execute), see **[Getting Started](../getting-started.md)**.

### Verify Installation

```bash
opencode --version
ouroboros --help
```

## OpenCode-Specific Strengths

- **Multi-provider support** -- use Anthropic, OpenAI, Google, or other providers through a single runtime
- **Built-in provider management** -- OpenCode handles its own authentication and provider configuration, no env var setup required
- **Rich tool access** -- full suite of file, shell, and search tools (same surface as Claude Code)
- **Native MCP integration** -- OpenCode has built-in MCP server support
- **Open-source** -- fully open-source, allowing inspection and contribution
- **Session-aware runtime** -- Ouroboros preserves OpenCode session handles and resume state across workflow steps

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Runtime Differences

OpenCode, Claude Code, and Codex CLI are independent runtime backends with different tool sets, permission models, and provider ecosystems. The same Seed file works with all three, but execution paths may differ.

| Aspect | OpenCode | Claude Code | Codex CLI |
|--------|----------|-------------|-----------|
| What it is | Ouroboros session runtime backed by OpenCode subprocess | Anthropic's agentic coding tool | Ouroboros session runtime backed by Codex CLI transport |
| Authentication | Managed by OpenCode (`opencode providers auth`) | Max Plan subscription | OpenAI API key |
| Model | Any model supported by configured provider | Claude (via claude-agent-sdk) | GPT-5.4 with medium reasoning effort (recommended) |
| Tool surface | Read, Write, Edit, Bash, Glob, Grep | Read, Write, Edit, Bash, Glob, Grep | Codex-native tools (file I/O, shell) |
| Session model | Session-aware via `--session` flag and runtime handles | Native Claude session context | Session-aware via runtime handles, resume IDs, and skill dispatch |
| Transport | Subprocess (`opencode run --format json`), prompt via stdin | Claude Agent SDK (direct API) | Subprocess (`codex` executable) |
| Cost model | Provider API usage charges | Included in Max Plan subscription | OpenAI API usage charges |
| Tested platforms | Linux | Linux, macOS | Linux, macOS |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because OpenCode, Claude Code, and Codex CLI have different underlying agent capabilities, tool access, and provider ecosystems, they may produce different execution paths and results for the same Seed file.

## CLI Options

### Workflow Commands

```bash
# Execute workflow (OpenCode runtime)
# Seeds generated by ouroboros init are saved to ~/.ouroboros/seeds/seed_{id}.yaml
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime opencode --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime opencode --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Seed File Reference

| Field | Required | Description |
|-------|----------|-------------|
| `goal` | Yes | Primary objective |
| `task_type` | No | Execution strategy: `code` (default), `research`, or `analysis` |
| `constraints` | No | Hard constraints to satisfy |
| `acceptance_criteria` | No | Specific success criteria |
| `ontology_schema` | Yes | Output structure definition |
| `evaluation_principles` | No | Principles for evaluation |
| `exit_conditions` | No | Termination conditions |
| `metadata.ambiguity_score` | Yes | Must be <= 0.2 |

## Known Limitations

### Session pollution (subprocess runtime only)

Each task execution via `opencode run` creates a visible session in OpenCode's session history. Long-running workflows with many orchestrator steps will accumulate sessions. This does **not** affect the plugin path — child sessions created by the bridge are reparented inline as Task panes and do not pollute the picker. See [#331](https://github.com/Q00/ouroboros/issues/331) for subprocess reparenting.

### No interactive mode

The adapter uses `opencode run --format json` (non-interactive). Features that require interactive OpenCode sessions (e.g., manual approval prompts) are not available during Ouroboros execution.

### Permission mode not wired to CLI

OpenCode's `opencode run` command does not expose a `--permission-mode` flag. The `opencode_permission_mode` config value is stored for forward compatibility but is not currently passed to the subprocess. OpenCode runs non-interactively by default, so there is no approval dialogue to bypass.

## Troubleshooting

### OpenCode not found

Ensure `opencode` is installed and available on your `PATH`:

```bash
which opencode
```

If not installed:

```bash
curl -fsSL https://opencode.ai/install | bash
```

### Provider not configured

If OpenCode reports a provider error, ensure you have completed first-run setup:

```bash
opencode                        # interactive first-run setup
# or
opencode providers auth anthropic   # configure a specific provider
```

OpenCode manages its own provider credentials — you do not need to set `ANTHROPIC_API_KEY` or similar environment variables for the Ouroboros integration.

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Cost

Using OpenCode as the runtime backend incurs API charges from your configured provider. Costs depend on:

- Provider and model selected in OpenCode's configuration
- Task complexity and token usage
- Number of tool calls and iterations

Refer to your provider's pricing page for current rates.
