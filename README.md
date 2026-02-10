<p align="center">
  <br/>
  <img src="https://raw.githubusercontent.com/Q00/ouroboros/main/docs/screenshots/dashboard.png" width="600" alt="Ouroboros TUI Dashboard">
  <br/>
  <strong>OUROBOROS</strong>
  <br/>
  <em>The Serpent That Eats Itself — Better Every Loop</em>
  <br/>
</p>

<p align="center">
  <strong>Stop prompting. Start specifying.</strong>
  <br/>
  <sub>🚀 The Visual AI Workflow Engine that transforms vague ideas into validated specifications — before writing a single line of code</sub>
</p>

<p align="center">
  <!-- Version & Status -->
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI Version"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="https://codecov.io/gh/Q00/ouroboros"><img src="https://img.shields.io/codecov/c/github/Q00/ouroboros/main" alt="Coverage"></a>
  <!-- Python -->
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.14+-blue" alt="Python"></a>
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/pyversions/ouroboros-ai" alt="Python Versions"></a>
  <!-- Performance -->
  <a href="#"><img src="https://img.shields.io/badge/cost-85%25%20savings-green" alt="Cost Savings"></a>
  <!-- Legal & Social -->
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://github.com/Q00/ouroboros/stargazers"><img src="https://img.shields.io/github/stars/Q00/ouroboros?style=social" alt="Stars"></a>
  <!-- Discord -->
  <a href="https://discord.gg/ouroboros"><img src="https://img.shields.io/discord/123456789012345678.svg?logo=discord&label=Discord&color=7289da" alt="Discord"></a>
  <!-- Architecture -->
  <a href="#architecture"><img src="https://img.shields.io/badge/architecture-event%20sourcing-purple" alt="Architecture"></a>
  <!-- Plugin -->
  <a href="https://github.com/claude-code/plugins"><img src="https://img.shields.io/badge/claude%20code-plugin-orange" alt="Claude Code Plugin"></a>
</p>

<p align="center">
  <a href="https://github.com/Q00/ouroboros">
    <img src="https://img.shields.io/badge/Star%20Us%20%E2%AD%90-GitHub-blue?style=for-the-badge&logo=github" alt="Star on GitHub">
  </a>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-why-ouroboros">Why Ouroboros?</a> •
  <a href="#-commands">Commands</a> •
  <a href="#-features">Features</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-comparison">vs OMC</a> •
  <a href="#-contributing">Contributing</a>
</p>

### Plugin Mode Issues

#### `ooo: command not found`
**Solution:**
```bash
# Reinstall plugin
claude /plugin marketplace add github:Q00/ouroboros
claude /plugin install ouroboros@ouroboros

# Verify installation
claude /plugin list
```

#### Interview won't start
**Solution:**
```bash
# Check skill files
ls .claude-plugin/skills/

# Try clearer prompt
ooo interview "Build a web scraper for news sites"
```

### Full Mode Issues

#### `ouroboros: command not found`
**Solution:**
```bash
# Check Python version
python --version  # Must be 3.14+

# Install with uv
uv sync
# Or with pip
pip install ouroboros-ai
```

#### TUI not displaying
**Solution:**
```bash
# Ensure terminal supports TUI
export TERM=xterm-256color

# Run with CLI fallback
ouroboros run --seed project.yaml --ui cli
```

#### High cost warnings
**Solution:**
```bash
# Use ecomode
ouroboros run --seed project.yaml --mode ecomode

# Check cost predictions
ouroboros run --seed project.yaml --cost-predict
```

### Common Issues

**"Ambiguity not decreasing"**
- Provide more specific answers in interview
- Break large ideas into smaller components
- Use `ooo unstuck` for fresh perspectives

**"Execution stalled"**
- Ouroboros auto-detects stagnation and switches personas
- Check logs: `ouroboros status --events`
- Try different execution mode

**"Evaluation failed"**
- Verify Seed is valid YAML
- Check all required fields are present
- Review acceptance criteria completeness

