# Getting Started with Ouroboros

> **Single source of truth for onboarding.** All install and first-run instructions live here.
> Runtime-specific configuration lives in [runtime guides](runtime-guides/). Architecture concepts live in [architecture.md](architecture.md).

Transform a vague idea into a verified, working codebase -- with any AI coding agent.

---

## Quick Start

### Recommended: Claude Code (`ooo`)

You do not install Ouroboros with pip or install a global Python on this path.
The host needs **uv**: its `uvx` command launches the plugin's MCP server
([`.claude-plugin/.mcp.json`](../.claude-plugin/.mcp.json)), and its `uv` command
is the skills' final Python >= 3.12 fallback. Install uv with `pipx install uv`,
`pip install --user uv`, or `brew install uv`.

The welcome, setup, and seed skills prefer a compatible `python3`, then a
compatible `python`, and otherwise run with
`uv run --no-project --quiet --python '>=3.12' python`. Older global
interpreters are rejected instead of entering the first-run flow.

Then run the install commands in your terminal. Start Claude Code and follow
the two commands below in order:

**1. Install the plugin** (in your terminal):
```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**2. First command** (inside a Claude Code session -- start one with `claude`):
```
ooo setup
ooo interview "Build a task management CLI"
```

`ooo setup` is a one-time runtime configuration. `ooo interview` is the first
useful command: it starts the Socratic interview and returns a session that can
be continued into Seed generation and execution.

For a one-command pipeline after setup, use:

```
ooo auto "Build a task management CLI"
```

`ooo auto` runs bounded Socratic interview rounds, generates an A-grade Seed,
repairs B/C Seeds when possible, and starts execution only after the A-grade
gate passes. It returns an `auto_session_id` so interrupted or blocked runs can
be resumed.

Prefer the manual path when you want to answer every question yourself:

```
ooo interview "Build a task management CLI"
ooo run
```

> `ooo` commands are Claude Code skills. They only work inside an active Claude Code session.
> `ooo setup` registers the MCP server globally (one-time) and optionally configures your project.
> After setup, choose **Start now** to use the recommended model settings, or
> **Directly configure models** to select a model for each pipeline stage. You
> can reopen those settings any time with `ooo config`.

---

### Alternative: Standalone CLI (`ouroboros`)

Use this path if you prefer a standalone terminal workflow, or are using a non-Claude runtime (e.g., Codex CLI, OpenCode).

**Requires Python >= 3.12.**

```bash
# Install
pip install ouroboros-ai

# Set up the runtime once
ouroboros setup

# Start your first interview
ouroboros init start "Build a task management CLI"
```

After the interview produces a Seed, run it with:

```bash
ouroboros run ~/.ouroboros/seeds/seed_abc123.yaml
```

### Codex first use

For the Codex plugin, add the marketplace and install Ouroboros. This needs
`codex` on your `PATH` and `uvx` on the host — the plugin's MCP descriptor
launches the server with it ([`.mcp.codex.json`](../.mcp.codex.json)). Install uv
with `pipx install uv`, `pip install --user uv`, or `brew install uv`; you do not
need to install Python yourself.

```bash
codex plugin marketplace add Q00/ouroboros
codex plugin add ouroboros@ouroboros
```

Start a new Codex session and run these commands in order:

```
ooo setup
ooo interview "Build a task management CLI"
```

`ooo setup` is the one-time runtime preparation. Once prepared, Codex uses its
current default model automatically. Choose **Directly configure models** only
when you want to choose or pin a model for a pipeline stage; in Codex this
opens the local settings UI in your browser at a temporary `localhost` address.

For a standalone Codex CLI installation without the plugin, prepare the
integration once:

```bash
ouroboros setup --runtime codex
ouroboros init start "Build a task management CLI"
```

> **Note:** The standalone CLI interview is invoked via `ouroboros init start "your context"` (not `ooo interview`, which is Claude Code-specific). The interview flow is identical across both tools. Power users can also author seed YAML files directly — see the [Seed Authoring Guide](guides/seed-authoring.md).

> **Tip:** `ouroboros run` requires a path to a seed YAML file as a positional argument (e.g., `ouroboros run ~/.ouroboros/seeds/seed_<id>.yaml`).

---


### Auto mode: one-command A-grade pipeline

Use auto mode when you want the agentic pipeline to handle interview, Seed generation, quality gating, and execution handoff from one goal:

```bash
ooo auto "Build a local-first habit tracker CLI"
```

Useful variants:

```bash
ooo auto "Build a local-first habit tracker CLI" --skip-run
ooo auto --resume auto_abc123
```

When using the shell CLI directly, add `--show-ledger` to print the assumptions and non-goals captured during convergence:

```bash
ouroboros auto "Build a local-first habit tracker CLI" --show-ledger
```

Auto mode is hang-resistant by design: interview and repair loops are bounded, slow tool calls transition the auto session to `blocked` or `failed`, and execution handoff returns job/session IDs instead of waiting forever for completion. If auto mode stops, resume with the command printed by the surface you used: `ooo auto --resume <auto_session_id>` inside Claude Code, or `ouroboros auto --resume <auto_session_id>` from the standalone shell CLI.

---

## Installation Details

### Option 1: Claude Code Plugin (Recommended)

Requires uv on the host. Its `uvx` command launches the plugin's MCP server
([`.claude-plugin/.mcp.json`](../.claude-plugin/.mcp.json)), and its `uv`
command provides Python >= 3.12 to bundled skills when no compatible global
interpreter is available. Install uv with `pipx install uv`,
`pip install --user uv`, or `brew install uv`.

```bash
# Terminal
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

