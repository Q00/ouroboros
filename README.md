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
  <sub>Specification-first workflow engine for AI coding agents</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/ouroboros-ai/"><img src="https://img.shields.io/pypi/v/ouroboros-ai?color=blue" alt="PyPI"></a>
  <a href="https://github.com/Q00/ouroboros/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Q00/ouroboros/test.yml?branch=main" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-ouroboros">Why</a> ·
  <a href="#what-you-get">Results</a> ·
  <a href="#the-loop">How It Works</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#from-wonder-to-ontology">Philosophy</a>
</p>

---

> **New: PM Mode** — `ooo pm` generates a PRD through a guided PM-focused interview.
> Ouroboros now supports product management workflows: stakeholder alignment, user story mapping, and structured PRD generation — all driven by the same Socratic engine.
>
> ```
> > ooo pm "I want to build a notification system"
> ```

---

**Turn a vague idea into a verified, working codebase -- with any AI coding agent.**

Ouroboros sits between you and your AI runtime (Claude Code, Codex CLI, or others). It replaces ad-hoc prompting with a structured specification-first workflow: interview, crystallize, execute, evaluate, evolve.

---

## Why Ouroboros?

Most AI coding fails at the **input**, not the output. The bottleneck is not AI capability -- it is human clarity.

| Problem | What Happens | Ouroboros Fix |
|:--------|:-------------|:--------------|
| Vague prompts | AI guesses, you rework | Socratic interview exposes hidden assumptions |
| No spec | Architecture drifts mid-build | Immutable seed spec locks intent before code |
| Manual QA | "Looks good" is not verification | 3-stage automated evaluation gate |

---

## Quick Start

**Install** — one command, everything auto-detected:

```bash
curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
```

**Build** — open your AI coding agent and go:

```
> ooo interview "I want to build a task management CLI"
```

> Works with Claude Code and Codex CLI. The installer detects your runtime, registers the MCP server, and installs skills automatically.

<details>
<summary><strong>Other install methods</strong></summary>

**Claude Code plugin only** (no system package):
```bash
claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros
```
Then run `ooo setup` inside a Claude Code session.

**pip / uv / pipx**:
```bash
pip install ouroboros-ai                # base
pip install ouroboros-ai[claude]        # + Claude Code deps
pip install ouroboros-ai[litellm]       # + LiteLLM multi-provider
pip install ouroboros-ai[mcp]           # + MCP server/client support
pip install ouroboros-ai[tui]           # + Textual terminal UI
pip install ouroboros-ai[all]           # everything
ouroboros setup                         # configure runtime
```

See runtime guides: [Claude Code](./docs/runtime-guides/claude-code.md) · [Codex CLI](./docs/runtime-guides/codex.md)

</details>

<details>
<summary><strong>Uninstall</strong></summary>

```bash
ouroboros uninstall
```

Removes all configuration, MCP registration, and data. See [UNINSTALL.md](./UNINSTALL.md) for details.

</details>

> **Python >= 3.12 required.** See [pyproject.toml](./pyproject.toml) for the full dependency list.

---

## What You Get

After one loop of the Ouroboros cycle, a vague idea becomes a verified codebase:

| Step | Before | After |
|:-----|:-------|:------|
| **Interview** | *"Build me a task CLI"* | 12 hidden assumptions exposed, ambiguity scored to 0.19 |
| **Seed** | No spec | Immutable specification with acceptance criteria, ontology, constraints |
| **Evaluate** | Manual review | 3-stage gate: Mechanical (free) -> Semantic -> Multi-Model Consensus |

<details>
<summary><strong>What just happened?</strong></summary>

```
interview  ->  Socratic questioning exposed 12 hidden assumptions
seed       ->  Crystallized answers into an immutable spec (Ambiguity: 0.15)
run        ->  Executed via Double Diamond decomposition
evaluate   ->  3-stage verification: Mechanical -> Semantic -> Consensus
```

> Use `ooo <cmd>` inside your AI coding agent session, or `ouroboros init start`, `ouroboros run seed.yaml`, etc. from the terminal.

The serpent completed one loop. Each loop, it knows more than the last.

</details>

---

## How It Compares

AI coding tools are powerful -- but they solve the **wrong problem** when the input is unclear.

| | Vanilla AI Coding | Ouroboros |
|:--|:------------------|:---------|
| **Vague prompt** | AI guesses intent, builds on assumptions | Socratic interview forces clarity *before* code |
| **Spec validation** | No spec -- architecture drifts mid-build | Immutable seed spec locks intent; Ambiguity gate (<= 0.2) blocks premature code |
| **Evaluation** | "Looks good" / manual QA | 3-stage automated gate: Mechanical -> Semantic -> Multi-Model Consensus |
| **Rework rate** | High -- wrong assumptions surface late | Low -- assumptions surface in the interview, not in the PR review |

---

## The Loop

The ouroboros -- a serpent devouring its own tail -- is not decoration. It IS the architecture:

