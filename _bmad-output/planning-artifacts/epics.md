---
stepsCompleted: [1, 2]
status: 'in-progress'
inputDocuments:
  - requirement/1_EXECUTIVE_SUMMARY.md
  - requirement/2_FULL_SPECIFICATION.md
  - _bmad-output/planning-artifacts/architecture.md
workflowType: 'epics-and-stories'
project_name: 'Ouroboros'
user_name: 'Jaegyu.lee'
date: '2026-01-14'
---

# Ouroboros - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for Ouroboros, a self-improving AI workflow system. The epics are organized around **developer value**, not technical layers, ensuring each epic delivers meaningful, standalone functionality.

---

## Requirements Inventory

### Functional Requirements

```
FR1: Phase 0 - Big Bang allows Seed creation only when Ambiguity Score ≤ 0.2
FR2: Interview Protocol for user requirement clarification (max 10 rounds)
FR3: Immutable Seed generation (goal, constraints, acceptanceCriteria, ontologySchema, evaluationPrinciples, exitConditions)
FR4: PAL Router - 3-Tier routing (Frugal 1x, Standard 10x, Frontier 30x)
FR5: Complexity-based automatic Tier selection (< 0.4 → Frugal, 0.4-0.7 → Standard, > 0.7 → Frontier)
FR6: Escalation to higher Tier after 2 consecutive failures
FR7: Downgrade to lower Tier after 5 consecutive successes
FR8: Double Diamond execution cycle (Discover → Define → Design → Deliver)
FR9: Hierarchical Acceptance Criteria decomposition (until Atomic)
FR10: Stagnation Detection - 4 patterns (Spinning, Oscillation, No Drift, Diminishing Returns)
FR11: Lateral Thinking - 5 persona switching (Hacker, Researcher, Simplifier, Architect, Contrarian)
FR12: SubAgent Isolation - prevent main context pollution
FR13: Stage 1 Mechanical Verification - Lint, Build, Test, Static Analysis ($0)
FR14: Stage 2 Semantic Evaluation - AC compliance, Goal alignment, Drift measurement (Standard Tier)
FR15: Stage 3 Consensus - 3-model multi-agreement, 2/3 majority (Frontier Tier)
FR16: Consensus Trigger Matrix - 6 trigger conditions implementation
FR17: TODO Registry - deferred processing of discovered improvements
FR18: Secondary Loop - TODO batch processing after Primary Goal achieved
FR19: Checkpoint Persistence - after each node completion + every 5 minutes
FR20: Context Compression - compress context exceeding 6 hours
FR21: Recovery Protocol - load latest checkpoint, rollback on corruption
FR22: Drift Measurement - calculate Goal/Constraint/Ontology drift
FR23: Retrospective - direction check every 3 iterations
```

### Non-Functional Requirements

```
NFR1: Cost Efficiency - 80%+ tasks processed at 1x Cost (Frugal Tier)
NFR2: Resilience - Zero-stop operation, break through stagnation with Lateral Thinking fallback
NFR3: Persistence - SQLite default, PostgreSQL optional, checkpoint-based recovery
NFR4: Multi-Provider - OpenAI, Anthropic, Google abstraction (LiteLLM + OpenRouter)
NFR5: Drift Control - continuous drift measurement, maintain ≤0.3 threshold
NFR6: Ambiguity Threshold - execution allowed only at ≤0.2
NFR7: Context Limit - max 100,000 tokens, compress context over 6 hours
NFR8: Consensus Timeout - individual model timeout, sequential retry on partial failure
NFR9: Coverage Threshold - Stage 1 test coverage ≥0.7
NFR10: AC Depth Limit - max 5 levels, compression applied at depth 3+
NFR11: Max Rollback Depth - checkpoint rollback max 3 levels
NFR12: Observability - cost tracking (1x/10x/30x units), drift metrics, stagnation signals
```

### Additional Requirements (from Architecture)

