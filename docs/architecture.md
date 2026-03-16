# Ouroboros Architecture

## System Overview

Ouroboros is a **specification-first AI workflow engine** that transforms vague ideas into validated specifications before execution. Built on event sourcing with a rich TUI interface, it provides complete lifecycle management from requirements to evaluation.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 OUROBOROS ARCHITECTURE                                               │
├─────────────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                                     │
│  ┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐                 │
│  │      PLUGIN LAYER    │     │      CORE LAYER     │     │      PRESENTATION    │                 │
│  │                     │     │                     │     │      LAYER           │                 │
│  │  ┌───────────────┐  │     │  ┌───────────────┐  │     │  ┌───────────────┐  │                 │
│  │  │   Skills      │──┼─────┼─▶│   Seed Spec    │──┼─────┼─▶│   TUI Dashboard │  │                 │
│  │  │   (9)         │  │     │  │   (Immutable)  │  │     │  │   (Textual)   │  │                 │
│  │  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │                 │
│  │                     │     │                     │     │                     │                 │
│  │  ┌───────────────┐  │     │  ┌───────────────┐  │     │  ┌───────────────┐  │                 │
│  │  │   Agents      │──┼─────┼─▶│  Acceptance    │──┼─────┼─▶│   CLI Interface│  │                 │
│  │  │   (9)         │  │     │  │  Criteria Tree │  │     │  │   (Typer)    │  │                 │
│  │  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │                 │
│  └─────────────────────┘     └─────────────────────┘     └─────────────────────┘                 │
│           │                         │                         │                                 │
│           └─────────────────────────┼─────────────────────────┘                                 │
│                                      │                                                         │
│           ┌─────────────────────────┼─────────────────────────┐                                 │
│           │                         │                         │                                 │
│  ┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐                 │
│  │    EXECUTION LAYER   │     │    STATE LAYER     │     │    ORCHESTRATION    │                 │
│  │                     │     │                     │     │      LAYER         │                 │
│  │  ┌───────────────┐  │     │  ┌───────────────┐  │     │  ┌───────────────┐  │                 │
│  │  │ 7 Execution  │  │     │  │ Event Store  │  │     │  │ 6-Phase       │  │                 │
│  │  │   Modes      │  │     │  │  (SQLite)    │  │     │  │ Pipeline      │  │                 │
│  │  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │                 │
│  │                     │     │                     │     │                     │                 │
│  │  ┌───────────────┐  │     │  │ Checkpoint   │  │     │  │ PAL Router    │  │                 │
│  │  │ Model Router │  │     │  │   Store      │  │     │  │ (Cost Opt.)   │  │                 │
│  │  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │                 │
│  └─────────────────────┘     └─────────────────────┘     └─────────────────────┘                 │
│                                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Core Components Overview

### 1. Plugin Layer
**Auto-discovery of skills and agents through the plugin system**
- Skills: 14 core workflow skills (interview, seed, run, evaluate, evolve, cancel, unstuck, update, help, setup, ralph, tutorial, welcome, status)
- Agents: 9 specialized agents for different thinking modes
- Hot-reload capabilities without restart
- Magic prefix detection (`/ouroboros:`)

### 2. Core Layer
**Immutable data models and specifications**
- Seed: Immutable frozen Pydantic model
- Acceptance Criteria Tree: Recursive decomposition with MECE principle
- Ontology schema: Structural validation
- Version tracking and ambiguity scoring

### 3. Execution Layer
**Evolutionary execution with feedback loops**
- Self-referential persistence loop with verification
- Dependency-aware parallel execution
- Automatic scaling and resilience

### 4. State Layer
**Event sourcing for complete auditability**
- SQLite event store with append-only writes
- Full replay capability
- Checkpoint system with compression
- 5 optimized indexes for performance

### 5. Orchestration Layer
**6-phase pipeline ensuring comprehensive execution**
- Phase 0: Big Bang (Interview → Seed)
- Phase 1: PAL Router (Cost optimization)
- Phase 2: Double Diamond (Discover → Define → Design → Deliver)
- Phase 3: Resilience (Lateral thinking)
- Phase 4: Evaluation (3-stage pipeline)
- Phase 5: Secondary Loop (TODO registry)