```
    Interview -> Seed -> Execute -> Evaluate
        ^                           |
        +---- Evolutionary Loop ----+
```

Each cycle does not repeat -- it **evolves**. The output of evaluation feeds back as input for the next generation, until the system truly knows what it is building.

| Phase | What Happens |
|:------|:-------------|
| **Interview** | Socratic questioning exposes hidden assumptions |
| **Seed** | Answers crystallize into an immutable specification |
| **Execute** | Double Diamond: Discover -> Define -> Design -> Deliver |
| **Evaluate** | 3-stage gate: Mechanical ($0) -> Semantic -> Multi-Model Consensus |
| **Evolve** | Wonder *("What do we still not know?")* -> Reflect -> next generation |

> *"This is where the Ouroboros eats its tail: the output of evaluation*
> *becomes the input for the next generation's seed specification."*
> -- `reflect.py`

Convergence is reached when ontology similarity >= 0.95 -- when the system has questioned itself into clarity.

### Ralph: The Loop That Never Stops

`ooo ralph` runs the evolutionary loop persistently -- across session boundaries -- until convergence is reached. Each step is **stateless**: the EventStore reconstructs the full lineage, so even if your machine restarts, the serpent picks up where it left off.

```
Ralph Cycle 1: evolve_step(lineage, seed) -> Gen 1 -> action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       -> Gen 2 -> action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       -> Gen 3 -> action=CONVERGED
                                                +-- Ralph stops.
                                                    The ontology has stabilized.
```

---

## Commands

Inside AI coding agent sessions, use `ooo <cmd>` skills. From the terminal, use the `ouroboros` CLI.

| Skill (`ooo`) | CLI equivalent | What It Does |
|:---------------|:---------------|:-------------|
| `ooo setup` | `ouroboros setup` | Register runtime and configure project (one-time) |
| `ooo interview` | `ouroboros init start` | Socratic questioning -- expose hidden assumptions |
| `ooo seed` | *(generated by interview)* | Crystallize into immutable spec |
| `ooo run` | `ouroboros run seed.yaml` | Execute via Double Diamond decomposition |
| `ooo evaluate` | *(via MCP)* | 3-stage verification gate |
| `ooo evolve` | *(via MCP)* | Evolutionary loop until ontology converges |
| `ooo unstuck` | *(via MCP)* | 5 lateral thinking personas when you are stuck |
| `ooo status` | `ouroboros status executions` / `ouroboros status execution <id>` | Session tracking + (MCP-only) drift detection |
| `ooo cancel` | `ouroboros cancel execution [<id>\|--all]` | Cancel stuck or orphaned executions |
| `ooo ralph` | *(via MCP)* | Persistent loop until verified |
| `ooo tutorial` | *(interactive)* | Interactive hands-on learning |
| `ooo help` | `ouroboros --help` | Full reference |
| `ooo pm` | *(via MCP)* | PM-focused interview + PRD generation |
| `ooo qa` | *(via skill)* | General-purpose QA verdict for any artifact |
| `ooo update` | `ouroboros update` | Check for updates + upgrade to latest |
| `ooo brownfield` | *(via skill)* | Scan and manage brownfield repo defaults |

> Not all skills have direct CLI equivalents. Some (`evaluate`, `evolve`, `unstuck`, `ralph`) are available through agent skills or MCP tools only.

See the [CLI reference](./docs/cli-reference.md) for full details.

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
<summary><strong>Architecture overview -- Python >= 3.12</strong></summary>

```
src/ouroboros/
+-- bigbang/        Interview, ambiguity scoring, brownfield explorer
+-- routing/        PAL Router -- 3-tier cost optimization (1x / 10x / 30x)
+-- execution/      Double Diamond, hierarchical AC decomposition
+-- evaluation/     Mechanical -> Semantic -> Multi-Model Consensus
+-- evolution/      Wonder / Reflect cycle, convergence detection
+-- resilience/     4-pattern stagnation detection, 5 lateral personas
+-- observability/  3-component drift measurement, auto-retrospective
+-- persistence/    Event sourcing (SQLAlchemy + aiosqlite), checkpoints
+-- orchestrator/   Runtime abstraction layer (Claude Code, Codex CLI)
+-- core/           Types, errors, seed, ontology, security
+-- providers/      LiteLLM adapter (100+ models)
+-- mcp/            MCP client/server integration
+-- plugin/         Plugin system (skill/agent auto-discovery)
+-- tui/            Terminal UI dashboard
+-- cli/            Typer-based CLI
```

**Key internals:**
- **PAL Router** -- Frugal (1x) -> Standard (10x) -> Frontier (30x) with auto-escalation on failure, auto-downgrade on success
- **Drift** -- Goal (50%) + Constraint (30%) + Ontology (20%) weighted measurement, threshold <= 0.3
- **Brownfield** -- Auto-detects config files across multiple language ecosystems
- **Evolution** -- Up to 30 generations, convergence at ontology similarity >= 0.95
- **Stagnation** -- Detects spinning, oscillation, no-drift, and diminishing returns patterns
- **Runtime backends** -- Pluggable abstraction layer (`orchestrator.runtime_backend` config) with first-class support for Claude Code and Codex CLI; same workflow spec, different execution engines

