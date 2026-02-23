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
  <sub>Transform vague ideas into validated specifications — before writing a single line of code</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI Version"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.14+-blue" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://github.com/Q00/ouroboros/stargazers"><img src="https://img.shields.io/github/stars/Q00/ouroboros?style=social" alt="Stars"></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> &middot;
  <a href="#-why-ouroboros">Why Ouroboros?</a> &middot;
  <a href="#-how-it-works">How It Works</a> &middot;
  <a href="#-commands">Commands</a> &middot;
  <a href="#-architecture">Architecture</a>
</p>

---

## Quick Start

```bash
# 1. Install plugin
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros

# 2. Setup (required — do this first!)
ooo setup

# 3. Interview — expose hidden assumptions
ooo interview "I want to build a task management CLI"

# 4. Generate Seed spec
ooo seed

# 5. Execute and evaluate
ooo run
ooo evaluate
```

> **`ooo setup` is required after installation.** It registers the MCP server
> that powers execution, evaluation, and drift tracking. Without it, other
> commands will redirect you back to setup.

<details>
<summary><strong>What just happened?</strong></summary>

1. `ooo setup` — Registered the Ouroboros MCP server (one-time, ~1 minute)
2. `ooo interview` — Socratic questioning exposed your hidden assumptions and contradictions
3. `ooo seed` — Crystallized answers into an immutable specification (the "Seed")
4. `ooo run` — Executed the seed with visual TUI dashboard
5. `ooo evaluate` — 3-stage verification (Mechanical → Semantic → Consensus)

</details>

---

## Why Ouroboros?

> *"I can already prompt Claude directly. Why do I need this?"*

### The Problem: Garbage In, Garbage Out

Human requirements arrive **ambiguous**, **incomplete**, and **contradictory**. When AI executes them directly:

```
You: "Build me a task management CLI"
      ↓
Claude builds something
      ↓
You realize it's wrong (forgot about priorities)
      ↓
Rewrite prompt → Claude rebuilds → Still wrong
      ↓
3 hours later, debugging requirements, not code
```

### The Solution: Specify Before You Build

Ouroboros exposes hidden assumptions **before** AI writes a single line of code:

```
Q: "Should completed tasks be deletable or archived?"
Q: "What happens when two tasks have the same priority?"
Q: "Is this for teams or solo use?"
      ↓
→ 12 hidden assumptions exposed
→ Seed generated. Ambiguity: 0.15
→ Claude builds exactly what you specified. First try.
```

### Core Benefits

| Problem | Ouroboros Solution |
|:--------|:-------------------|
| Vague requirements → wrong output | Socratic interview exposes hidden assumptions before coding begins |
| Most expensive model for everything | PAL Router: **85% cost reduction** via automatic tier selection |
| No idea if you're still on track | Drift detection flags when execution diverges from spec |
| Stuck → retry the same approach harder | 5 lateral thinking personas offer fresh angles |
| Did we actually build the right thing? | 3-stage evaluation (Mechanical → Semantic → Consensus) |

---

## How It Works

Ouroboros applies two ancient methods to transform messy human intent into precise specifications:

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

## Commands

> Run `ooo setup` first after installing the plugin. All commands require it.

| Command | Description |
|:--------|:------------|
| `ooo setup` | **Run this first** — register MCP server (one-time) |
| `ooo interview` | Socratic questioning → expose hidden assumptions |
| `ooo seed` | Crystallize answers into immutable spec |
| `ooo run` | Execute seed via Double Diamond decomposition |
| `ooo evaluate` | 3-stage verification (Mechanical → Semantic → Consensus) |
| `ooo unstuck` | 5 lateral thinking personas when you're stuck |
| `ooo status` | Drift detection + session tracking |
| `ooo evolve` | Evolutionary loop until ontology converges |
| `ooo ralph` | Persistent loop until verified ("don't stop") |
| `ooo tutorial` | Interactive hands-on learning |
| `ooo help` | Full command reference |

### Natural Language Triggers

You can also use natural language — these work identically:

| Instead of... | Say... |
|:-------------|:-------|
| `ooo interview` | "Clarify requirements" / "Explore this idea" |
| `ooo unstuck` | "I'm stuck" / "Help me think differently" |
| `ooo evaluate` | "Check if this works" / "Verify the implementation" |
| `ooo status` | "Where are we?" / "Show current progress" |

---

## Architecture

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

## Troubleshooting

**`ooo: command not found`**
- Reinstall: `claude plugin marketplace add Q00/ouroboros`
- Then: `claude plugin install ouroboros@ouroboros`
- Restart Claude Code after installation
- Run `ooo setup` after installation

**Commands redirect to setup**
- This means MCP is not registered yet. Run `ooo setup` to fix it.

**`ouroboros: command not found`** (CLI mode)
- Ensure Python 3.14+ is installed: `python --version`
- Run `uv sync` from the ouroboros directory
- Or install globally: `pip install ouroboros-ai`

**`ouroboros status health` shows errors**
- Missing API key: Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
- Database error: Run `ouroboros config init`
- MCP server issue: Run `ooo setup`

### Common Issues

**"Ambiguity not decreasing"** — Provide more specific answers in interview, or use `ooo unstuck` for fresh perspectives.

**"Execution stalled"** — Ouroboros auto-detects stagnation and switches personas. Check logs with `ouroboros status --events`.

---

## Contributing

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

- [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- [GitHub Discussions](https://github.com/Q00/ouroboros/discussions)

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <code>MIT License</code>
</p>