```
- Starter Template: uv init --package ouroboros --python 3.14 (implement in Epic 0 Story 1)
- SQLAlchemy Core (Query Builder, no ORM)
- Event Sourcing pattern - single events table, aggregate_type classification
- LiteLLM v1.80.15 + OpenRouter integration
- Result Type pattern - use Result<T, E> instead of exceptions for expected failures
- stamina v25.1.0 - Exponential backoff with jitter
- structlog - Structured JSON logging
- Rich - CLI output (Progress bar, Tables, Panels)
- contextvars - Cross-async context propagation
- UnitOfWork pattern - phase-based checkpointing
- Layered Architecture - CLI → Application → Domain → Infrastructure
- No direct imports between domain packages (orchestration via ExecutionEngine)
- PEP 8 Strict + dot.notation.past_tense event naming
- Python 3.14 features: Free-threaded mode, Template strings, Deferred annotations
- ~/.ouroboros/ directory - config.yaml, credentials.yaml, data/, logs/
```

---

## FR Coverage Map

| FR | Epic | Description |
|----|------|-------------|
| FR1 | Epic 1 | Big Bang Ambiguity Gate ≤0.2 |
| FR2 | Epic 1 | Interview Protocol (max 10 rounds) |
| FR3 | Epic 1 | Immutable Seed generation |
| FR4 | Epic 2 | PAL 3-Tier routing |
| FR5 | Epic 2 | Complexity-based tier selection |
| FR6 | Epic 2 | Escalation on 2 failures |
| FR7 | Epic 2 | Downgrade on 5 successes |
| FR8 | Epic 3 | Double Diamond cycle |
| FR9 | Epic 3 | Hierarchical AC decomposition |
| FR10 | Epic 4 | Stagnation Detection (4 patterns) |
| FR11 | Epic 4 | Lateral Thinking (5 personas) |
| FR12 | Epic 3 | SubAgent Isolation |
| FR13 | Epic 5 | Mechanical Verification ($0) |
| FR14 | Epic 5 | Semantic Evaluation |
| FR15 | Epic 5 | Consensus (3 models, 2/3 majority) |
| FR16 | Epic 5 | Consensus Trigger Matrix |
| FR17 | Epic 7 | TODO Registry |
| FR18 | Epic 7 | Secondary Loop batch processing |
| FR19 | Epic 0 | Checkpoint Persistence |
| FR20 | Epic 0 | Context Compression |
| FR21 | Epic 0 | Recovery Protocol |
| FR22 | Epic 6 | Drift Measurement |
| FR23 | Epic 6 | Retrospective every 3 iterations |

---

## Epic List

### Epic 0: Project Foundation & Infrastructure
Developers can install and configure Ouroboros as a working CLI tool with persistence and observability.
**FRs covered:** FR19, FR20, FR21
**NFRs covered:** NFR3, NFR12

### Epic 1: Seed Creation via Big Bang
Developers can create clear, unambiguous Seed specifications through an interactive interview process.
**FRs covered:** FR1, FR2, FR3
**NFRs covered:** NFR6

### Epic 2: Intelligent Task Routing (PAL)
Developers benefit from cost-optimized LLM routing that automatically selects the right model tier.
**FRs covered:** FR4, FR5, FR6, FR7
**NFRs covered:** NFR1, NFR4

### Epic 3: Double Diamond Execution
Developers can execute complex tasks through recursive decomposition with structured diverge-converge cycles.
**FRs covered:** FR8, FR9, FR12
**NFRs covered:** NFR10

### Epic 4: Resilience & Stagnation Recovery
Developers experience zero-stop operation where the system automatically recovers from stagnation.
**FRs covered:** FR10, FR11
**NFRs covered:** NFR2

### Epic 5: Three-Stage Evaluation Pipeline
Developers get rigorous verification of all outputs through mechanical, semantic, and consensus stages.
**FRs covered:** FR13, FR14, FR15, FR16
**NFRs covered:** NFR9

### Epic 6: Drift Control & Retrospective
Developers maintain goal alignment through continuous drift measurement and periodic retrospectives.
**FRs covered:** FR22, FR23
**NFRs covered:** NFR5

### Epic 7: Secondary Loop & TODO Processing
Developers can defer non-critical improvements and process them in batches after primary goals are met.
**FRs covered:** FR17, FR18

---

## Epic 0: Project Foundation & Infrastructure

**Goal:** Developers can install and configure Ouroboros as a working CLI tool with persistence, observability, and LLM provider integration.

