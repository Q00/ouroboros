# Getting Started with Ouroboros

Transform your vague ideas into validated specifications and execute them with confidence.

> **Command context guide:** This page contains commands for two different contexts:
> - **Terminal** -- commands you run in your regular shell (bash, zsh, etc.)
> - **Inside a runtime session** -- `ooo` skill commands run inside a Claude Code session; Codex CLI users run equivalent `ouroboros` CLI commands in their terminal
>
> Each code block is labeled to indicate where to run it.

## Quick Start

### Claude Code (Skill Mode -- No Python Required)

**Terminal -- install the skill:**
```bash
# Run these in your regular terminal (shell)
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**Inside a Claude Code session -- run setup, then start building:**
```
# Run these inside an active Claude Code session (start one with `claude`)
ooo setup
ooo interview "Build a task management CLI"
ooo seed
```

> **Important:** `ooo` commands are Claude Code skills, not shell commands. They only work inside a Claude Code session. For Codex CLI or standalone usage, use the `ouroboros` CLI instead (see Full Mode below).
> `ooo setup` registers the MCP server globally (one-time) and optionally adds an Ouroboros reference block to your project's CLAUDE.md (per-project).

**Done!** You now have a validated specification ready for execution.

### Full Mode (Python >= 3.12 Required)

**Terminal:**
```bash
# Setup
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync

# Configure
export ANTHROPIC_API_KEY="your-key"
ouroboros setup

# Execute
ouroboros run workflow ~/.ouroboros/seeds/latest.yaml
```

---

## Installation Guide

### Prerequisites
- **Claude Code** (for Skill Mode) or **Codex CLI** (for Codex runtime)
- **Python >= 3.12** (for Full Mode / Codex runtime)
- **API Key** from OpenAI, Anthropic, or compatible provider

### Option 1: Claude Code Skill Mode (Recommended for Claude Code Users)

**Terminal:**
```bash
# Install via Claude Code marketplace
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**Inside a Claude Code session:**
```
# Start a session first with `claude`, then run:
ooo setup

# Verify installation
ooo help
```

### Option 2: pip Install (For Users)

**Terminal:**
```bash
pip install ouroboros-ai              # Base (core engine)
pip install ouroboros-ai[claude]      # + Claude Code runtime deps
pip install ouroboros-ai[litellm]     # + LiteLLM multi-provider support
pip install ouroboros-ai[all]         # Everything (claude + litellm + dashboard)

# Verify CLI
ouroboros --version
```

> **Codex CLI** is an external prerequisite installed separately (`npm install -g @openai/codex`). No Python extras are required -- the base `ouroboros-ai` package is sufficient.

### Option 3: From Source (For Contributors)
```bash
# Clone repository
git clone https://github.com/Q00/ouroboros
cd ouroboros

# Install all dependencies (including dev tools)
uv sync

# Verify CLI
uv run ouroboros --version
```

---

## Configuration

### API Keys
```bash
# Set environment variables
export ANTHROPIC_API_KEY="your-anthropic-key"
# OR
export OPENAI_API_KEY="your-openai-key"

# Verify setup
ouroboros status health
```

### Configuration File
Create `~/.ouroboros/config.yaml`:
```yaml
# Model preferences
providers:
  default: anthropic/claude-3-5-sonnet
  frugal: anthropic/claude-3-haiku
  standard: anthropic/claude-3-5-sonnet
  frontier: anthropic/claude-3-opus

# TUI settings
tui:
  theme: dark
  refresh_rate: 100ms
  show_metrics: true

# Execution settings
execution:
  max_parallel_tasks: 5
  default_mode: standard
  auto_save: true
```

### Environment Variables
```bash
# Terminal customization
export TERM=xterm-256color
export OUROBOROS_THEME=dark

# MCP settings
export OUROBOROS_MCP_HOST=localhost
export OUROBOROS_MCP_PORT=8000
```

---

## Choosing a Runtime Backend

Ouroboros is a specification-first workflow engine that delegates code execution to a **runtime backend**. Two backends are currently supported:

| | Claude Code | Codex CLI |
|---|---|---|
| **Best for** | Teams already using Claude Code; subscription-based usage | OpenAI-ecosystem users; pay-per-token API billing |
| **Billing model** | Claude Code Max Plan (flat subscription) | OpenAI API usage (pay-per-token) |
| **Install** | `pip install ouroboros-ai[claude]` | `pip install ouroboros-ai` (base package) + `npm install -g @openai/codex` |
| **Skill shortcuts** | `ooo` commands inside Claude Code sessions | `ooo` commands via installed Codex rules and skills |
| **Sandbox** | Runs inside Claude Code session | Codex CLI manages its own sandbox |
| **Config value** | `claude` | `codex` |

> **Note:** Both backends execute the same Ouroboros workflow engine -- seeds, interviews, evaluations, and the TUI dashboard work identically. The runtime backend only determines which AI coding agent performs the underlying code generation and tool execution.

