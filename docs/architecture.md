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

### Phase 2: Double Diamond

The execution phase uses the Double Diamond design process with recursive decomposition.

**Components:**
- `execution/double_diamond.py` - Four-phase execution cycle
- `execution/decomposition.py` - Hierarchical task decomposition
- `execution/atomicity.py` - Atomicity detection for tasks
- `execution/subagent.py` - Isolated subagent execution

**Four Phases:**
1. **Discover** - Diverge to explore the problem space
2. **Define** - Converge on the core problem
3. **Design** - Diverge to explore solutions
4. **Deliver** - Converge on implementation

### Phase 3: Resilience

When execution stalls, the resilience system detects stagnation and applies lateral thinking.

**Components:**
- `resilience/stagnation.py` - Stagnation detection (4 patterns)
- `resilience/lateral.py` - Persona rotation and lateral thinking

**Stagnation Patterns:**
1. Repeated similar outputs
2. Error loops
3. Resource exhaustion
4. Time-based limits

**Personas:**
| Persona | Strategy | Trigger |
|---------|----------|---------|
| THE HACKER | Make it work, elegance be damned | Quick fix needed |
| THE RESEARCHER | Stop coding, read the docs | Knowledge gap |
| THE SIMPLIFIER | Cut scope, return to MVP | Overengineering |
| THE ARCHITECT | Question foundations, rebuild | Structural issues |

### Phase 4: Evaluation

Three-stage progressive evaluation ensures quality while minimizing cost.

**Components:**
- `evaluation/pipeline.py` - Evaluation pipeline orchestration
- `evaluation/mechanical.py` - Stage 1: Mechanical checks
- `evaluation/semantic.py` - Stage 2: Semantic verification
- `evaluation/consensus.py` - Stage 3: Multi-model consensus
- `evaluation/trigger.py` - Consensus trigger matrix

**Stages:**
1. **Mechanical ($0)** - Lint, build, test, static analysis
2. **Semantic ($$)** - AC compliance check, drift measurement
3. **Consensus ($$$$)** - Multi-model voting (triggered at gates only)

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

All state changes are persisted as events, enabling:
- Full audit trail
- Checkpoint/recovery
- Session resumption
- Retrospective analysis

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

Ouroboros can both consume and expose MCP:
- **Client** - Connect to external MCP servers for additional tools/resources
- **Server** - Expose Ouroboros as an MCP server for other AI agents

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