**User Outcome:** After `pip install ouroboros`, developers can run `ouroboros --help`, initialize configuration, and have a working database with event sourcing.

### Story 0.1: Project Initialization with uv

As a developer,
I want to initialize the Ouroboros project with proper Python 3.14 packaging,
So that I have a clean, modern project structure ready for development.

**Acceptance Criteria:**

**Given** a clean directory
**When** I run `uv init --package ouroboros --python 3.14`
**Then** the project structure is created with src/ouroboros/ layout
**And** pyproject.toml is configured with all dependencies from Architecture doc
**And** .python-version is set to 3.14

---

### Story 0.2: Core Types and Error Handling

As a developer,
I want a consistent Result type and error hierarchy,
So that all components handle errors uniformly without exceptions for expected failures.

**Acceptance Criteria:**

**Given** the core/types.py module
**When** I use Result[T, E] for operations that can fail
**Then** I can pattern match on is_ok/is_err without try/catch
**And** OuroborosError hierarchy provides specific error types

---

### Story 0.3: Event Store with SQLAlchemy Core

As a developer,
I want an event sourcing infrastructure,
So that all state changes are captured as immutable events for replay and debugging.

**Acceptance Criteria:**

**Given** the persistence/event_store.py module
**When** I append events with aggregate_type and aggregate_id
**Then** events are stored in a single unified events table
**And** I can replay events to reconstruct state
**And** async operations use aiosqlite

---

### Story 0.4: Configuration and Credentials Management

As a developer,
I want to configure Ouroboros via ~/.ouroboros/ directory,
So that API keys and settings are stored securely and persistently.

**Acceptance Criteria:**

**Given** the config/loader.py module
**When** I run `ouroboros config init`
**Then** ~/.ouroboros/ directory is created with config.yaml and credentials.yaml templates
**And** credentials.yaml has chmod 600 permissions
**And** config validates against Pydantic models

---

### Story 0.5: LLM Provider Adapter with LiteLLM

As a developer,
I want a unified LLM adapter that works with multiple providers,
So that I can switch between OpenAI, Anthropic, and Google models seamlessly.

**Acceptance Criteria:**

**Given** the providers/litellm_adapter.py module
**When** I call adapter.complete() with any supported model
**Then** the request is routed through LiteLLM to the correct provider
**And** responses are wrapped in Result type
**And** stamina handles retries with exponential backoff

---

### Story 0.6: CLI Skeleton with Typer and Rich

As a developer,
I want a beautiful CLI interface with progress indicators,
So that I have clear feedback during long-running operations.

**Acceptance Criteria:**

**Given** the cli/main.py module
**When** I run `ouroboros --help`
**Then** I see formatted help text with all available commands
**And** Rich progress bars show during async operations
**And** Tables display structured data clearly

---

### Story 0.7: Structured Logging with structlog

As a developer,
I want structured JSON logging,
So that I can analyze logs programmatically and debug issues effectively.

**Acceptance Criteria:**

**Given** the observability/logging.py module
**When** log events are emitted
**Then** they include timestamp, level, and context from contextvars
**And** dev mode shows human-readable console output
**And** production mode outputs JSON

---

### Story 0.8: Checkpoint and Recovery System

As a developer,
I want automatic checkpointing and recovery,
So that long-running workflows can resume after interruption.

**Acceptance Criteria:**

**Given** an active workflow execution
**When** a checkpoint is triggered (node completion or 5-minute interval)
**Then** current state is persisted to checkpoint store
**And** on restart, the latest valid checkpoint is loaded
**And** corrupted checkpoints trigger rollback (max 3 levels)

---

### Story 0.9: Context Compression Engine

As a developer,
I want automatic context compression for long-running workflows,
So that token limits are respected and costs are optimized.

**Acceptance Criteria:**

**Given** a workflow context exceeding 6 hours or 100,000 tokens
**When** context compression is triggered
**Then** historical context is summarized while preserving key facts
**And** the compressed context stays within NFR7 limits (100,000 tokens)
**And** critical information (Seed, current AC, recent history) is preserved
**And** compression events are logged with before/after token counts

---

## Epic 1: Seed Creation via Big Bang

