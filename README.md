<p align="center">
  <br/>
  ◯ ─────────── ◯
  <br/><br/>
  <img src="./docs/images/ouroboros.png" width="520" alt="Ouroboros">
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
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#the-problem-everyone-ignores">Why</a> ·
  <a href="#from-wonder-to-ontology">Philosophy</a> ·
  <a href="#the-loop">How</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#the-nine-minds">Agents</a>
</p>

---

> *AI can build anything. The hard part is knowing what to build.*

Ouroboros is a **specification-first AI development system**. It applies Socratic questioning and ontological analysis to expose your hidden assumptions — before a single line of code is written.

Most AI coding fails at the **input**, not the output. The bottleneck isn't AI capability. It's human clarity. Ouroboros fixes the human, not the machine.

---

## From Wonder to Ontology

> *Wonder → "How should I live?" → "What IS 'live'?" → Ontology*
> — Socrates

This is the philosophical engine behind Ouroboros. Every great question leads to a deeper question — and that deeper question is always **ontological**: not *"how do I do this?"* but *"what IS this, really?"*

```
   Wonder                          Ontology
     💡                               🔬
"What do I want?"    →    "What IS the thing I want?"
"Build a task CLI"   →    "What IS a task? What IS priority?"
"Fix the auth bug"   →    "Is this the root cause, or a symptom?"
```

This is not abstraction for its own sake. When you answer *"What IS a task?"* — deletable or archivable? solo or team? — you eliminate an entire class of rework. **The ontological question is the most practical question.**

Ouroboros embeds this into its architecture through the **Double Diamond**:

```
    ◇ Wonder          ◇ Design
   ╱  (diverge)      ╱  (diverge)
  ╱    explore      ╱    create
 ╱                 ╱
◆ ──────────── ◆ ──────────── ◆
 ╲                 ╲
  ╲    define       ╲    deliver
   ╲  (converge)     ╲  (converge)
    ◇ Ontology        ◇ Evaluation
```

The first diamond is **Socratic**: diverge into questions, converge into ontological clarity. The second diamond is **pragmatic**: diverge into design options, converge into verified delivery. Each diamond requires the one before it — you cannot design what you haven't understood.

---

## Quick Start

```bash
# Install
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros

# One-time setup
ooo setup

# Question everything
ooo interview "I want to build a task management CLI"
```

<details>
<summary><strong>What just happened?</strong></summary>

```
ooo interview  →  Socratic questioning exposed 12 hidden assumptions
ooo seed       →  Crystallized answers into an immutable spec (Ambiguity: 0.15)
ooo run        →  Executed via Double Diamond decomposition
ooo evaluate   →  3-stage verification: Mechanical → Semantic → Consensus
```

The serpent completed one loop. Each loop, it knows more than the last.

</details>

---

## The Problem Everyone Ignores

```
You: "Build me a task management CLI"
                    ↓
          Claude builds something
                    ↓
     "Wait — I forgot about priorities"
                    ↓
        Rewrite prompt → rebuild
                    ↓
     3 hours later: debugging requirements, not code
```

This isn't an AI problem. It's a **clarity** problem.

> *"Should completed tasks be deletable or archived?"*
> *"What happens when two tasks have the same priority?"*
> *"Is this for teams or solo use?"*

You didn't know what you wanted. Neither did the AI.

**Ouroboros asks these questions first.** Not after the build fails — before it begins.

---

## The Loop

The ouroboros — a serpent devouring its own tail — isn't decoration. It IS the architecture:

```
    Interview → Seed → Execute → Evaluate
        ↑                           ↓
        └──── Evolutionary Loop ────┘
```

Each cycle doesn't repeat — it **evolves**. The output of evaluation feeds back as input for the next generation, until the system truly knows what it's building.

| Phase | What Happens |
|:------|:-------------|
| **Interview** | Socratic questioning exposes hidden assumptions |
| **Seed** | Answers crystallize into an immutable specification |
| **Execute** | Double Diamond: Discover → Define → Design → Deliver |
| **Evaluate** | 3-stage gate: Mechanical ($0) → Semantic → Multi-Model Consensus |
| **Evolve** | Wonder *("What do we still not know?")* → Reflect → next generation |

> *"This is where the Ouroboros eats its tail: the output of evaluation*
> *becomes the input for the next generation's seed specification."*
> — `reflect.py`

Convergence is reached when ontology similarity ≥ 0.95 — when the system has questioned itself into clarity.

### Ralph: The Loop That Never Stops