### 6. Presentation Layer
**Rich TUI interface with real-time visibility**
- Textual-based dashboard with live updates
- AC tree visualization with progress tracking
- Agent activity monitor
- Cost tracking and drift visualization
- Interactive debugging capabilities

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

The Big Bang phase transforms vague ideas into crystallized specifications through iterative questioning. **The seed is auto-generated at the end of this phase** — users do not need to author seeds manually in the normal flow.

**Components:**
- `bigbang/interview.py` — InterviewEngine for conducting Socratic interviews
- `bigbang/ambiguity.py` — Ambiguity score calculation
- `bigbang/seed_generator.py` — Seed generation from interview results

**Process:**
1. User provides initial context/idea (`ooo interview "..."` or `ouroboros interview "..."`)
2. Engine asks clarifying questions (up to MAX_INTERVIEW_ROUNDS)
3. Ambiguity score calculated after each response
4. Interview completes when ambiguity <= 0.2
5. Immutable Seed auto-generated and stored in `~/.ouroboros/seeds/`

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

For the current recursive execution flow, see [parallel_executor.py](../src/ouroboros/orchestrator/parallel_executor.py) and [runner.py](../src/ouroboros/orchestrator/runner.py).

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
   - Auto-detects project language from marker files (e.g., `uv.lock` → Python/uv, `Cargo.toml` → Rust, `go.mod` → Go, `package-lock.json` → Node). Supported: Python, Rust, Go, Zig, Node (npm/pnpm/bun/yarn).
   - Projects can override or extend commands via `.ouroboros/mechanical.toml`. Overrides are validated against an executable allowlist for security in CI/CD environments.
   - If no language is detected, Stage 1 checks are skipped and evaluation proceeds to Stage 2.
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

For the current evaluation flow, see [pipeline.py](../src/ouroboros/evaluation/pipeline.py) and [definitions.py](../src/ouroboros/mcp/tools/definitions.py).

For failure modes, error-handling guidance, and configuration reference, see the [Evaluation Pipeline Guide](./guides/evaluation-pipeline.md).

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
+-- orchestrator/   # Runtime abstraction and orchestration
|   +-- adapter.py     # AgentRuntime protocol, ClaudeAgentAdapter
|   +-- codex_cli_runtime.py  # CodexCliRuntime adapter
|   +-- runtime_factory.py    # create_agent_runtime() factory
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

The Seed is the "constitution" of a workflow — an immutable specification with:
- **Goal** — Primary objective
- **Constraints** — Hard requirements that must be satisfied
- **Acceptance Criteria** — Specific criteria for success
- **Ontology Schema** — Structure of workflow outputs
- **Exit Conditions** — When to terminate

**In the normal flow, seeds are auto-generated by the Socratic interview** (`ooo interview` / `ouroboros interview`). Most users never need to create or edit a seed manually — the interview handles crystallization automatically.

Once generated, the Seed cannot be modified (frozen Pydantic model).

> **Advanced:** For power users who want to hand-craft or edit seed YAML directly, see the [Seed Authoring Guide](guides/seed-authoring.md).

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

## Runtime Abstraction Layer

Ouroboros decouples workflow orchestration from the agent runtime that executes
tasks. The runtime abstraction layer allows different AI coding tools to serve
as runtime backends while the core engine (event sourcing, six-phase pipeline,
evaluation) remains unchanged.

### Architecture overview

```
                          ┌──────────────────────────┐
                          │   Orchestrator / Runner   │
                          │  (runtime-agnostic core)  │
                          └────────────┬─────────────┘
                                       │ uses AgentRuntime protocol
                          ┌────────────┴─────────────┐
                          │      RuntimeFactory       │
                          │  create_agent_runtime()   │
                          └────┬──────────┬──────┬───┘
                               │          │      │
              ┌────────────────┘          │      └────────────────┐
              ▼                           ▼                       ▼
  ┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
  │  ClaudeAgentAdapter │   │   CodexCliRuntime    │   │   (future adapter)  │
  │   backend="claude"  │   │   backend="codex"    │   │                     │
  │  session-oriented   │   │   session-oriented   │   │                     │
  └─────────────────────┘   └─────────────────────┘   └─────────────────────┘
```

> Both `ClaudeAgentAdapter` and `CodexCliRuntime` expose the same `AgentRuntime`
> protocol and provide equivalent session-oriented workflow capabilities.
> The orchestrator interacts with each backend exclusively through normalized
> `AgentMessage` / `RuntimeHandle` types — backend-specific communication
> details are fully encapsulated inside the adapters.