**Goal:** Developers can create clear, unambiguous Seed specifications through an interactive interview process that ensures requirement clarity before execution.

**User Outcome:** Running `ouroboros init` starts an interview that produces a valid Seed YAML file with Ambiguity Score ≤ 0.2.

### Story 1.1: Interview Protocol Engine

As a developer,
I want an interactive interview process,
So that my vague ideas are refined into clear, executable requirements.

**Acceptance Criteria:**

**Given** I run `ouroboros init`
**When** the interview begins
**Then** I am asked clarifying questions in rounds (max 10)
**And** each round focuses on reducing ambiguity
**And** I can provide context and examples

---

### Story 1.2: Ambiguity Score Calculation

As a developer,
I want automatic ambiguity measurement,
So that I know when my requirements are clear enough to proceed.

**Acceptance Criteria:**

**Given** my responses during the interview
**When** ambiguity is calculated
**Then** the score reflects clarity of goals, constraints, and success criteria
**And** scores > 0.2 trigger additional clarification questions
**And** the calculation method is transparent and explainable

---

### Story 1.3: Immutable Seed Generation

As a developer,
I want a validated Seed YAML file,
So that I have a clear, immutable specification for execution.

**Acceptance Criteria:**

**Given** Ambiguity Score ≤ 0.2
**When** the Seed is generated
**Then** it contains goal, constraints, acceptanceCriteria, ontologySchema, evaluationPrinciples, exitConditions
**And** the Seed is validated against the schema
**And** the Seed file is saved to the specified location

---

## Epic 2: Intelligent Task Routing (PAL)

**Goal:** Developers benefit from cost-optimized LLM routing that automatically selects the right model tier based on task complexity, with intelligent escalation and downgrade.

**User Outcome:** 80%+ of tasks run on the cheapest Frugal tier, with automatic escalation only when necessary.

### Story 2.1: Three-Tier Model Configuration

As a developer,
I want configurable model tiers,
So that I can define which models to use at each cost level.

**Acceptance Criteria:**

**Given** the routing/tiers.py module
**When** I configure tiers in config.yaml
**Then** Frugal (1x), Standard (10x), and Frontier (30x) tiers are available
**And** each tier maps to specific model identifiers
**And** cost multipliers are tracked for reporting

---

### Story 2.2: Complexity-Based Routing

As a developer,
I want automatic tier selection based on task complexity,
So that simple tasks don't waste expensive model calls.

**Acceptance Criteria:**

**Given** a task to be routed
**When** complexity is estimated
**Then** complexity < 0.4 routes to Frugal
**And** complexity 0.4-0.7 routes to Standard
**And** complexity > 0.7 routes to Frontier

---

### Story 2.3: Escalation on Failure

As a developer,
I want automatic escalation when tasks fail,
So that difficult tasks eventually get the power they need.

**Acceptance Criteria:**

**Given** a task running on a tier
**When** 2 consecutive failures occur
**Then** the task is escalated to the next higher tier
**And** escalation events are logged with cost impact
**And** Frontier tier failures trigger lateral thinking

---

### Story 2.4: Downgrade on Success

As a developer,
I want automatic tier downgrade after sustained success,
So that we continuously optimize for cost efficiency.

**Acceptance Criteria:**

**Given** a task pattern running on a tier
**When** 5 consecutive successes occur
**Then** similar tasks are downgraded to the next lower tier
**And** downgrade decisions are logged
**And** Frugal tier tasks remain at Frugal

---

## Epic 3: Double Diamond Execution

**Goal:** Developers can execute complex tasks through the recursive Double Diamond pattern with structured diverge-converge cycles and hierarchical AC decomposition.

**User Outcome:** `ouroboros run seed.yaml` decomposes acceptance criteria into atomic tasks and executes them systematically.

### Story 3.1: Double Diamond Cycle Implementation

As a developer,
I want the Double Diamond execution pattern,
So that each task goes through structured Discover → Define → Design → Deliver phases.

**Acceptance Criteria:**

**Given** an acceptance criterion to execute
**When** the Double Diamond cycle runs
**Then** Discover phase explores the problem space
**And** Define phase converges on the approach
**And** Design phase creates the solution
**And** Deliver phase implements and validates

---

### Story 3.2: Hierarchical AC Decomposition