Then inside a Claude Code session:
```
ooo setup
ooo help        # verify installation
```

No pip install of Ouroboros and no API key configuration is needed -- Claude
Code handles the runtime. uv is the only host prerequisite: it provides `uvx`
for the MCP server and can provision Python >= 3.12 for shipped skill snippets.
A compatible global `python3` or `python` is only an optional fast path; when it
is absent or too old, the skills use the uv-managed interpreter.

### Option 2: pip Install

```bash
pip install ouroboros-ai              # Base package (core engine)
pip install 'ouroboros-ai[claude]'      # + default Claude Agent SDK profile (MCP 1.x)
pip install 'ouroboros-ai[claude-cli]'  # + dependency-free Claude CLI worker
pip install 'ouroboros-ai[claude-sdk]'  # + explicit alias for the SDK profile
pip install 'ouroboros-ai[litellm]'     # + LiteLLM multi-provider support; Python 3.12-3.13
pip install 'ouroboros-ai[mcp]'         # + MCP server/client runtime support
pip install 'ouroboros-ai[tui]'         # + Textual terminal UI
pip install 'ouroboros-ai[all]'         # MCP 1.x app bundle; excludes the MCP 2 server

ouroboros --version                   # verify CLI
```

> **Which extra do I need?** Use `ouroboros-ai[claude]` for the default Agent SDK runtime on MCP 1.x. Use `ouroboros-ai[mcp]` for the modern protocol server in a separate environment; its Claude launcher selects the dependency-free `[claude-cli]` worker. `[claude-sdk]` is an explicit alias for `[claude]`. Never combine either SDK spelling or `[all]` with `[mcp]` in one interpreter. See the [package compatibility and migration matrix](platform-support.md#mcp-2-and-claude-package-profiles).
> For multi-model support via LiteLLM, use `ouroboros-ai[litellm]` or just grab everything with `ouroboros-ai[all]` from Python 3.12 or 3.13; examples prefer Python 3.13.
> Core and non-LiteLLM installs support Python 3.12-3.14. See the [Python profile matrix](platform-support.md#python-profile-matrix).
> Legacy note: `ouroboros-ai[dashboard]` is still accepted as a compatibility alias/no-op and does not install dashboard runtime payload; `[all]` includes that no-op alias only for compatibility.

**One-liner alternative** (auto-detects your runtime and installs matching extras):
```bash
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | OUROBOROS_INSTALL_REF=docs-getting-started bash
```

### Option 3: From Source (Contributors)

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync                              # base dependencies only
uv sync --python 3.13 --extra all     # include the MCP 1.x app bundle and LiteLLM
uv run ouroboros --version            # verify CLI
```

Source checkouts use the repository `.python-version`, which currently defaults to **stable Python 3.14**. Core and non-LiteLLM source environments support Python 3.12-3.14. LiteLLM-bearing source environments, including `--extra all`, support Python 3.12-3.13; examples prefer Python 3.13 without making it the minimum. Do not use `--all-extras`: that asks uv to select the intentionally conflicting MCP 1.x application and MCP 2 server profiles together.

```bash
uv sync --python 3.13                  # base dependencies on the preferred current interpreter
uv sync --python 3.13 --extra all      # include co-installable backends/extras, including LiteLLM
uv run --python 3.13 ouroboros --version
uv run --python 3.13 pytest tests/unit/ -q
```

> See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contributor setup (linting, testing, pre-commit hooks).

### Windows users

Use WSL 2 for the supported Windows path, then run the Linux install commands from inside the WSL distribution. Windows 11 Home can run WSL 2; if `wsl --install` or distro installation fails, see [Windows WSL 2 troubleshooting](guides/windows-wsl-troubleshooting.md).

### Prerequisites

| Path | Requirements |
|------|-------------|
| All runner sessions | Git >= 2.36.0 on PATH. Project identity requires the unambiguous `worktree list --porcelain -z` topology grammar even for a non-Git local workspace; an older or unrepresentable Git version is a non-retryable configuration error. |
| Claude Code (`ooo`) | Claude Code with plugin support |
| Standalone CLI (`ouroboros`) | Python >= 3.12, API key (Anthropic or OpenAI) |
| Codex CLI backend | Python >= 3.12, `npm install -g @openai/codex`, and a signed-in Codex CLI account with access to a Codex-supported model |
| OpenCode backend | Python >= 3.12, `opencode` on PATH, provider configured in OpenCode |
| Kiro CLI backend | Python >= 3.12, `kiro-cli` on PATH (signed in to Kiro), plus `pipx install 'ouroboros-ai[mcp]'` or `uv tool install 'ouroboros-ai[mcp]'`. Then `ouroboros setup --runtime kiro` registers the isolated Ouroboros MCP server in `~/.kiro/settings/mcp.json` |
| GitHub Copilot CLI backend | Python >= 3.12, `copilot` on PATH, `gh` on PATH (`gh auth login`), plus `pipx install 'ouroboros-ai[mcp]'` or `uv tool install 'ouroboros-ai[mcp]'`. Then `ouroboros setup --runtime copilot` live-discovers available models, picks a default, and registers the Ouroboros MCP server in `~/.copilot/mcp-config.json` |
| Pi CLI backend | Python >= 3.12, `pi` on PATH or `orchestrator.pi_cli_path` configured. Use `runtime_backend: pi` for workflow execution. Use `llm.backend: pi` only when authoring/evaluation flows can accept Pi's adapter-level JSON extraction and schema validation rather than native `--output-schema` enforcement |

---

## Configuration

### API Keys

```bash
# Claude-backed flows
export ANTHROPIC_API_KEY="your-anthropic-key"

# Codex-backed flows when using API-key authentication
export OPENAI_API_KEY="your-openai-key"
```

> Codex CLI can also use its normal account sign-in, so `OPENAI_API_KEY` is not required unless you choose API-key authentication. Claude Code plugin users: your Claude Code session provides credentials automatically. No export needed.

### Configuration File

`ouroboros setup` creates `~/.ouroboros/config.yaml` with sensible defaults. To edit manually:

```yaml
orchestrator:
  runtime_backend: claude   # default SDK runtime on MCP 1.x

llm:
  backend: claude_code      # claude_code | codex | litellm | copilot | opencode | gemini | goose | kiro | pi

logging:
  level: info

runtime_controls:
  mcp_tool_timeout_seconds: 0                     # no fixed adapter wall-clock cap
  generation_idle_timeout_seconds: 7200           # 2h with no activity
  generation_no_progress_timeout_seconds: 14400  # 4h without material progress
```

For Codex CLI, leave the model on Codex's default unless you intentionally need a pin. `ouroboros config --web` and `ouroboros config` offer **Use Codex default model** for that choice; choose **Enter another model ID…** when you want to pin a stage to a model that is not listed. Ouroboros applies the task's reasoning effort per invocation. The equivalent `~/.ouroboros/config.yaml` model pins look like this:

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex

llm:
  backend: codex
  qa_model: gpt-5.4

clarification:
  default_model: gpt-5.4

execution:
  default_model: gpt-5.4  # omit, or choose Use Codex default model in config --web

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
```

`ouroboros setup --runtime codex` uses `~/.codex/config.toml` only for the Codex MCP/env hookup and installs managed Ouroboros rules/skills into `~/.codex/`. Existing URL/custom Ouroboros MCP entries are preserved by default; run `ouroboros codex refresh` when you only need to refresh `~/.codex/rules/ouroboros*.md` and `~/.codex/skills/ouroboros-*`.

### Environment Variables

```bash
# Override the runtime backend (highest priority)
export OUROBOROS_AGENT_RUNTIME=codex
```

Resolution order: `OUROBOROS_AGENT_RUNTIME` env var > `config.yaml` > auto-detection during `ouroboros setup`.

For the full list of configuration keys, see [Configuration Reference](config-reference.md).

---

## Your First Workflow

This tutorial walks through a complete workflow. Examples use `ooo` skills (Claude Code); CLI equivalents are shown in callouts for terminal-based workflows.

### Step 1: Interview

Inside a Claude Code session:
```
ooo interview "I want to build a personal finance tracker"
```

> **CLI note:** You can also run interviews from the terminal with `ouroboros init start --llm-backend <backend> "your idea"` (where `<backend>` is `claude_code`, `codex`, `opencode`, or `litellm`). For in-agent `ooo interview` usage: Claude Code works out-of-the-box; Codex CLI and OpenCode require `ouroboros setup --runtime <codex|opencode>` first to register the MCP server.

The Socratic Interviewer asks clarifying questions:
- "What platforms do you want to track?" (Bank accounts, credit cards, investments)
- "Do you need budgeting features?" (Yes, with category tracking)
- "Mobile app or web-based?" (Desktop-only with web export)
- "Data storage preference?" (SQLite, local file)

Answer until the ambiguity score drops below 0.2, and the interview auto-generates a seed spec. If you would rather stop before that, the CLI offers to force generation at whatever score you are on:

```yaml
# Auto-generated seed (example)
goal: "Build a personal finance tracker with SQLite storage"
constraints:
  - "Desktop application only"
  - "Category-based budgeting"
  - "Export to CSV/Excel"
acceptance_criteria:
  - "Track income and expenses"
  - "Categorize transactions automatically"
  - "Generate monthly reports"
  - "Set and monitor budgets"
metadata:
  ambiguity_score: 0.15
  seed_id: "seed_abc123"
```

### Step 2: Execute

```
ooo run
```

> **CLI equivalent:** `ouroboros run ~/.ouroboros/seeds/seed_abc123.yaml` (requires the seed file path as a positional argument)

Ouroboros decomposes the seed into tasks via the Double Diamond (Discover -> Define -> Design -> Deliver) and executes them through your configured runtime backend.

### Step 3: Monitor

Open a second terminal to watch progress in the TUI dashboard:

```bash
ouroboros monitor
```

The dashboard shows:
- Double Diamond phase progress
- Acceptance criteria tree with live status
- Cost, drift, and agent activity

See [TUI Usage Guide](guides/tui-usage.md) for keyboard shortcuts and screen details.

### Step 4: Review

`ooo run` (or `ouroboros run`) prints a session summary with the QA verdict when complete.

Useful follow-ups:

```
ooo evaluate          # Re-run 3-stage evaluation
ooo status            # Check drift and session state
ooo evolve            # Start evolutionary refinement loop
```

> **CLI equivalent:** `ouroboros run seed.yaml --resume <session_id>` to resume, `ouroboros run seed.yaml --debug` for verbose output.

---

## Common Workflows

### New Project from Scratch

```
ooo interview "Build a REST API for a blog"
ooo run
```

### Bug Fix

```
ooo interview "User registration fails with email validation"
ooo run
```

### Feature Enhancement

```
ooo interview "Add real-time notifications to the chat app"
ooo run
```

> **Terminal users:** Run interviews from the terminal with `ouroboros init start --llm-backend <backend> "your idea"`, then execute with `ouroboros run workflow <seed_file>`. (Separate from in-agent `ooo` usage; terminal flows don't require MCP registration.)

---

## Choosing a Runtime Backend

Ouroboros delegates code execution to a pluggable runtime backend. Three ship out of the box:

| | Claude Code | Codex CLI | OpenCode |
|---|---|---|---|
| **Best for** | Claude Code users; subscription billing | OpenAI ecosystem; pay-per-token billing | Multi-provider flexibility; open-source tooling |
| **Install** | `pip install 'ouroboros-ai[claude]'` (SDK/MCP 1.x); plugin MCP server uses isolated `[mcp]`/MCP 2 | `pip install ouroboros-ai` + `npm install -g @openai/codex` | `pip install ouroboros-ai` + `opencode` on PATH |
| **Skill shortcuts** | `ooo` inside Claude Code | `ooo` after `ouroboros setup --runtime codex` installs managed Codex skills | `ooo` after `ouroboros setup --runtime opencode` |
| **Config value** | `claude` (SDK default); `claude_mcp` only for explicit CLI worker | `codex` | `opencode` |

All three backends run the same core workflow engine (seed execution, TUI). However, user-facing commands still differ: Claude Code has native in-session `ooo` workflows, while Codex CLI and OpenCode rely on `ouroboros setup --runtime <backend>` to configure the integration. The `ouroboros` CLI remains the most universal terminal path, and some advanced operations are still MCP/Claude-only.

For backend-specific configuration:
- [Claude Code runtime guide](runtime-guides/claude-code.md)
- [Codex CLI runtime guide](runtime-guides/codex.md)
- [OpenCode runtime guide](runtime-guides/opencode.md)
- [Kiro CLI runtime guide](runtime-guides/kiro.md)
- [GitHub Copilot CLI runtime guide](runtime-guides/copilot.md)
- [Pi CLI runtime guide](runtime-guides/pi.md)
- [Pi JSON mode documentation](https://pi.dev/docs/latest/json)

---

## Troubleshooting

### Claude Code skill not recognized

```bash
# Check skill is installed
claude plugin list

# Reinstall if needed
claude plugin install ouroboros@ouroboros --force
```

### Python / CLI issues

```bash
python --version            # Must be >= 3.12 and should be a stable release
pip install --force-reinstall ouroboros-ai
ouroboros --version
```

For source checkouts, `uv run ...` follows `.python-version` and may choose Python 3.14. Use Python 3.13 for LiteLLM-bearing source environments such as `uv sync --python 3.13 --extra all`, or Python 3.12 when validating the lower supported bound. Core and non-LiteLLM source environments still support Python 3.12-3.14.

### API key not found

```bash
export ANTHROPIC_API_KEY="your-key"     # or OPENAI_API_KEY
env | grep -E 'ANTHROPIC|OPENAI'        # verify
```

### MCP server issues

```bash
ouroboros mcp info
ouroboros mcp serve --runtime claude-cli
```

### TUI not displaying

```bash
export TERM=xterm-256color
ouroboros tui monitor
```

### Stuck execution

Inside Claude Code:
```
ooo unstuck
```

From terminal:
```bash
ouroboros run seed.yaml --resume <session_id>
ouroboros cancel execution <session_id>
```

### Quick Reference

| Issue | Solution |
|-------|----------|
| Skill not loaded | `claude plugin install ouroboros@ouroboros --force` |
| CLI not found | `pip install ouroboros-ai` |
| API errors | Check `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| TUI blank | `export TERM=xterm-256color` |
| High costs | Reduce seed scope or use a lower model tier |
| Execution stuck | `ooo unstuck` or `ouroboros run seed.yaml --resume <id>` |

---

## Best Practices

### For Better Interviews
1. **Be specific** -- "build a Twitter clone with real-time messaging" beats "build a social app"
2. **State constraints early** -- budget, timeline, technical limitations
3. **Define success** -- clear acceptance criteria produce better seeds

### For Effective Seeds
1. **Include non-functional requirements** -- performance, security, scalability
2. **Define boundaries** -- what is in scope and what is not
3. **Specify integrations** -- APIs, databases, third-party services

### For Successful Execution
1. **Validate first** -- `ouroboros run seed.yaml --dry-run` checks YAML and schema before executing
2. **Monitor with the TUI** -- run `ouroboros monitor` in a separate terminal during long workflows
3. **Keep QA enabled** -- post-execution QA runs automatically unless you pass `--no-qa`

---

## Next Steps

- [Seed Authoring Guide](guides/seed-authoring.md) -- advanced seed customization
- [Evaluation Pipeline](guides/evaluation-pipeline.md) -- understand the 3-stage verification gate
- [TUI Usage Guide](guides/tui-usage.md) -- dashboard screens and keyboard shortcuts
- [Architecture](architecture.md) -- system design and component overview
- [Configuration Reference](config-reference.md) -- all config keys and defaults
- [Claude Code runtime guide](runtime-guides/claude-code.md) -- backend-specific setup
- [Codex CLI runtime guide](runtime-guides/codex.md) -- backend-specific setup
- [OpenCode runtime guide](runtime-guides/opencode.md) -- backend-specific setup
- [Kiro CLI runtime guide](runtime-guides/kiro.md) -- backend-specific setup
- [GitHub Copilot CLI runtime guide](runtime-guides/copilot.md) -- backend-specific setup with live model discovery

Need help? Open an issue on [GitHub](https://github.com/Q00/ouroboros/issues).