### Key abstractions

Every runtime adapter satisfies the `AgentRuntime` protocol (defined in `src/ouroboros/orchestrator/adapter.py`), which requires two methods: `execute_task()` (async streaming) and `execute_task_to_result()` (collected result).

| Type | Purpose |
|------|---------|
| `AgentMessage` | Normalized streaming message (assistant text, tool calls, results) |
| `RuntimeHandle` | Backend-neutral frozen dataclass for session resume/observe/terminate |
| `TaskResult` | Collected outcome of a completed task execution |

The orchestrator never inspects backend-specific internals — each adapter maps its native events into these shared types.

### Shipped adapters

- **`ClaudeAgentAdapter`** (`backend="claude"`) — Wraps Claude Agent SDK / Claude Code CLI with streaming, retry, and session resumption. Module: `src/ouroboros/orchestrator/adapter.py`
- **`CodexCliRuntime`** (`backend="codex"`) — Drives the OpenAI Codex CLI as a session-oriented runtime with NDJSON event parsing. Module: `src/ouroboros/orchestrator/codex_cli_runtime.py`

> Claude Code and Codex CLI have different tool sets, permission models, and streaming semantics. Ouroboros normalizes these differences at the adapter boundary, but feature parity is not guaranteed across runtimes.

### Runtime factory

`create_agent_runtime()` in `src/ouroboros/orchestrator/runtime_factory.py` resolves the backend name and returns the appropriate adapter. The backend can be set via:

1. `OUROBOROS_AGENT_RUNTIME` environment variable
2. `orchestrator.runtime_backend` in `~/.ouroboros/config.yaml`
3. Explicit `backend=` parameter

Accepted aliases: `claude` / `claude_code`, `codex` / `codex_cli`.

For API details, see the source in `src/ouroboros/orchestrator/adapter.py`. For contributing a new runtime adapter, see [Contributing](contributing/).

## Integration Points

### MCP (Model Context Protocol)

Ouroboros functions as a **bidirectional MCP Hub**:

- **Server mode** (`ouroboros mcp serve`) — Exposes tools (`ouroboros_execute_seed`, `ouroboros_session_status`, `ouroboros_query_events`) to Claude Desktop and other MCP clients
- **Client mode** (`ouroboros run --mcp-config mcp.yaml`) — Discovers and consumes tools from external MCP servers (filesystem, GitHub, databases, etc.), merged with built-in tools

Tool precedence: built-in tools win over MCP tools; first MCP server in config wins for duplicates.

### LiteLLM

All LLM calls go through LiteLLM for provider abstraction (100+ models), automatic retries, cost tracking, and streaming support.

## Design Principles

1. **Frugal First** - Start with the cheapest option, escalate only when needed
2. **Immutable Direction** - The Seed cannot change; only the path to achieve it adapts
3. **Progressive Verification** - Cheap checks first, expensive consensus only at gates
4. **Lateral Over Vertical** - When stuck, change perspective rather than try harder
5. **Event-Sourced** - Every state change is an event; nothing is lost

## Extension Points

- **Skills** — Add YAML-defined skills in `skills/` with magic prefix detection and tool declarations
- **Agents** — Add bundled specialist prompts in `src/ouroboros/agents/`; use `OUROBOROS_AGENTS_DIR` for explicit local overrides
- **MCP integration** — Bidirectional: expose Ouroboros tools as an MCP server, or consume external MCP servers during execution
- **Runtime adapters** — Implement the `AgentRuntime` protocol and register in the runtime factory

## Error Handling & Recovery

Ouroboros handles errors through four categories: validation errors (invalid seeds), execution errors (agent failures/timeouts), system errors (network/resource), and business errors (ambiguity > 0.2, stagnation). Recovery mechanisms include session replay from checkpoints, agent respawn, tier escalation, and persona switching.

## Configuration

For environment variables, `config.yaml` schema, and all configuration options, see **[config-reference.md](config-reference.md)**.

---

> For install instructions and first-run onboarding, see **[Getting Started](getting-started.md)**.
> For backend-specific configuration, see the [Claude Code](runtime-guides/claude-code.md) and [Codex CLI](runtime-guides/codex.md) runtime guides.