As a developer,
I want automatic decomposition of complex ACs,
So that large tasks are broken into manageable atomic units.

**Acceptance Criteria:**

**Given** a non-atomic acceptance criterion
**When** decomposition is triggered at Define phase
**Then** the AC is split into child ACs
**And** each child AC runs its own Double Diamond
**And** decomposition continues until atomic (max depth 5)

---

### Story 3.3: Atomicity Detection

As a developer,
I want clear atomicity criteria,
So that decomposition stops at the right granularity.

**Acceptance Criteria:**

**Given** an acceptance criterion
**When** atomicity is evaluated
**Then** complexity below threshold indicates atomic
**And** single-tool solvability indicates atomic
**And** duration below limit indicates atomic

---

### Story 3.4: SubAgent Isolation

As a developer,
I want isolated execution contexts for subtasks,
So that SubAgent failures don't pollute the main context.

**Acceptance Criteria:**

**Given** a subtask execution
**When** a SubAgent is spawned
**Then** it receives filtered context (seed_summary, current_ac, recent_history, key_facts)
**And** main context is not modified by SubAgent actions
**And** SubAgent results are validated before integration

---

## Epic 4: Resilience & Stagnation Recovery

**Goal:** Developers experience zero-stop operation where the system automatically detects stagnation and recovers through lateral thinking strategies.

**User Outcome:** When workflows get stuck, Ouroboros automatically tries different approaches until progress resumes.

### Story 4.1: Stagnation Detection (4 Patterns)

As a developer,
I want automatic stagnation detection,
So that stuck workflows are identified early.

**Acceptance Criteria:**

**Given** an ongoing execution
**When** stagnation patterns are monitored
**Then** Spinning (same output repeated) is detected
**And** Oscillation (A→B→A→B) is detected
**And** No Drift (no progress toward goal) is detected
**And** Diminishing Returns (progress slowing) is detected

---

### Story 4.2: Lateral Thinking Personas

As a developer,
I want alternative thinking strategies,
So that stagnation is broken through creative approaches.

**Acceptance Criteria:**

**Given** stagnation is detected
**When** lateral thinking is triggered
**Then** Hacker persona tries unconventional solutions
**And** Researcher persona seeks additional information
**And** Simplifier persona reduces complexity
**And** Architect persona restructures the approach
**And** Contrarian persona challenges assumptions

---

### Story 4.3: Persona Rotation Strategy

As a developer,
I want intelligent persona selection,
So that the most appropriate lateral thinking approach is tried first.

**Acceptance Criteria:**

**Given** a stagnation trigger
**When** persona is selected
**Then** selection considers the stagnation pattern type
**And** previously failed personas are deprioritized
**And** rotation continues until progress or all personas exhausted

---

## Epic 5: Three-Stage Evaluation Pipeline

**Goal:** Developers get rigorous verification of all outputs through a progressive evaluation pipeline that balances cost and thoroughness.

**User Outcome:** All results pass through mechanical checks ($0), semantic evaluation, and consensus voting when needed.

### Story 5.1: Stage 1 - Mechanical Verification

As a developer,
I want zero-cost mechanical checks,
So that obvious issues are caught before expensive LLM evaluation.

**Acceptance Criteria:**

**Given** a generated artifact
**When** Stage 1 evaluation runs
**Then** lint checks are applied
**And** build validation is performed
**And** tests are executed
**And** static analysis runs
**And** coverage threshold ≥ 0.7 is verified

---

### Story 5.2: Stage 2 - Semantic Evaluation

As a developer,
I want LLM-based semantic evaluation,
So that outputs are verified for goal alignment and AC compliance.

**Acceptance Criteria:**

**Given** an artifact passing Stage 1
**When** Stage 2 evaluation runs (Standard tier)
**Then** AC compliance is verified
**And** goal alignment is scored
**And** drift from original intent is measured

---

### Story 5.3: Stage 3 - Multi-Model Consensus

As a developer,
I want multi-model consensus for critical decisions,
So that important outputs have diverse verification.

**Acceptance Criteria:**

**Given** a consensus trigger condition
**When** Stage 3 evaluation runs (Frontier tier)
**Then** 3 different models evaluate the output
**And** 2/3 majority agreement is required
**And** disagreements are logged with reasoning

