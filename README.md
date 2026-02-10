<p align="center">
  <br/>
  ◯ ─────────── ◯
  <br/><br/>
  <strong>O U R O B O R O S</strong>
  <br/><br/>
  ◯ ─────────── ◯
  <br/>
</p>

<p align="center">
  <strong>Stop prompting. Start specifying.</strong>
  <br/>
  <sub>A Claude Code plugin that turns vague ideas into validated specs — before AI writes a single line of code.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.14+-blue" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-why-ouroboros">Why?</a> •
  <a href="#-commands">Commands</a> •
  <a href="#-how-it-works">How It Works</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## ◈ Quick Start

```bash
# Install (2 commands, no Python needed)
claude /plugin marketplace add github:Q00/ouroboros
claude /plugin install ouroboros@ouroboros
```

```bash
# Use (2 commands, that's it)
ooo interview "I want to build a task management CLI"
ooo seed
```

**Done.** You now have a validated spec with ambiguity scored below 0.2 — ready for AI to execute.

<details>
<summary><strong>What just happened?</strong></summary>

1. `ooo interview` — Socratic questioning exposed your hidden assumptions and contradictions
2. `ooo seed` — Crystallized answers into an immutable specification (the "Seed")
3. The Seed is what you hand to AI — no more "build me X" and hoping for the best

</details>

### Want more? Enable Full Mode

Full Mode adds execution, evaluation, and drift tracking. Requires Python 3.14+:

```bash
ooo setup       # register MCP server
ooo run         # execute via Double Diamond decomposition
ooo evaluate    # 3-stage verification (Mechanical → Semantic → Consensus)
```

> **[Full Guide](docs/running-with-claude-code.md)** | **[CLI Reference](docs/cli-reference.md)**

---

## ◈ Why Ouroboros?

> *"I can already prompt Claude directly. Why do I need this?"*

**Before** — You say "build me a task management CLI":
```
Claude builds something. You realize it's wrong.
You rewrite the prompt. Claude rebuilds. Still wrong.
3 hours later, you're debugging requirements, not code.
```

**After** — Ouroboros interviews you first:
```
Q: "Should completed tasks be deletable or archived?"
Q: "What happens when two tasks have the same priority?"
Q: "Is this for teams or solo use?"
→ 12 hidden assumptions exposed. Seed generated. Ambiguity: 0.15
→ Claude builds exactly what you specified. First try.
```

| Problem | How Ouroboros Solves It |
|---------|----------------------|
| Vague requirements → wrong output | Socratic interview exposes hidden assumptions |
| Most expensive model for everything | PAL Router: **85% cost reduction** via automatic tier selection |
| No idea if you're still on track | Drift detection flags when execution diverges from spec |
| Stuck → retry the same approach harder | 5 lateral thinking personas offer fresh angles |

---

## ◈ Commands

| Command | Description | Mode |
|---------|-------------|:----:|
| `ooo interview` | Socratic questioning → expose hidden assumptions | Plugin |
| `ooo seed` | Crystallize answers into immutable spec | Plugin |
| `ooo unstuck` | 5 lateral thinking personas when you're stuck | Plugin |
| `ooo run` | Execute seed via Double Diamond decomposition | Full |
| `ooo evaluate` | 3-stage verification (Mechanical → Semantic → Consensus) | Full |
| `ooo status` | Drift detection + session tracking | Full |
| `ooo help` | Full command reference | Plugin |

Natural language also works — say "i'm stuck" instead of `ooo unstuck`, or "clarify requirements" instead of `ooo interview`.

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
