<p align="right">
  <strong>English</strong> | <a href="./README.ko.md">한국어</a>
</p>

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

**Step 1 — Install the plugin** (in your terminal):
```bash
claude plugin marketplace add Q00/ouroboros
claude plugin install ouroboros@ouroboros
```

**Step 2 — Run setup** (inside a Claude Code session):
```
# Start Claude Code, then type:
ooo setup
```

> `ooo` commands are Claude Code skills — they run **inside a Claude Code session**, not in your terminal.
> Setup registers the MCP server globally (one-time) and optionally adds an Ouroboros reference block to your project's CLAUDE.md.

**Step 3 — Start building:**
```
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

### Ambiguity Score: The Gate Between Wonder and Code

The Interview doesn't end when you feel ready — it ends when the **math** says you're ready. Ouroboros quantifies ambiguity as the inverse of weighted clarity:

```
Ambiguity = 1 − Σ(clarityᵢ × weightᵢ)
```

Each dimension is scored 0.0–1.0 by the LLM (temperature 0.1 for reproducibility), then weighted:

| Dimension | Greenfield | Brownfield |
|:----------|:----------:|:----------:|
| **Goal Clarity** — *Is the goal specific?* | 40% | 35% |
| **Constraint Clarity** — *Are limitations defined?* | 30% | 25% |
| **Success Criteria** — *Are outcomes measurable?* | 30% | 25% |
| **Context Clarity** — *Is the existing codebase understood?* | — | 15% |

**Threshold: Ambiguity ≤ 0.2** — only then can a Seed be generated.

```
Example (Greenfield):

  Goal: 0.9 × 0.4  = 0.36
  Constraint: 0.8 × 0.3  = 0.24
  Success: 0.7 × 0.3  = 0.21
                        ──────
  Clarity             = 0.81
  Ambiguity = 1 − 0.81 = 0.19  ≤ 0.2 → ✓ Ready for Seed
```

Why 0.2? Because at 80% weighted clarity, the remaining unknowns are small enough that code-level decisions can resolve them. Above that threshold, you're still guessing at architecture.

### Ontology Convergence: When the Serpent Stops

The evolutionary loop doesn't run forever. It stops when consecutive generations produce ontologically identical schemas. Similarity is measured as a weighted comparison of schema fields:

```
Similarity = 0.5 × name_overlap + 0.3 × type_match + 0.2 × exact_match
```

| Component | Weight | What It Measures |
|:----------|:------:|:-----------------|
| **Name overlap** | 50% | Do the same field names exist in both generations? |
| **Type match** | 30% | Do shared fields have the same types? |
| **Exact match** | 20% | Are name, type, AND description all identical? |

**Threshold: Similarity ≥ 0.95** — the loop converges and stops evolving.

But raw similarity isn't the only signal. The system also detects pathological patterns:

| Signal | Condition | What It Means |
|:-------|:----------|:--------------|
| **Stagnation** | Similarity ≥ 0.95 for 3 consecutive generations | Ontology has stabilized |
| **Oscillation** | Gen N ≈ Gen N-2 (period-2 cycle) | Stuck bouncing between two designs |
| **Repetitive feedback** | ≥ 70% question overlap across 3 generations | Wonder is asking the same things |
| **Hard cap** | 30 generations reached | Safety valve |

```
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     → similarity 0.78 → CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     → similarity 1.00 → CONVERGED ✓
```

Two mathematical gates, one philosophy: **don't build until you're clear (Ambiguity ≤ 0.2), don't stop evolving until you're stable (Similarity ≥ 0.95).**

---

## Commands

> All `ooo` commands run inside a Claude Code session, not in your terminal.
> Run `ooo setup` after installation to register the MCP server (one-time) and optionally integrate with your project's CLAUDE.md.

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

## Real-Time Monitoring (TUI)

Ouroboros includes a **terminal dashboard** for real-time workflow monitoring. Run it in a separate terminal while `ooo run` or `ooo evolve` is executing:

```bash
# Install and launch
uvx --from ouroboros-ai ouroboros tui monitor

# Or if installed locally
uv run ouroboros tui monitor
```

| Key | Screen | What You See |
|:---:|:-------|:-------------|
| `1` | **Dashboard** | Phase progress, acceptance criteria tree, live status |
| `2` | **Execution** | Timeline, phase outputs, detailed events |
| `3` | **Logs** | Filterable log viewer with level-based coloring |
| `4` | **Debug** | State inspector, raw events, configuration |

> See [TUI Usage Guide](./docs/guides/tui-usage.md) for full details.

---

## Contributing

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

[Issues](https://github.com/Q00/ouroboros/issues) · [Discussions](https://github.com/Q00/ouroboros/discussions)

---

## Star History

<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-light-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=light" alt="Star History Chart" width="100%" />
</a>
<a href="https://www.star-history.com/?repos=Q00/ouroboros&type=Date#gh-dark-mode-only">
  <img src="https://api.star-history.com/svg?repos=Q00/ouroboros&type=Date&theme=dark" alt="Star History Chart" width="100%" />
</a>

---

<p align="center">
  <em>"The beginning is the end, and the end is the beginning."</em>
  <br/><br/>
  <strong>The serpent doesn't repeat — it evolves.</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
