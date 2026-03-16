<!--
doc_metadata:
  runtime_scope: [all]
-->

# The Evolutionary Loop

> *"This is where the Ouroboros eats its tail: the output of evaluation
> becomes the input for the next generation's seed specification."*
> -- `reflect.py`

The evolutionary loop is the core feedback mechanism that distinguishes Ouroboros from linear AI coding tools. After execution and evaluation, the system **does not stop** -- it asks *"What do we still not know?"* and feeds the answer back into the next generation.

---

## How It Works

```
Gen 1:  Seed(O₁)              → Execute → Evaluate
Gen 2:  Wonder(O₁,E₁) → Reflect → Seed(O₂) → Execute → Evaluate
Gen 3:  Wonder(O₂,E₂) → Reflect → Seed(O₃) → Execute → Evaluate
...until convergence or max_generations (30)
```

**Gen 1** uses the seed from the Socratic interview. **Gen 2+** are fully autonomous -- the Wonder and Reflect engines replace human input.

### The Two Engines

| Engine | Question | Input | Output |
|--------|----------|-------|--------|
| **Wonder** | *"What do we still not know?"* | Current ontology + evaluation results | Questions, tensions, gaps |
| **Reflect** | *"How should the spec evolve?"* | Wonder output + execution artifacts | Refined ACs + ontology mutations |

**Wonder** is philosophical -- it identifies what the system is *assuming* rather than *knowing*. Inspired by Socrates: wonder leads to deeper ontological questions.

**Reflect** is pragmatic -- it takes those gaps and produces concrete changes: new acceptance criteria, modified ontology fields, tightened constraints.

---

## Convergence: When the Serpent Stops

The loop terminates when the ontology stabilizes. Similarity is measured as a weighted comparison:

```
Similarity = 0.5 * name_overlap + 0.3 * type_match + 0.2 * exact_match
```

| Component | Weight | Measures |
|-----------|--------|----------|
| **Name overlap** | 50% | Same field names in both generations? |
| **Type match** | 30% | Shared fields have same types? |
| **Exact match** | 20% | Name, type, AND description identical? |

**Threshold: Similarity >= 0.95** -- the loop converges and stops.

### Termination Signals

The `ConvergenceCriteria` checks four signals (any one triggers termination):

| Signal | Condition | Default |
|--------|-----------|---------|
| **Ontology stability** | `similarity(Oₙ, Oₙ₋₁) >= threshold` | >= 0.95 |
| **Stagnation** | Similarity >= threshold for N consecutive gens | 3 gens |
| **Oscillation** | Gen N ≈ Gen N-2 (period-2 cycle) | Enabled |
| **Hard cap** | Max generations reached | 30 |

A minimum of 2 generations must complete before convergence signals 1-3 are checked.

```
Gen 1: {Task, Priority, Status}
Gen 2: {Task, Priority, Status, DueDate}     → similarity 0.78 → CONTINUE
Gen 3: {Task, Priority, Status, DueDate}     → similarity 1.00 → CONVERGED
```

---

## Ralph: The Persistent Loop

`ooo ralph` (Claude Code) or `ouroboros ralph` (CLI) runs the evolutionary loop persistently -- across session boundaries -- until convergence. Each step is **stateless**: the EventStore reconstructs the full lineage, so even if your machine restarts, the serpent picks up where it left off.

```
Ralph Cycle 1: evolve_step(lineage, seed) → Gen 1 → action=CONTINUE
Ralph Cycle 2: evolve_step(lineage)       → Gen 2 → action=CONTINUE
Ralph Cycle 3: evolve_step(lineage)       → Gen 3 → action=CONVERGED
                                                 └── Ralph stops.
                                                     The ontology has stabilized.
```

### Ralph vs Evolve

| | `ooo evolve` / `ouroboros evolve` | `ooo ralph` |
|---|---|---|
| **Scope** | Single evolution step | Loop until convergence |
| **Session** | Within current session | Survives session restarts |
| **Control** | Manual -- you decide when to stop | Automatic -- convergence decides |
| **Use case** | Incremental refinement | Full autonomous evolution |

---

## Configuration

Evolution parameters in `~/.ouroboros/config.yaml`:

```yaml
evolution:
  max_generations: 30           # Hard cap on generations
  convergence_threshold: 0.95   # Ontology similarity threshold
  stagnation_window: 3          # Consecutive stable gens before termination
  min_generations: 2            # Minimum gens before convergence check
```

See [Configuration Reference](../config-reference.md) for the full list.

---

## Two Mathematical Gates

The entire Ouroboros workflow is governed by two numerical thresholds:

1. **Ambiguity <= 0.2** -- Do not build until you are clear (interview gate)
2. **Similarity >= 0.95** -- Do not stop evolving until you are stable (convergence gate)

The first gate prevents premature execution. The second prevents premature termination. Together they ensure the system questions itself into clarity before acting, and continues acting until the ontology stabilizes.

---

## Source Code

| Module | Purpose |
|--------|---------|
| `src/ouroboros/evolution/loop.py` | EvolutionaryLoop orchestrator |
| `src/ouroboros/evolution/wonder.py` | WonderEngine -- gap identification |
| `src/ouroboros/evolution/reflect.py` | ReflectEngine -- ontology mutation |
| `src/ouroboros/evolution/convergence.py` | Convergence criteria and signals |
| `src/ouroboros/evolution/projector.py` | Lineage state projection |
| `src/ouroboros/evolution/regression.py` | Regression detection across gens |

---

> See [Architecture](../architecture.md) for the full system design, and the [README philosophy sections](../../README.md#from-wonder-to-ontology) for the Socratic and ontological foundations.
