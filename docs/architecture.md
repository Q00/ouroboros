# Ouroboros System Architecture

This document provides an overview of the Ouroboros system architecture, its six phases, and core design principles.

## Philosophy

### The Problem

Human requirements arrive **ambiguous**, **incomplete**, **contradictory**, and **surface-level**. If AI executes such input directly, the result is GIGO (Garbage In, Garbage Out).

### The Solution

Ouroboros applies two ancient methods to transmute irrational input into executable truth:

1. **Socratic Questioning** - Reveals hidden assumptions, exposes contradictions, challenges the obvious
2. **Ontological Analysis** - Finds the root problem, separates essential from accidental, maps the structure of being

## The Six Phases

```
Phase 0: BIG BANG         -> Crystallize requirements into a Seed
Phase 1: PAL ROUTER       -> Select appropriate model tier
Phase 2: DOUBLE DIAMOND   -> Decompose and execute tasks
Phase 3: RESILIENCE       -> Handle stagnation with lateral thinking
Phase 4: EVALUATION       -> Verify outputs at three stages
Phase 5: SECONDARY LOOP   -> Process deferred TODOs
         ↺ (cycle back as needed)
```

### Phase 0: Big Bang

The Big Bang phase transforms vague ideas into crystallized specifications through iterative questioning.

**Components:**
- `bigbang/interview.py` - InterviewEngine for conducting Socratic interviews
- `bigbang/ambiguity.py` - Ambiguity score calculation
- `bigbang/seed_generator.py` - Seed generation from interview results

**Process:**
1. User provides initial context/idea
2. Engine asks clarifying questions (up to MAX_INTERVIEW_ROUNDS)
3. Ambiguity score calculated after each response
4. Interview completes when ambiguity <= 0.2
5. Immutable Seed generated

**Gate:** Ambiguity <= 0.2

### Phase 1: PAL Router (Progressive Adaptive LLM)

The PAL Router selects the most cost-effective model tier based on task complexity.

**Components:**
- `routing/router.py` - Main routing logic
- `routing/complexity.py` - Task complexity estimation
- `routing/tiers.py` - Model tier definitions
- `routing/escalation.py` - Escalation logic on failure
- `routing/downgrade.py` - Downgrade logic on success

**Tiers:**
| Tier | Cost | Complexity Threshold |
|------|------|---------------------|
| FRUGAL | 1x | < 0.4 |
| STANDARD | 10x | < 0.7 |
| FRONTIER | 30x | >= 0.7 or critical |

**Strategy:** Start frugal, escalate only on failure.

**Complexity Scoring Algorithm:**

The complexity score is a weighted sum of three normalized factors:

| Factor | Weight | Normalization | Threshold |
|--------|--------|---------------|-----------|
| Token count | 30% | `min(tokens / 4000, 1.0)` | 4000 tokens |
| Tool dependencies | 30% | `min(tools / 5, 1.0)` | 5 tools |
| AC nesting depth | 40% | `min(depth / 5, 1.0)` | depth 5 |

```
complexity = 0.30 * norm_tokens + 0.30 * norm_tools + 0.40 * norm_depth
```

**Escalation Path:**

When a task fails consecutively at its current tier (threshold: 2 failures), it escalates:

```
Frugal → Standard → Frontier → Stagnation Event (triggers resilience)
```

**Downgrade Path:**

After sustained success (threshold: 5 consecutive successes), the tier downgrades:

```
Frontier → Standard → Frugal
```

Similar task patterns (Jaccard similarity >= 0.80) inherit tier preferences from previously successful tasks.

### Phase 2: Double Diamond

The execution phase uses the Double Diamond design process with recursive decomposition.

**Components:**
- `execution/double_diamond.py` - Four-phase execution cycle
- `execution/decomposition.py` - Hierarchical task decomposition
- `execution/atomicity.py` - Atomicity detection for tasks
- `execution/subagent.py` - Isolated subagent execution

**Four Phases:**
1. **Discover** (divergent) - Explore the problem space broadly
2. **Define** (convergent) - Converge on the core problem
3. **Design** (divergent) - Explore solution approaches
4. **Deliver** (convergent) - Converge on implementation

**Recursive Decomposition:**

Each AC goes through Discover and Define, then atomicity is checked:
- **Atomic** (single-focused, 1-2 files) → proceed to Design and Deliver
- **Non-atomic** → decompose into 2-5 child ACs, recurse on each child

Key constraints:
- `MAX_DEPTH = 5` — hard recursion limit
- `COMPRESSION_DEPTH = 3` — context truncated to 500 chars at depth 3+
- Children are dependency-sorted and executed in parallel within each level

See [Execution Deep Dive](./design/execution-deep-dive.md) for the full recursive algorithm and configuration reference.

### Phase 3: Resilience

When execution stalls, the resilience system detects stagnation and applies lateral thinking.

**Components:**
- `resilience/stagnation.py` - Stagnation detection (4 patterns)
- `resilience/lateral.py` - Persona rotation and lateral thinking

**Stagnation Patterns (4):**