### Setting the Runtime Backend

The easiest way to configure your runtime is during initial setup:

```bash
ouroboros setup
# Detects installed runtimes and prompts you to choose one
```

To set or change it manually, edit `~/.ouroboros/config.yaml`:

```yaml
orchestrator:
  runtime_backend: claude   # or: codex
```

Or use the CLI:

```bash
ouroboros config set orchestrator.runtime_backend codex
```

Or set the environment variable (overrides config file):

```bash
export OUROBOROS_RUNTIME_BACKEND=codex
```

Resolution order (highest priority first):

1. `OUROBOROS_RUNTIME_BACKEND` environment variable
2. `orchestrator.runtime_backend` in `~/.ouroboros/config.yaml`
3. Auto-detection during `ouroboros setup`

### Decision Guide

**Choose Claude Code if you:**
- Already have a Claude Code Max Plan subscription
- Want `ooo` skill shortcuts inside Claude Code sessions
- Prefer Anthropic models (Claude Sonnet / Opus)

**Choose Codex CLI if you:**
- Prefer OpenAI models (GPT-5.4 or later)
- Want pay-per-token billing through the OpenAI API
- Are already using the OpenAI ecosystem

For detailed runtime-specific setup, see:
- [Claude Code runtime guide](runtime-guides/claude-code.md)
- [Codex CLI runtime guide](runtime-guides/codex.md)

---

## Your First Workflow: Complete Tutorial

> **Runtime note:** The examples below use `ouroboros` CLI commands (work with any runtime). Claude Code users can substitute `ooo` skill shortcuts inside an active session (e.g., `ooo interview` instead of `ouroboros init`).

### Step 1: Start with an Idea
```bash
ouroboros init "I want to build a personal finance tracker"
# Claude Code alternative: ooo interview "I want to build a personal finance tracker"
```

### Step 2: Answer Clarifying Questions
The interview will ask questions like:
- "What platforms do you want to track?" (Bank accounts, credit cards, investments)
- "Do you need budgeting features?" (Yes, with category tracking)
- "Mobile app or web-based?" (Desktop-only with web export)
- "Data storage preference?" (SQLite, local file)

Continue until the ambiguity score drops below 0.2.

### Step 3: Generate the Seed
```bash
# Create immutable specification
ouroboros seed
# Claude Code alternative: ooo seed
```

This generates a `seed.yaml` file like:
```yaml
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
ontology_schema:
  name: "FinanceTracker"
  fields:
    - name: "transactions"
      type: "array"
      description: "All financial transactions"
metadata:
  ambiguity_score: 0.15
  seed_id: "seed_abc123"
```

### Step 4: Execute with TUI

```bash
ouroboros run workflow finance-tracker.yaml
```

### Step 5: Monitor Progress
Watch the TUI dashboard show:
- Double Diamond phases (Discover → Define → Design → Deliver)
- Task decomposition tree
- Parallel execution batches
- Real-time metrics (tokens, cost, drift)

### Step 6: Evaluate Results
```bash
ouroboros evaluate
# Claude Code alternative: ooo evaluate
```

The evaluation checks:
1. **Mechanical** - Code compiles, tests pass, linting clean
2. **Semantic** - Meets acceptance criteria, aligned with goals
3. **Consensus** - Multi-model validation for critical decisions

---

## Common Workflows

### Workflow 1: New Project from Scratch

```bash
# 1. Clarify requirements
ouroboros init "Build a REST API for a blog"

# 2. Generate specification
ouroboros seed

# 3. Execute with visualization
ouroboros run workflow latest.yaml

# 4. Evaluate results
ouroboros evaluate

# 5. Monitor drift
ouroboros status
```

### Workflow 2: Bug Fixing

```bash
# 1. Analyze the problem
ouroboros init "User registration fails with email validation"

# 2. Generate fix seed
ouroboros seed

# 3. Execute
ouroboros run workflow latest.yaml

# 4. Verify fix
ouroboros evaluate
```

### Workflow 3: Feature Enhancement

```bash
# 1. Plan the enhancement
ouroboros init "Add real-time notifications to the chat app"

# 2. Break into tasks
ouroboros seed

# 3. Execute
ouroboros run workflow latest.yaml

# 4. Review implementation
ouroboros evaluate
```

> **Claude Code users:** Substitute `ooo` skill commands (e.g., `ooo interview`, `ooo seed`, `ooo run`) inside an active Claude Code session for any of the workflows above.

---

## Understanding the TUI Dashboard

The TUI provides real-time visibility into your workflow:

### Main Dashboard View
```
┌──────────────────────────────────────────────────────┐
│  OUROBOROS DASHBOARD                                 │
├──────────────────────────────────────────────────────┤
│  Phase: [*] DESIGN                                   │
│  Progress: 65% [============-------]                 │
│  Cost: $2.34 (85% saved)                             │
│  Drift: 0.12 OK                                     │
├──────────────────────────────────────────────────────┤
│  Task Tree                                          │
│  ├─ [*] Define API endpoints (100%)                  │
│  ├─ [~] Implement auth service (75%)                 │
│  └─ ○ Create database schema (0%)                    │
├──────────────────────────────────────────────────────┤
│  Active Agents: 3/5                                  │
│  ├── executor [Building auth service]                │
│  ├── researcher [Analyzing best practices]           │
│  └── verifier [Waiting results]                      │
└──────────────────────────────────────────────────────┘
```

### Key Components
1. **Phase Indicator** - Shows current Double Diamond phase
2. **Progress Bar** - Overall completion percentage
3. **Metrics Panel** - Cost, drift, and agent status
4. **Task Tree** - Hierarchical view of all tasks
5. **Agent Activity** - Live status of working agents

### Interactive Features
- **Click** on tasks to see details
- **Press Space** to pause/resume execution
- **Press D** to view drift analysis
- **Press C** to see cost breakdown

---

## Troubleshooting

### Installation Issues

#### Claude Code skill not recognized

**Terminal:**
```bash
# Check skill is installed
claude plugin list

# Reinstall if needed
claude plugin install ouroboros@ouroboros --force

# Restart Claude Code
```

#### Python dependency errors
```bash
# Check Python version
python --version  # Must be >= 3.12

# Reinstall with uv
uv sync --all-groups

# Or with pip
pip install --force-reinstall ouroboros-ai
```

### Configuration Issues

#### API key not found
```bash
# Set environment variable
export ANTHROPIC_API_KEY="your-key"

# Or use .env file
echo 'ANTHROPIC_API_KEY=your-key' > ~/.ouroboros/.env

# Verify
ouroboros status health
```

#### MCP server issues
```bash
# Check MCP server info
ouroboros mcp info

# Restart MCP server
ouroboros mcp serve
```

### Execution Issues

#### TUI not displaying
```bash
# Check terminal capabilities
echo $TERM

# Set proper TERM
export TERM=xterm-256color

# Launch TUI monitor in a separate terminal
ouroboros tui monitor
```

#### High costs

Reduce seed scope or use a more cost-efficient model tier. Check execution cost in the TUI dashboard or session status output.

#### Stuck execution

**Terminal:**
```bash
# Check execution status
ouroboros status executions

# Or resume a paused/failed execution
ouroboros run resume
```

**Inside a runtime session (Claude Code):**
```
ooo unstuck
```

### Performance Issues

#### Slow startup
```bash
# Clear cache
rm -rf ~/.ouroboros/cache/

# Check resource usage
ps aux | grep ouroboros

# Reduce parallel tasks
export OUROBOROS_MAX_PARALLEL=2
```

#### Memory issues
```bash
# Reduce parallel tasks
export OUROBOROS_MAX_PARALLEL=2

# Check current configuration
ouroboros config show
```

---

## Best Practices

### For Better Interviews
1. **Be specific** - Instead of "build a social app" say "build a Twitter clone with real-time messaging"
2. **Consider constraints** - Think about budget, timeline, and technical limitations
3. **Define success** - Clear acceptance criteria help generate better specs

### For Effective Seeds
1. **Include non-functional requirements** - Performance, security, scalability
2. **Define boundaries** - What's in scope and what's not
3. **Specify integrations** - APIs, databases, third-party services

### For Successful Execution
1. **Monitor drift** - Check status regularly to catch deviations early
2. **Use evaluation** - Always run evaluation to ensure quality
3. **Iterate with evolve** - Use evolutionary loops to refine specs

---

## Next Steps

### After Your First Project
1. **Explore Modes** - Try different execution modes for various scenarios
2. **Custom Skills** - Create your own skills for repetitive workflows
3. **Team Work** - Use swarm mode for team-based development

### Advanced Topics
1. **Custom Agents** - Define specialized agents for your domain
2. **MCP Integration** - Connect to external tools and services
3. **Event Analysis** - Use replay to learn from past executions

### Community
- [Documentation](https://github.com/Q00/ouroboros/tree/main/docs)
- [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- [Feature Requests](https://github.com/Q00/ouroboros/discussions)

---

## Troubleshooting Reference

| Issue | Solution | Command | Where |
|-------|----------|---------|-------|
| Claude Code skill not loaded | Reinstall skill | `claude plugin install ouroboros@ouroboros` | Terminal |
| CLI not found | Install Python package | `pip install ouroboros-ai` | Terminal |
| API errors | Check API key | `export ANTHROPIC_API_KEY=...` | Terminal |
| TUI blank | Check terminal | `export TERM=xterm-256color` | Terminal |
| High costs | Reduce seed scope | `ooo interview` / `ouroboros init` | Runtime session |
| Execution stuck | Use unstuck | `ooo unstuck` / `ouroboros run resume` | Runtime session |
| Drift detected | Review spec | `ouroboros status executions` | Terminal |

Need more help? Open an issue on [GitHub](https://github.com/Q00/ouroboros/issues).