---

### Story 5.4: Consensus Trigger Matrix

As a developer,
I want clear rules for when consensus is required,
So that expensive multi-model evaluation is used appropriately.

**Acceptance Criteria:**

**Given** the consensus trigger matrix
**When** a trigger condition is met
**Then** Seed modification triggers consensus
**And** Ontology evolution triggers consensus
**And** Goal interpretation changes trigger consensus
**And** Seed Drift Alert (drift > 0.3) triggers consensus
**And** Stage 2 Uncertainty (> 0.3) triggers consensus
**And** Lateral Thinking Adoption triggers consensus

---

## Epic 6: Drift Control & Retrospective

**Goal:** Developers maintain goal alignment through continuous drift measurement and periodic retrospectives that catch direction errors early.

**User Outcome:** Drift stays below 0.3 threshold, with automatic course correction every 3 iterations.

### Story 6.1: Drift Measurement Engine

As a developer,
I want continuous drift calculation,
So that deviation from original goals is quantified.

**Acceptance Criteria:**

**Given** ongoing execution
**When** drift is measured
**Then** Goal drift captures deviation from stated objectives
**And** Constraint drift captures constraint violations
**And** Ontology drift captures concept evolution
**And** combined drift threshold is ≤ 0.3

---

### Story 6.2: Automatic Retrospective

As a developer,
I want periodic direction checks,
So that accumulated drift is caught and corrected.

**Acceptance Criteria:**

**Given** every 3 iterations
**When** retrospective triggers
**Then** current state is compared to original Seed
**And** drift components are analyzed
**And** course correction recommendations are generated
**And** high drift triggers human notification

---

## Epic 7: Secondary Loop & TODO Processing

**Goal:** Developers can defer non-critical improvements discovered during execution and process them efficiently in batches after primary goals are met.

**User Outcome:** Improvements don't derail primary work, and are automatically addressed afterward.

### Story 7.1: TODO Registry

As a developer,
I want to capture discovered improvements without disrupting flow,
So that good ideas aren't lost but don't derail primary work.

**Acceptance Criteria:**

**Given** an improvement is discovered during execution
**When** it's registered as a TODO
**Then** it's stored with context and priority
**And** it doesn't interrupt primary execution
**And** it's visible in status reports

---

### Story 7.2: Secondary Loop Batch Processing

As a developer,
I want automatic TODO processing after primary goals,
So that improvements are implemented efficiently.

**Acceptance Criteria:**

**Given** primary goal is achieved
**When** secondary loop activates
**Then** TODOs are processed in priority order
**And** each TODO runs through appropriate execution path
**And** batch processing optimizes for efficiency

---

## Epic Summary

| Epic | Title | Stories | FRs | Key Value |
|------|-------|---------|-----|-----------|
| 0 | Project Foundation & Infrastructure | 9 | 3 | Installable, configurable CLI |
| 1 | Seed Creation via Big Bang | 3 | 3 | Clear requirement specification |
| 2 | Intelligent Task Routing (PAL) | 4 | 4 | Cost-optimized LLM usage |
| 3 | Double Diamond Execution | 4 | 3 | Recursive task decomposition |
| 4 | Resilience & Stagnation Recovery | 3 | 2 | Zero-stop operation |
| 5 | Three-Stage Evaluation Pipeline | 4 | 4 | Rigorous verification |
| 6 | Drift Control & Retrospective | 2 | 2 | Goal alignment |
| 7 | Secondary Loop & TODO Processing | 2 | 2 | Deferred improvements |
| **Total** | | **31** | **23** | |

---

## Dependency Flow

```
Epic 0 (Foundation)
    ↓
Epic 1 (Seed) ──→ Epic 2 (Routing) ──→ Epic 3 (Execution)
                                            ↓
                                      Epic 4 (Resilience)
                                            ↓
                                      Epic 5 (Evaluation)
                                            ↓
                                      Epic 6 (Drift) ──→ Epic 7 (Secondary)
```

Each Epic provides **complete, standalone functionality** while building upon previous Epics. No Epic requires a future Epic to function.

---

_Generated: 2026-01-14_
_Status: In Progress_