See [Architecture](./docs/architecture.md) for the full design document.

</details>

---

## From Wonder to Ontology

<details>
<summary><strong>The philosophical engine behind Ouroboros</strong></summary>

> *Wonder -> "How should I live?" -> "What IS 'live'?" -> Ontology*
> -- Socrates

Every great question leads to a deeper question -- and that deeper question is always **ontological**: not *"how do I do this?"* but *"what IS this, really?"*

```
   Wonder                          Ontology
"What do I want?"    ->    "What IS the thing I want?"
"Build a task CLI"   ->    "What IS a task? What IS priority?"
"Fix the auth bug"   ->    "Is this the root cause, or a symptom?"
```

This is not abstraction for its own sake. When you answer *"What IS a task?"* -- deletable or archivable? solo or team? -- you eliminate an entire class of rework. **The ontological question is the most practical question.**

Ouroboros embeds this into its architecture through the **Double Diamond**:

```
    * Wonder          * Design
   /  (diverge)      /  (diverge)
  /    explore      /    create
 /                 /
* ------------ * ------------ *
 \                 \
  \    define       \    deliver
   \  (converge)     \  (converge)
    * Ontology        * Evaluation
```

The first diamond is **Socratic**: diverge into questions, converge into ontological clarity. The second diamond is **pragmatic**: diverge into design options, converge into verified delivery. Each diamond requires the one before it -- you cannot design what you have not understood.

</details>

<details>
<summary><strong>Ambiguity Score: The Gate Between Wonder and Code</strong></summary>

The Interview does not end when you feel ready -- it ends when the **math** says you are ready. Ouroboros quantifies ambiguity as the inverse of weighted clarity:

```
Ambiguity = 1 - Sum(clarity_i * weight_i)
```

Each dimension is scored 0.0-1.0 by the LLM (temperature 0.1 for reproducibility), then weighted:

| Dimension | Greenfield | Brownfield |
|:----------|:----------:|:----------:|
| **Goal Clarity** -- *Is the goal specific?* | 40% | 35% |
| **Constraint Clarity** -- *Are limitations defined?* | 30% | 25% |
| **Success Criteria** -- *Are outcomes measurable?* | 30% | 25% |
| **Context Clarity** -- *Is the existing codebase understood?* | -- | 15% |

**Threshold: Ambiguity <= 0.2** -- only then can a Seed be generated.

```
Example (Greenfield):

  Goal: 0.9 * 0.4  = 0.36
  Constraint: 0.8 * 0.3  = 0.24
  Success: 0.7 * 0.3  = 0.21
                        ------
  Clarity             = 0.81
  Ambiguity = 1 - 0.81 = 0.19  <= 0.2 -> Ready for Seed
```

Why 0.2? Because at 80% weighted clarity, the remaining unknowns are small enough that code-level decisions can resolve them. Above that threshold, you are still guessing at architecture.

</details>

<details>
<summary><strong>Ontology Convergence: When the Serpent Stops</strong></summary>

The evolutionary loop does not run forever. It stops when consecutive generations produce ontologically identical schemas. Similarity is measured as a weighted comparison of schema fields:

```
Similarity = 0.5 * name_overlap + 0.3 * type_match + 0.2 * exact_match
```

| Component | Weight | What It Measures |
|:----------|:------:|:-----------------|
| **Name overlap** | 50% | Do the same field names exist in both generations? |
| **Type match** | 30% | Do shared fields have the same types? |
| **Exact match** | 20% | Are name, type, AND description all identical? |

**Threshold: Similarity >= 0.95** -- the loop converges and stops evolving.

But raw similarity is not the only signal. The system also detects pathological patterns:

| Signal | Condition | What It Means |
|:-------|:----------|:--------------|
| **Stagnation** | Similarity >= 0.95 for 3 consecutive generations | Ontology has stabilized |
| **Oscillation** | Gen N ~ Gen N-2 (period-2 cycle) | Stuck bouncing between two designs |
| **Repetitive feedback** | >= 70% question overlap across 3 generations | Wonder is asking the same things |
| **Hard cap** | 30 generations reached | Safety valve |

```
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     -> similarity 0.78 -> CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     -> similarity 1.00 -> CONVERGED
```

Two mathematical gates, one philosophy: **do not build until you are clear (Ambiguity <= 0.2), do not stop evolving until you are stable (Similarity >= 0.95).**

</details>

---

## Contributing

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync --all-groups && uv run pytest
```

[Issues](https://github.com/Q00/ouroboros/issues) · [Discussions](https://github.com/Q00/ouroboros/discussions) · [Contributing Guide](./CONTRIBUTING.md)

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
  <strong>The serpent does not repeat -- it evolves.</strong>
  <br/><br/>
  <code>MIT License</code>
</p>
