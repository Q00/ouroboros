# Getting Started with Ouroboros

> **Single source of truth for onboarding.** All install and first-run instructions live here.
> Runtime-specific configuration lives in [runtime guides](runtime-guides/). Architecture concepts live in [architecture.md](architecture.md).

Transform a vague idea into a verified, working codebase -- with any AI coding agent.

---

## Quick Start

### Recommended: Claude Code (`ooo`)

No Python install required. Run these three commands to go from idea to execution:

**1. Install the plugin** (in your terminal):
```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**2. Set up and build** (inside a Claude Code session -- start one with `claude`):
```
ooo setup
ooo interview "Build a task management CLI"
ooo run
```

That's it. `ooo interview` runs a Socratic interview that auto-generates a seed spec, and `ooo run` executes it.

> `ooo` commands are Claude Code skills. They only work inside an active Claude Code session.
> `ooo setup` registers the MCP server globally (one-time) and optionally configures your project.

---

### Alternative: Standalone CLI (`ouroboros`)

Use this path if you prefer a standalone terminal workflow, or are using a non-Claude runtime (e.g., Codex CLI, OpenCode).

**Requires Python >= 3.12.**

```bash
# Install
pip install ouroboros-ai

# Set up
ouroboros setup

# Run a seed spec
ouroboros run ~/.ouroboros/seeds/seed_abc123.yaml
```

> **Note:** The standalone CLI interview is invoked via `ouroboros init start "your context"` (not `ooo interview`, which is Claude Code-specific). The interview flow is identical across both tools. Power users can also author seed YAML files directly — see the [Seed Authoring Guide](guides/seed-authoring.md).

> **Tip:** `ouroboros run` requires a path to a seed YAML file as a positional argument (e.g., `ouroboros run ~/.ouroboros/seeds/seed_<id>.yaml`).

---

## Installation Details

### Option 1: Claude Code Plugin (Recommended)

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

No Python, pip, or API key configuration needed -- Claude Code handles the runtime.

### Option 2: pip Install

```bash
pip install ouroboros-ai              # Base package (core engine)
pip install ouroboros-ai[claude]      # + Claude Code runtime deps (anthropic, claude-agent-sdk)
pip install ouroboros-ai[litellm]     # + LiteLLM multi-provider support (100+ models)
pip install ouroboros-ai[mcp]         # + MCP server/client runtime support
pip install ouroboros-ai[tui]         # + Textual terminal UI
pip install ouroboros-ai[all]         # Everything (claude + litellm + mcp + tui)

ouroboros --version                   # verify CLI
```

> **Which extra do I need?** If you only use Claude Code as your runtime, `ouroboros-ai[claude]` is sufficient.
> For multi-model support via LiteLLM, use `ouroboros-ai[litellm]` or just grab everything with `ouroboros-ai[all]`.

**One-liner alternative** (auto-detects your runtime and installs matching extras):
```bash
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
```

### Option 3: From Source (Contributors)

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync                              # base dependencies only
uv sync --all-extras                  # or: include all optional extras
uv run ouroboros --version            # verify CLI
```

> See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contributor setup (linting, testing, pre-commit hooks).

### Prerequisites

| Path | Requirements |
|------|-------------|
| Claude Code (`ooo`) | Claude Code with plugin support |
| Standalone CLI (`ouroboros`) | Python >= 3.12, API key (Anthropic or OpenAI) |
| Codex CLI backend | Python >= 3.12, `npm install -g @openai/codex`, OpenAI API key with access to GPT-5.4 |
| OpenCode backend | Python >= 3.12, `opencode` on PATH, API key for your configured provider |

---

## Configuration

### API Keys

```bash
# Claude-backed flows
export ANTHROPIC_API_KEY="your-anthropic-key"

# Codex-backed flows
export OPENAI_API_KEY="your-openai-key"
```

> Claude Code plugin users: your Claude Code session provides credentials automatically. No export needed.

### Configuration File

`ouroboros setup` creates `~/.ouroboros/config.yaml` with sensible defaults. To edit manually:

```yaml
orchestrator:
  runtime_backend: claude   # claude | codex | opencode

llm:
  backend: claude_code      # claude_code | codex | litellm

logging:
  level: info
```

For Codex CLI, the recommended documented baseline is GPT-5.4 with medium reasoning effort. Put Ouroboros per-role overrides in `~/.ouroboros/config.yaml`, not in `~/.codex/config.toml`:

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

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
```

`ouroboros setup --runtime codex` uses `~/.codex/config.toml` only for the Codex MCP/env hookup and installs managed Ouroboros rules/skills into `~/.codex/`.

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

> **CLI note:** The standalone CLI does not have an `interview` command. Use `ooo interview` inside Claude Code, or use MCP tools to run interviews.

The Socratic Interviewer asks clarifying questions:
- "What platforms do you want to track?" (Bank accounts, credit cards, investments)
- "Do you need budgeting features?" (Yes, with category tracking)
- "Mobile app or web-based?" (Desktop-only with web export)
- "Data storage preference?" (SQLite, local file)

Answer until the ambiguity score drops below 0.2. The interview then auto-generates a seed spec:

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

> **Terminal users:** The standalone CLI does not have an `interview` command. Generate seeds via `ooo interview` in Claude Code or via MCP tools, then run with `ouroboros run <seed_file>`.

---

## Choosing a Runtime Backend

Ouroboros delegates code execution to a pluggable runtime backend. Three ship out of the box:

| | Claude Code | Codex CLI | OpenCode |
|---|---|---|---|
| **Best for** | Claude Code users; subscription billing | OpenAI ecosystem; pay-per-token billing | Multi-provider flexibility; open-source tooling |
| **Install** | `pip install ouroboros-ai[claude]` | `pip install ouroboros-ai` + `npm install -g @openai/codex` | `pip install ouroboros-ai` + `opencode` on PATH |
| **Skill shortcuts** | `ooo` inside Claude Code | `ooo` after `ouroboros setup --runtime codex` installs managed Codex skills | `ooo` after `ouroboros setup --runtime opencode` |
| **Config value** | `claude` | `codex` | `opencode` |

All three backends run the same core workflow engine (seed execution, TUI). However, user-facing commands still differ: Claude Code has native in-session `ooo` workflows, while Codex CLI and OpenCode rely on `ouroboros setup --runtime <backend>` to configure the integration. The `ouroboros` CLI remains the most universal terminal path, and some advanced operations are still MCP/Claude-only.

For backend-specific configuration:
- [Claude Code runtime guide](runtime-guides/claude-code.md)
- [Codex CLI runtime guide](runtime-guides/codex.md)
- [OpenCode runtime guide](runtime-guides/opencode.md)

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
python --version            # Must be >= 3.12
pip install --force-reinstall ouroboros-ai
ouroboros --version
```

### API key not found

```bash
export ANTHROPIC_API_KEY="your-key"     # or OPENAI_API_KEY
env | grep -E 'ANTHROPIC|OPENAI'        # verify
```

### MCP server issues

```bash
ouroboros mcp info
ouroboros mcp serve
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

Need help? Open an issue on [GitHub](https://github.com/Q00/ouroboros/issues).