| Pattern | Detection | Default Threshold |
|---------|-----------|-------------------|
| **SPINNING** | Same output hash repeated (SHA-256) | 3 repetitions |
| **OSCILLATION** | A→B→A→B alternating pattern | 2 cycles |
| **NO_DRIFT** | Drift score unchanging (epsilon < 0.01) | 3 iterations |
| **DIMINISHING_RETURNS** | Progress improvement rate < 0.01 | 3 iterations |

Detection is stateless — all state passed via `ExecutionHistory` (phase outputs, error signatures, drift scores).

**Personas (5):**

| Persona | Strategy | Best For (Affinity) |
|---------|----------|---------------------|
| **HACKER** | Unconventional workarounds | SPINNING |
| **RESEARCHER** | Seek more information | NO_DRIFT, DIMINISHING_RETURNS |
| **SIMPLIFIER** | Reduce complexity | DIMINISHING_RETURNS, OSCILLATION |
| **ARCHITECT** | Restructure fundamentally | OSCILLATION, NO_DRIFT |
| **CONTRARIAN** | Challenge all assumptions | All patterns |

Each persona generates a thinking prompt (not a solution). `suggest_persona_for_pattern()` recommends the best persona for a given stagnation type based on these affinities.

### Phase 4: Evaluation

Three-stage progressive evaluation ensures quality while minimizing cost.

**Components:**
- `evaluation/pipeline.py` - Evaluation pipeline orchestration
- `evaluation/mechanical.py` - Stage 1: Mechanical checks
- `evaluation/semantic.py` - Stage 2: Semantic verification
- `evaluation/consensus.py` - Stage 3: Multi-model consensus
- `evaluation/trigger.py` - Consensus trigger matrix

**Stages:**
1. **Mechanical ($0)** — Lint, build, test, static analysis, coverage (threshold: 70%)
   - If any check fails → pipeline stops, returns failure
2. **Semantic ($$)** — AC compliance, goal alignment, drift, uncertainty scoring
   - If score >= 0.8 and no trigger → approved without consensus
   - Uses Standard tier model (temperature: 0.2)
3. **Consensus ($$$)** — Multi-model voting, only when triggered by 1 of 6 conditions
   - Simple mode: 3 models vote (GPT-4o, Claude Sonnet 4, Gemini 2.5 Pro), 2/3 majority required
   - Deliberative mode: Advocate/Devil's Advocate/Judge roles with ontological questioning

**6 Consensus Trigger Conditions** (checked in priority order):
1. Seed modification (seeds are immutable — any change requires consensus)
2. Ontology evolution (schema changes affect output structure)
3. Goal reinterpretation
4. Seed drift > 0.3
5. Stage 2 uncertainty > 0.3
6. Lateral thinking adoption

See [Evaluation Pipeline Deep Dive](./design/evaluation-pipeline-deep-dive.md) for thresholds, configuration, and deliberative consensus details.

### Phase 5: Secondary Loop

Non-critical tasks are deferred to maintain focus on the primary goal.

**Components:**
- `secondary/todo_registry.py` - TODO item tracking
- `secondary/scheduler.py` - Batch processing scheduler

**Process:**
1. During execution, non-blocking TODOs registered
2. After primary goal completion, TODOs batch-processed
3. Low-priority tasks executed during idle time

## Module Structure

```
src/ouroboros/
|
+-- core/           # Foundation: types, errors, seed, context
|   +-- types.py       # Result type, type aliases
|   +-- errors.py      # Error hierarchy
|   +-- seed.py        # Immutable Seed specification
|   +-- context.py     # Workflow context management
|   +-- ac_tree.py     # Acceptance criteria tree
|
+-- bigbang/        # Phase 0: Interview and seed generation
+-- routing/        # Phase 1: PAL router
+-- execution/      # Phase 2: Double Diamond execution
+-- resilience/     # Phase 3: Stagnation and lateral thinking
+-- evaluation/     # Phase 4: Three-stage evaluation
+-- secondary/      # Phase 5: TODO registry and scheduling
|
+-- orchestrator/   # Claude Agent SDK integration
|   +-- adapter.py     # Claude Agent SDK wrapper
|   +-- runner.py      # Orchestration logic
|   +-- session.py     # Session state tracking
|   +-- events.py      # Orchestrator events
|   +-- mcp_tools.py   # MCP tool provider for external tools
|   +-- mcp_config.py  # MCP client configuration loading
|
+-- mcp/            # Model Context Protocol integration
|   +-- client/        # MCP client for external servers
|   +-- server/        # MCP server exposing Ouroboros
|   +-- tools/         # Tool definitions and registry
|   +-- resources/     # Resource handlers
|
+-- providers/      # LLM provider adapters
|   +-- base.py        # Provider protocol
|   +-- litellm_adapter.py  # LiteLLM integration
|
+-- persistence/    # Event sourcing and checkpoints
|   +-- event_store.py # Event storage
|   +-- checkpoint.py  # Checkpoint/recovery
|   +-- schema.py      # Database schema
|
+-- observability/  # Logging and monitoring
|   +-- logging.py     # Structured logging
|   +-- drift.py       # Drift measurement
|   +-- retrospective.py  # Automatic retrospectives
|
+-- config/         # Configuration management
+-- cli/            # Command-line interface
```

