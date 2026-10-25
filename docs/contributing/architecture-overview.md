# Architecture Overview for Contributors

This document explains how Ouroboros's components fit together so you can orient yourself quickly when working on any part of the codebase.

## High-Level Flow

```
CLI / MCP / skills
    |
    v
Authoring: bigbang/ and auto/ -> immutable Seed
    |
    v
Execution: orchestrator/ -> selected runtime backend
    |
    v
Verification: evaluation/ -> results and feedback
    |
    +--> refinement / evolution where the selected workflow supports it

State: persistence/ stores events; CLI and TUI observe them.
```

These are responsibility boundaries, not six mandatory execution phases.
Runtime selection follows the [runtime factory contract](../architecture.md#runtime-factory).

## Module Responsibility Map

Paths below are relative to `src/ouroboros/`. Start with the boundary that
owns the behavior being changed rather than adding another execution layer.

| Boundary | Packages | Responsibility |
|----------|----------|----------------|
| Entry points | `cli/`, `mcp/`, `skills/` | User commands, tool schemas, and skill dispatch |
| Authoring | `bigbang/`, `auto/`, `pm/` | Interactive requirements, bounded automatic interview, and product requirements |
| Execution | `orchestrator/` | Runtime adapters, scheduling, workflow execution, and session control |
| Backend selection | `backends/`, `providers/`, `router/`, `config/` | Capability registry, LLM adapters, model routing, and configuration |
| Verification | `evaluation/`, `resilience/` | Mechanical/semantic/consensus evaluation and recovery helpers |
| Shared contracts | `core/` | Seed, result/error types, and shared domain contracts |
| Durable state | `persistence/` | Events, checkpoints, and state reconstruction |
| Observation | `tui/` | Terminal views of execution state |

The runtime and LLM backend are distinct configuration surfaces. The registry
and factories decide supported capabilities; this map does not promise equal
features across adapters. See [The Development Loop](developing.md) for local
source selection, state locations, and the smallest validation for each boundary.

## Key Module Guide

### core/ -- Foundation Layer

**When to touch**: Adding new domain types, error categories, or modifying the Seed schema.

| File | Purpose |
|------|---------|
| `types.py` | `Result[T, E]` type, type aliases |
| `errors.py` | Error hierarchy (`ValidationError`, `ProviderError`, etc.) |
| `seed.py` | Immutable `Seed` Pydantic model -- the workflow "constitution" |
| `context.py` | Runtime workflow context |
| `ac_tree.py` | Acceptance criteria tree structure |
| `ontology_aspect.py` | AOP-based ontological analysis (`OntologicalAspect`, `AnalysisResult`) |
| `ontology_questions.py` | Centralized Socratic/ontological question engine |

### evaluation/ -- Three-Stage Pipeline

**When to touch**: Adding check types, modifying evaluation logic, changing consensus rules.

| File | Purpose |
|------|---------|
| `models.py` | Data models: `CheckType`, `CheckResult`, `SemanticResult`, `Vote`, `EvaluationResult` |
| `mechanical.py` | Stage 1: Shell-command checks (lint, test, build, static, coverage) |
| `semantic.py` | Stage 2: LLM-based evaluation (AC compliance, drift, goal alignment) |
| `consensus.py` | Stage 3: Multi-model voting + deliberative consensus (Advocate/Devil/Judge) |
| `trigger.py` | Trigger matrix: 6 conditions that escalate to Stage 3 |
| `pipeline.py` | Orchestrator: runs stages sequentially, respects config and triggers |

**Data flow**: `EvaluationContext` -> `MechanicalVerifier` -> `SemanticEvaluator` -> `ConsensusTrigger` -> `ConsensusEvaluator` -> `EvaluationResult`

### orchestrator/ -- Runtime Abstraction and Orchestration

**When to touch**: Modifying execution behavior, parallel scheduling, strategy patterns.

| File | Purpose |
|------|---------|
| `adapter.py` | `ClaudeAgentAdapter` -- wraps Claude Agent SDK (one of several runtime adapters) |
| `runner.py` | `OrchestratorRunner` -- main execution loop, AC iteration |
| `parallel_executor.py` | Parallel AC execution with dependency analysis |
| `execution_strategy.py` | `ExecutionStrategy` protocol + Code/Research/Analysis implementations |
| `level_context.py` | Inter-level context passing for parallel execution |
| `workflow_state.py` | TUI activity tracking during execution |

### tui/ -- Terminal User Interface

**When to touch**: Adding widgets, screens, or modifying event handling.

| File | Purpose |
|------|---------|
| `app.py` | `OuroborosTUI` main app -- screen management, event subscription |
| `events.py` | `TUIState` dataclass, message types, `create_message_from_event()` |
| `screens/dashboard_v3.py` | Active dashboard: Double Diamond bar + AC tree + node detail |
| `screens/logs.py` | Log viewer with level filtering |
| `screens/execution.py` | Execution timeline and details |
| `screens/debug.py` | State inspector and raw events |
| `widgets/` | Reusable widgets: `ac_tree`, `drift_meter`, `cost_tracker`, etc. |

**State flow**: `EventStore` --> `app._subscribe_to_events()` (0.5s poll) --> `create_message_from_event()` --> `post_message()` --> screen handlers

**Key rule**: `app.py` owns `_state.ac_tree` as the single source of truth. Dashboard renders from app state.

### providers/ -- LLM Abstraction

| File | Purpose |
|------|---------|
| `base.py` | `LLMAdapter` protocol, `CompletionConfig`, `Message` |
| `litellm_adapter.py` | LiteLLM implementation (100+ model support) |

### persistence/ -- Event Sourcing

| File | Purpose |
|------|---------|
| `event_store.py` | `EventStore` -- append-only event storage (SQLite) |
| `checkpoint.py` | Checkpoint/recovery for session resumption |
| `schema.py` | Database schema definitions |

## How the Boundaries Connect

### Authoring -> execution: Seed drives execution

The `Seed` object records the authoring result for execution. The runner selects an execution strategy/profile from `seed.task_type`, and `seed.acceptance_criteria` become execution tree nodes. Model routing is not a mandatory intermediate phase between authoring and execution.

### Execution -> verification: artifacts are evaluated

Each AC execution produces an artifact (code or document). The `EvaluationContext` wraps this artifact with the AC text, goal, and constraints for the pipeline to evaluate.

### Verification -> recovery: workflow-specific feedback

Evaluation results feed the selected workflow's recovery or refinement policy. The `resilience/` package provides stagnation and persona helpers; not every CLI command runs the same recovery loop.

### Orchestrator ties it together

`OrchestratorRunner` in `orchestrator/runner.py` is the main loop that:
1. Loads the Seed
2. Gets the ExecutionStrategy for `seed.task_type`
3. Iterates over ACs (parallel or sequential)
4. Calls the configured runtime backend (e.g., `ClaudeAgentAdapter`, `CodexCLIRuntime`)
5. Collects results and emits events to `EventStore`
6. TUI picks up events via polling

## Adding a New Feature: Checklist

1. **Identify the module**: Use the module guide above
2. **Read existing code**: Understand the patterns before changing
3. **Add types first**: Define data models in the appropriate `models.py`
4. **Implement logic**: Follow existing patterns (Result type, frozen dataclasses)
5. **Write tests**: Unit tests in `tests/unit/<module>/`
6. **Update exports**: Add to `__init__.py` and `__all__`
7. **Run full suite**: `uv run pytest tests/unit/ -v`