`ooo ralph` runs the evolutionary loop persistently — across session boundaries — until convergence is reached. Each step is **stateless**: the EventStore reconstructs the full lineage, so even if your machine restarts, the serpent picks up where it left off.

```
Ralph Cycle 1: evolve_step(lineage, seed) → Gen 1 → action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       → Gen 2 → action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       → Gen 3 → action=CONVERGED ✓
                                                └── Ralph stops.
                                                    The ontology has stabilized.
```

> *"The boulder never stops."*

---

## Commands

> Run `ooo setup` first after installation. All commands require it.

| Command | What It Does |
|:--------|:-------------|
| `ooo setup` | Register MCP server (one-time) |
| `ooo interview` | Socratic questioning → expose hidden assumptions |
| `ooo seed` | Crystallize into immutable spec |
| `ooo run` | Execute via Double Diamond decomposition |
| `ooo evaluate` | 3-stage verification gate |
| `ooo evolve` | Evolutionary loop until ontology converges |
| `ooo unstuck` | 5 lateral thinking personas when you're stuck |
| `ooo status` | Drift detection + session tracking |
| `ooo ralph` | Persistent loop until verified |
| `ooo tutorial` | Interactive hands-on learning |
| `ooo help` | Full reference |

You can also just say what you mean:

| Instead of... | Say... |
|:--------------|:-------|
| `ooo interview` | *"Clarify requirements"* / *"Explore this idea"* |
| `ooo unstuck` | *"I'm stuck"* / *"Help me think differently"* |
| `ooo evaluate` | *"Check if this works"* |
| `ooo status` | *"Where are we?"* |

---

## The Nine Minds

Nine agents, each a different mode of thinking. Loaded on-demand, never preloaded:

| Agent | Role | Core Question |
|:------|:-----|:--------------|
| **Socratic Interviewer** | Questions-only. Never builds. | *"What are you assuming?"* |
| **Ontologist** | Finds essence, not symptoms | *"What IS this, really?"* |
| **Seed Architect** | Crystallizes specs from dialogue | *"Is this complete and unambiguous?"* |
| **Evaluator** | 3-stage verification | *"Did we build the right thing?"* |
| **Contrarian** | Challenges every assumption | *"What if the opposite were true?"* |
| **Hacker** | Finds unconventional paths | *"What constraints are actually real?"* |
| **Simplifier** | Removes complexity | *"What's the simplest thing that could work?"* |
| **Researcher** | Stops coding, starts investigating | *"What evidence do we actually have?"* |
| **Architect** | Identifies structural causes | *"If we started over, would we build it this way?"* |

---

## Under the Hood

<details>
<summary><strong>18 packages · 166 modules · 95 test files · Python 3.14+</strong></summary>

```
src/ouroboros/
├── bigbang/        Interview, ambiguity scoring, brownfield explorer
├── routing/        PAL Router — 3-tier cost optimization (1x / 10x / 30x)
├── execution/      Double Diamond, hierarchical AC decomposition
├── evaluation/     Mechanical → Semantic → Multi-Model Consensus
├── evolution/      Wonder / Reflect cycle, convergence detection
├── resilience/     4-pattern stagnation detection, 5 lateral personas
├── observability/  3-component drift measurement, auto-retrospective
├── persistence/    Event sourcing (SQLAlchemy + aiosqlite), checkpoints
├── orchestrator/   Claude Agent SDK integration, session management
├── core/           Types, errors, seed, ontology, security
├── providers/      LiteLLM adapter (100+ models)
├── mcp/            MCP client/server for Claude Code
├── plugin/         Claude Code plugin system
├── tui/            Terminal UI dashboard
└── cli/            Typer-based CLI
```

**Key internals:**
- **PAL Router** — Frugal (1x) → Standard (10x) → Frontier (30x) with auto-escalation on failure, auto-downgrade on success
- **Drift** — Goal (50%) + Constraint (30%) + Ontology (20%) weighted measurement, threshold ≤ 0.3
- **Brownfield** — Scans 15 config file types across 12+ language ecosystems
- **Evolution** — Up to 30 generations, convergence at ontology similarity ≥ 0.95
- **Stagnation** — Detects spinning, oscillation, no-drift, and diminishing returns patterns

</details>

---

## Contributing

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

[Issues](https://github.com/Q00/ouroboros/issues) · [Discussions](https://github.com/Q00/ouroboros/discussions)

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <strong>The serpent doesn't repeat — it evolves.</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