### Getting Help
- 📚 [Full Documentation](docs/)
- 💬 [Discord Community](https://discord.gg/ouroboros)
- 🐛 [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- 🎯 [Examples Gallery](playground/examples/)

---

## ◈ Contributing

### Development Setup
```bash
# Clone and setup
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups

# Run tests
uv run pytest

# Type check
uv run mypy
```

### Code Style
- Follow PEP 8 with black formatting
- Use type annotations everywhere
- Write comprehensive docstrings
- Add tests for new features

### Documentation
- Update README.md for new features
- Add examples to `playground/examples/`
- Document new modes in `docs/modes/`
- Update comparison tables

---

## ◈ Roadmap

### v1.0 (Current)
- ✅ Core TUI dashboard
- ✅ Socratic interview system
- ✅ Seed specifications
- ✅ 7 execution modes
- ✅ 3-stage evaluation
- ✅ PAL Router

### v1.1 (Next)
- 🔄 Skill marketplace
- 🔄 Performance profiling
- 🔄 Session replay
- 🔄 Advanced metrics

### v1.2 (Future)
- 🆕 Visual workflow builder
- 🆕 Advanced analytics
- 🆕 Multi-project coordination
- 🆕 Enterprise features

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <a href="docs/getting-started.md">Getting Started</a> •
  <a href="docs/architecture.md">Architecture</a> •
  <a href="docs/skills.md">Skills Development</a> •
  <a href="docs/compare-alternatives.md">Feature Comparison</a>
  <br/><br/>
  <strong>MIT License</strong> •
  <a href="https://github.com/Q00/ouroboros/graphs/contributors">Contributors</a> •
  <a href="https://discord.gg/ourorboros">Discord</a>
</p>

### 🚀 Quick Start in 3 Commands

**Plugin Mode** (No Python Required):
```bash
# 1. Install
claude /plugin marketplace add github:Q00/ouroboros
claude /plugin install ouroboros@ouroboros

# 2. Interview
ooo interview "I want to build a task management CLI"

# 3. Generate Spec
ooo seed
```

**Full Mode** (Python 3.14+):
```bash
# 1. Setup
uv sync && ouroboros setup

# 2. Execute
ouroboros run --seed project.yaml --parallel

# 3. Evaluate
ouroboros evaluate
```

> **Success**: In < 5 minutes, you've transformed a vague idea into a validated specification ready for AI execution with 85% cost optimization.

<details>
<summary><strong>What just happened?</strong></summary>

1. `ooo interview` — Socratic questioning exposed your hidden assumptions and contradictions
2. `ooo seed` — Crystallized answers into an immutable specification (the "Seed")
3. The Seed is what you hand to AI — no more "build me X" and hoping for the best

</details>

---

## 📸 Screenshots

See Ouroboros in action:

| Dashboard | Interview | Execution | Evaluation |
|-----------|-----------|-----------|------------|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Interview](docs/screenshots/interview.png) | ![Execution](docs/screenshots/seed.png) | ![Evaluation](docs/screenshots/evaluate.png) |

---

### ⚡ Full Mode (Python 3.14+ Required)

Unlock execution, evaluation, and drift tracking:

```bash
# Setup (one-time)
ooo setup       # register MCP server

# Core commands
ooo run         # execute seed via Double Diamond decomposition
ooo evaluate    # 3-stage verification (Mechanical → Semantic → Consensus)
ooo status      # drift detection + session tracking
```

**Verify Installation:**

```bash
# Check ouroboros CLI is available
ouroboros --version

# Expected output: ouroboros v0.x.x
# If you see "command not found", ensure Python 3.14+ is installed and uv sync was run

# Check health status
ouroboros status health

# Expected output: System status including API key, database, and MCP server status
# Any issues will be flagged with specific error messages
```

> **[Full Guide](docs/running-with-claude-code.md)** | **[CLI Reference](docs/cli-reference.md)**

---

## ◈ Why Ouroboros?

> *"I can already prompt Claude directly. Why do I need this?"*

### The Problem: Garbage In, Garbage Out

Human requirements arrive **ambiguous**, **incomplete**, and **contradictory**. When AI executes them directly:

```
┌─────────────────────────────────────────────────────────────┐
│  BEFORE: The "Build Me X" Loop                             │
├─────────────────────────────────────────────────────────────┤
│  You: "Build me a task management CLI"                    │
│         ↓                                                  │
│  Claude builds something                                   │
│         ↓                                                  │
│  You realize it's wrong (forgot about priorities)          │
│         ↓                                                  │
│  Rewrite prompt → Claude rebuilds → Still wrong            │
│         ↓                                                  │
│  3 hours later, debugging requirements, not code           │
└─────────────────────────────────────────────────────────────┘
```

### The Solution: Specify Before You Build

Ouroboros applies two ancient methods to expose hidden assumptions **before** AI writes a single line of code:

```
┌─────────────────────────────────────────────────────────────┐
│  AFTER: The Ouroboros Way                                  │
├─────────────────────────────────────────────────────────────┤
│  Q: "Should completed tasks be deletable or archived?"     │
│  Q: "What happens when two tasks have the same priority?"  │
│  Q: "Is this for teams or solo use?"                       │
│         ↓                                                  │
│  → 12 hidden assumptions exposed                          │
│  → Seed generated. Ambiguity: 0.15                         │
│  → Claude builds exactly what you specified. First try.    │
└─────────────────────────────────────────────────────────────┘
```

### Core Benefits

| Problem | Ouroboros Solution |
|:--------|:-------------------|
| 🎯 **Vague requirements → wrong output** | Socratic interview exposes hidden assumptions before coding begins |
| 💰 **Most expensive model for everything** | PAL Router: **85% cost reduction** via automatic tier selection |
| 📍 **No idea if you're still on track** | Drift detection flags when execution diverges from spec |
| 🔄 **Stuck → retry the same approach harder** | 5 lateral thinking personas offer fresh angles |
| ✅ **Did we actually build the right thing?** | 3-stage evaluation (Mechanical → Semantic → Consensus) |

---

## ◈ Features

### 🎨 Visual Workflow Engine
- **Rich TUI Dashboard** - Real-time visualization of phases, AC tree, and parallel execution
- **Live Progress Tracking** - Watch tasks decompose and execute with visual indicators
- **Interactive Debugging** - Inspect execution state, drift metrics, and agent activity
- **Session Replay** - Full event sourcing for debugging and analysis
- **Screenshot Gallery** - [View screenshots](docs/screenshots/) of the TUI in action

### 🎯 Specification-First Approach
- **Socratic Interview** - Ontological questioning reveals hidden assumptions before coding
- **Immutable Seed Specs** - "Constitution" for your workflows prevents scope creep
- **Acceptance Criteria Tree** - Recursive decomposition with MECE principle
- **Drift Detection** - Real-time alerts when execution diverges from specification

### 💰 Intelligent Cost Optimization
- **PAL Router** - Progressive Adaptive LLM routing with 85% cost reduction
- **Cost Prediction** - Preview costs before execution with tier breakdown
- **Model Tier Selection** - Automatic: Frugal (1x) → Standard (10x) → Frontier (30x)
- **Performance Analytics** - Token usage and cost tracking per phase
- **Cost Dashboard** - Real-time cost tracking in TUI

### 🚀 7 Execution Modes
- **Autopilot** - Full autonomous execution with verification
- **Ultrawork** - Maximum parallelism with dependency analysis
- **Ralph** - "The boulder never stops" persistence mode
- **Ultrapilot** - Parallel execution with file ownership
- **Ecomode** - Cost-optimized execution (haiku/sonnet only)
- **Swarm** - Coordinated multi-agent teams
- **Pipeline** - Sequential agent chaining

### 🔧 Professional Developer Experience
- **VS Code Integration** - Seamless extension for TUI and monitoring
- **MCP Server** - Bidirectional tool integration with 100+ model support
- **Event Sourcing** - Full audit trail with replay capability
- **Hot Reload** - Live skill updates without restart
- **Comprehensive Testing** - 1,341 tests with 97%+ coverage

---

## ◈ Ouroboros vs oh-my-claudecode (OMC)

| Dimension | Ouroboros | oh-my-claudecode (OMC) |
|-----------|-----------|------------------------|
| **Primary Interface** | 🎨 **TUI-First** + CLI | CLI-only |
| **Core Philosophy** | 🎯 **Specification-first** (Seed specs) | ⚡ **Execution-first** (skills/agents) |
| **State Management** | 💾 **Event sourcing** with replay | 💾 State files per mode |
| **Cost Optimization** | 💸 **PAL Router (85% savings)** | 💸 Smart model routing |
| **Visualization** | 📊 **Rich dashboard** with graphs | 📝 None (CLI output only) |
| **Requirements** | 🧠 Socratic interview + ontology | ❌ No requirement process |
| **Evaluation** | ✅ **3-stage pipeline** (→ Consensus) | 🔁 UltraQA cycles |
| **Plugin System** | 🔌 Claude Code plugin | 📂 Skills/hooks/agents |
| **Modes** | 7 + hybrid composition | 7 execution modes |
| **Parallelism** | 🔄 **Dependency-aware** topological sort | 🔀 Independent task spawning |
| **Persistence** | 🗃️ Full session replay | 📄 Session resume only |

### 🏆 Key Advantages

| Feature | Ouroboros | Impact |
|---------|-----------|--------|
| **TUI Dashboard** | Visual workflow tracking | **See execution progress** |
| **Seed Specifications** | Immutable requirements | **Build the right thing** |
| **PAL Router** | Tier-based optimization | **85% cost reduction** |
| **Event Sourcing** | Full replay capability | **Debuggable sessions** |
| **3-Stage Evaluation** | Mechanical → Semantic → Consensus | **Quality assurance** |

### 🎯 Why Choose Ouroboros

**Choose Ouroboros when:**
- Starting **new projects** with unclear requirements
- Need **visual workflow tracking** and real-time feedback
- Want **cost-effective AI development** with transparent pricing
- Require **quality assurance** and validation pipelines
- Prefer **structured specification-first** approach

**Choose OMC when:**
- Working with **existing codebases** and legacy systems
- Prefer **CLI-only** workflows and minimal UI
- Need maximum **agent variety** (32+ agents)
- Want minimal setup overhead
- Working with **complex multi-agent coordination**

### 🔄 Migration Path from OMC

```bash
# 1. Install Ouroboros alongside OMC
claude /plugin marketplace add github:Q00/ouroboros
claude /plugin install ouroboros@ouroboros

# 2. Convert existing workflows
# OMC: /oh-my-claudecode:autopilot
# Ouroboros: ooo interview → ooo run

# 3. Use existing OMC agents
# Agent: ouroboros:executor (replaces OMC executor)
# Skill: ouroboros:unblock (replaces OMC unstuck)
```

---

## ◈ Commands

### Command Catalog

| Command | Description | Mode |
|:--------|:------------|:----:|
| **Specification** |||
| `ooo interview` | Socratic questioning → expose hidden assumptions | Plugin |
| `ooo seed` | Crystallize answers into immutable spec | Plugin |
| **Resilience** |||
| `ooo unstuck` | 5 lateral thinking personas when you're stuck | Plugin |
| **Execution** |||
| `ooo run` | Execute seed via Double Diamond decomposition | Full |
| `ooo evaluate` | 3-stage verification (Mechanical → Semantic → Consensus) | Full |
| `ooo status` | Drift detection + session tracking | Full |
| **TUI Interface** |||
| `ouroboros dashboard` | Interactive TUI dashboard | Full |
| `ouroboros monitor` | Live execution visualization | Full |
| **Cost Control** |||
| `ouroboros run --eco` | Ecomode: 85% cost reduction | Full |
| `ouroboros predict` | Cost prediction before execution | Full |
| **Advanced** |||
| `ooo setup` | Register MCP server for Full Mode | Full |
| `ouroboros replay` | Replay and debug sessions | Full |
| `ooo help` | Full command reference | Plugin |

### Natural Language Triggers

You can also use natural language — these work identically:

| Instead of... | Say... |
|:-------------|:-------|
| `ooo unstuck` | "I'm stuck" / "Help me think differently" |
| `ooo interview` | "Clarify requirements" / "Explore this idea" |
| `ooo evaluate` | "Check if this works" / "Verify the implementation" |
| `ooo status` | "Where are we?" / "Show current progress" |

---

## ◈ How It Works

Human requirements arrive **ambiguous**, **incomplete**, and **contradictory**. If AI executes them directly — Garbage In, Garbage Out.

Ouroboros applies two ancient methods to fix this:

- **Socratic Questioning** — *"Why do you want this? Is that truly necessary?"* → reveals hidden assumptions
- **Ontological Analysis** — *"What IS this, really? Symptom or root cause?"* → finds the essential problem

These iterate until a **Seed** crystallizes — a spec with `Ambiguity ≤ 0.2`. Only then does execution begin.

### The Pipeline

```
  Interview → Seed → Route → Execute → Evaluate → Adapt
  (Phase 0)   (0)    (1)     (2)        (4)       (3,5)
```

| Phase | What It Does |
|:-----:|-------------|
| **0 — Big Bang** | Socratic + Ontological questioning → crystallized Seed |
| **1 — PAL Router** | Auto-selects model tier: 1x / 10x / 30x → **~85% cost savings** |
| **2 — Double Diamond** | Discover → Define → Design → Deliver |
| **3 — Resilience** | Stagnation? Switch to one of 5 lateral thinking personas |
| **4 — Evaluation** | Mechanical ($0) → Semantic ($$) → Consensus ($$$$) |
| **5 — Secondary Loop** | TODO registry: defer the trivial, pursue the essential |

---

## ◈ Architecture

<details>
<summary><code>75 modules</code> · <code>1,341 tests</code> · <code>97%+ coverage</code></summary>

```
src/ouroboros/
├── core/           ◆ Types, errors, seed, ontology
├── bigbang/        ◇ Phase 0: Interview → Seed
├── routing/        ◇ Phase 1: PAL router, tiers
├── execution/      ◇ Phase 2: Double Diamond
├── resilience/     ◇ Phase 3: Lateral thinking
├── evaluation/     ◇ Phase 4: 3-stage evaluation
├── secondary/      ◇ Phase 5: TODO registry
├── orchestrator/   ★ Claude Agent SDK integration
├── observability/  ○ Drift control, retrospective
├── persistence/    ○ Event sourcing, checkpoints
├── providers/      ○ LiteLLM adapter (100+ models)
└── cli/            ○ Command-line interface
```

</details>

---

## ◈ Troubleshooting

### Plugin Mode Issues

**`ooo: command not found`**

- The plugin isn't installed. Run: `claude /plugin marketplace add github:Q00/ouroboros`
- Then: `claude /plugin install ouroboros@ouroboros`
- Restart Claude Code after installation

**`ooo help` shows unexpected output**

- Check plugin is loaded: `claude /plugin list`
- If ouroboros isn't listed, re-run the install commands above
- Report issues at [GitHub Issues](https://github.com/Q00/ouroboros/issues)

### Full Mode Issues

**`ouroboros: command not found`**

- Ensure Python 3.14+ is installed: `python --version`
- Run: `uv sync` (from the ouroboros directory)
- Or install globally: `pip install ouroboros-ai`

**`ouroboros --version` fails**

- Check Python version: `python --version` (must be 3.14+)
- Verify dependencies: `uv sync`
- Check for conflicts: `uv run ouroboros --version`

**`ouroboros status health` shows errors**

- Missing API key: Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` environment variable
- Database error: Run `ouroboros config init` to initialize
- MCP server issue: Run `ooo setup` to register the MCP server

> **Need more help?** See [Getting Started](docs/getting-started.md#troubleshooting) for detailed troubleshooting.

---

## ◈ Contributing

```bash
uv sync --all-groups && uv run pytest   # Setup + test
```

- **Issues**: [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Q00/ouroboros/discussions)

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <a href="docs/getting-started.md">Getting Started</a> · <a href="docs/cli-reference.md">CLI Reference</a>
  <br/><br/>
  <code>MIT License</code>
</p>

---

<p align="center">
  <strong>Found Ouroboros useful?</strong>
  <br/>
  <a href="https://github.com/Q00/ouroboros">
    <img src="https://img.shields.io/badge/Star%20on%20GitHub-black?style=for-the-badge&logo=github" alt="Star on GitHub">
  </a>
  <br/>
  <em>Stars help others discover Ouroboros and support our development</em>
</p>