## Core Concepts

### The Seed

The Seed is the "constitution" of a workflow - an immutable specification with:
- **Goal** - Primary objective
- **Constraints** - Hard requirements that must be satisfied
- **Acceptance Criteria** - Specific criteria for success
- **Ontology Schema** - Structure of workflow outputs
- **Exit Conditions** - When to terminate

Once generated, the Seed cannot be modified (frozen Pydantic model).

### Result Type

Ouroboros uses a Result type for handling expected failures without exceptions:

```python
result: Result[int, str] = Result.ok(42)
# or
result: Result[int, str] = Result.err("something went wrong")

if result.is_ok:
    process(result.value)
else:
    handle_error(result.error)
```

### Event Sourcing

All state changes are persisted as immutable events in a single SQLite table (`events`) via SQLAlchemy Core:
- **Event types** use dot-notation past tense (e.g., `orchestrator.session.started`, `execution.ac.completed`)
- **Append-only** — events can never be modified or deleted
- **Unit of Work** pattern groups events + checkpoint into atomic commits
- **Replay** capability — reconstruct any session by replaying its events

Enables:
- Full audit trail
- Checkpoint/recovery (3-level rollback depth, 5-minute periodic checkpointing)
- Session resumption
- Retrospective analysis

**Event Schema:**
- Single `events` table with columns: `id` (UUID), `aggregate_type`, `aggregate_id`, `event_type`, `payload` (JSON), `timestamp`, `consensus_id`
- 5 indexes: `aggregate_type`, `aggregate_id`, `(aggregate_type, aggregate_id)` composite, `event_type`, `timestamp`

### Security Limits

Input validation constants for DoS prevention (defined in `core/security.py`):

| Constant | Value | Purpose |
|----------|-------|---------|
| MAX_INITIAL_CONTEXT_LENGTH | 50,000 chars | Interview input limit |
| MAX_USER_RESPONSE_LENGTH | 10,000 chars | Interview response limit |
| MAX_SEED_FILE_SIZE | 1,000,000 bytes | Seed YAML file size cap |
| MAX_LLM_RESPONSE_LENGTH | 100,000 chars | LLM response truncation |

### Drift Control

Drift measurement tracks how far execution has strayed from the original Seed:
- Drift score 0.0 - 1.0
- Automatic retrospective every N cycles
- High drift triggers re-examination of the Seed

## Integration Points

### Claude Agent SDK

The orchestrator module integrates with Claude Agent SDK for:
- Streaming task execution
- Tool use (Read, Write, Edit, Bash, etc.)
- Session management
- Resume capability

### MCP (Model Context Protocol)

Ouroboros functions as an **MCP Hub**, capable of both consuming and exposing MCP:

#### MCP Server Mode
Expose Ouroboros as an MCP server for other AI agents:
```bash
ouroboros mcp serve
```
- Provides tools: `ouroboros_execute_seed`, `ouroboros_session_status`, `ouroboros_query_events`
- Integrates with Claude Desktop and other MCP clients

#### MCP Client Mode
Connect to external MCP servers during workflow execution:
```bash
ouroboros run --mcp-config mcp.yaml seed.yaml
```
- Discovers tools from configured MCP servers
- Merges with built-in tools (Read, Write, Edit, Bash, Glob, Grep)
- Provides additional capabilities (filesystem, GitHub, databases, etc.)

**Tool Precedence:**
1. Built-in tools always win
2. First MCP server in config wins for duplicate tool names
3. Use `--mcp-tool-prefix` to namespace MCP tools

**Architecture:**
```
                           +------------------+
                           |   Ouroboros      |
                           | (MCP Hub)        |
                           +--------+---------+
                                    |
              +---------------------+---------------------+
              |                                           |
    +---------v---------+                       +---------v---------+
    | MCP Server Mode   |                       | MCP Client Mode   |
    | (expose tools)    |                       | (consume tools)   |
    +---------+---------+                       +---------+---------+
              |                                           |
    +---------v---------+                       +---------v---------+
    | Claude Desktop    |                       | External MCP      |
    | MCP Clients       |                       | Servers           |
    +-------------------+                       +-------------------+
                                                         |
                                    +--------------------+--------------------+
                                    |                    |                    |
                           +--------v-------+   +--------v-------+   +--------v-------+
                           | filesystem     |   | github         |   | postgres       |
                           | server         |   | server         |   | server         |
                           +----------------+   +----------------+   +----------------+
```

### LiteLLM

All LLM calls go through LiteLLM for:
- Provider abstraction (100+ models)
- Automatic retries
- Cost tracking
- Streaming support

## Design Principles

1. **Frugal First** - Start with the cheapest option, escalate only when needed
2. **Immutable Direction** - The Seed cannot change; only the path to achieve it adapts
3. **Progressive Verification** - Cheap checks first, expensive consensus only at gates
4. **Lateral Over Vertical** - When stuck, change perspective rather than try harder
5. **Event-Sourced** - Every state change is an event; nothing is lost
