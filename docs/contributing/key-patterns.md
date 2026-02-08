# Key Patterns

Core patterns used throughout the Ouroboros codebase. Understanding these will help you write idiomatic contributions.

## 1. Result Type for Error Handling

Ouroboros uses `Result[T, E]` instead of exceptions for expected failures. Exceptions are reserved for programming errors (bugs).

**Location**: `src/ouroboros/core/types.py`

```python
from ouroboros.core.types import Result
from ouroboros.core.errors import ValidationError

# Returning success
def validate_score(score: float) -> Result[float, ValidationError]:
    if 0.0 <= score <= 1.0:
        return Result.ok(score)
    return Result.err(ValidationError(f"Score {score} out of range"))

# Consuming results
result = validate_score(0.85)
if result.is_ok:
    process(result.value)
else:
    log_error(result.error.message)

# Chaining
result = (
    validate_score(0.85)
    .map(lambda s: s * 100)           # Transform Ok value
    .map_err(lambda e: str(e))        # Transform Err value
)

# FlatMap / bind
result = validate_score(0.85).and_then(lambda s: further_validation(s))
```

**Rules**:
- Functions that can fail due to external factors (LLM calls, file I/O, validation) return `Result`
- Functions that should never fail (pure transforms, property access) raise exceptions on bugs
- Always check `is_ok` or `is_err` before accessing `.value` or `.error`

## 2. Frozen Dataclasses for Immutability

All data models are frozen (immutable after construction) for thread safety and predictability.

```python
from dataclasses import dataclass, field

@dataclass(frozen=True, slots=True)
class CheckResult:
    check_type: CheckType
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
```

**Pydantic models** use `frozen=True` for the same effect:

```python
from pydantic import BaseModel, Field

class Seed(BaseModel, frozen=True):
    goal: str = Field(..., min_length=1)
    constraints: tuple[str, ...] = Field(default_factory=tuple)
```

**Rules**:
- Use `frozen=True, slots=True` on dataclasses
- Use `frozen=True` on Pydantic BaseModel
- Use `tuple[...]` instead of `list[...]` for frozen collections
- If you need to "modify", create a new instance with changed values

## 3. Event Sourcing

All state changes are recorded as immutable events. The `EventStore` is append-only.

**Location**: `src/ouroboros/events/`, `src/ouroboros/persistence/event_store.py`

```python
from ouroboros.events.base import BaseEvent

# Events are created by factory functions
event = create_stage1_completed_event(
    execution_id="exec_123",
    passed=True,
    checks=[...],
    coverage_score=0.85,
)

# Events are appended to the store
await event_store.append(event)

# Events are replayed for state reconstruction
events = await event_store.replay(aggregate_id="exec_123")
```

**Rules**:
- Events are immutable (`BaseEvent` is frozen)
- Never modify events after creation
- Use factory functions to create events (they set timestamps, IDs, etc.)
- The TUI polls the EventStore to update its display

## 4. Protocol Classes for Pluggable Strategies

Use `Protocol` (not abstract base classes) for defining interfaces that multiple implementations satisfy.

**Location**: `src/ouroboros/orchestrator/execution_strategy.py`

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ExecutionStrategy(Protocol):
    def get_tools(self) -> list[str]: ...
    def get_system_prompt_fragment(self) -> str: ...
    def get_task_prompt_suffix(self) -> str: ...
    def get_activity_map(self) -> dict[str, ActivityType]: ...

class CodeStrategy:
    """Satisfies ExecutionStrategy implicitly -- no inheritance needed."""
    def get_tools(self) -> list[str]:
        return ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    # ... other methods
```

**Registration**:
```python
# Built-in strategies
_STRATEGY_REGISTRY: dict[str, ExecutionStrategy] = {
    "code": CodeStrategy(),
    "research": ResearchStrategy(),
    "analysis": AnalysisStrategy(),
}

# Custom strategies
register_strategy("custom", MyCustomStrategy())
```

**Rules**:
- Use `Protocol` for interfaces, not `ABC`
- Add `@runtime_checkable` if you need `isinstance()` checks
- Implementations satisfy the protocol by duck typing (no `class Foo(ExecutionStrategy)`)
- Register in the module-level registry

## 5. Three-Stage Evaluation Pipeline

Every artifact goes through up to three evaluation stages:

```
Artifact --> Stage 1: Mechanical ($0)     -- lint, build, test, static, coverage
         --> Stage 2: Semantic ($$)       -- LLM evaluates AC compliance + drift
         --> Stage 3: Consensus ($$$$)    -- multi-model vote (only if triggered)
```

**Key classes**:
- `EvaluationPipeline` orchestrates the stages
- `MechanicalVerifier` runs shell commands
- `SemanticEvaluator` calls LLM for semantic analysis
- `ConsensusEvaluator` / `DeliberativeConsensus` runs multi-model voting
- `ConsensusTrigger` decides whether Stage 3 is needed (6 trigger conditions)

**Stage 3 triggers**:
1. Seed modification
2. Ontology evolution
3. Goal reinterpretation
4. Seed drift > 0.3
5. Stage 2 uncertainty > 0.3
6. Lateral thinking adoption

## 6. TUI State Management

The TUI uses Textual's reactive system with a strict single source of truth.

**Location**: `src/ouroboros/tui/app.py`, `src/ouroboros/tui/events.py`

```python
# TUIState is the SSOT -- owned by app.py
@dataclass
class TUIState:
    current_phase: str = ""
    ac_tree: dict[str, Any] = field(default_factory=dict)
    drift_score: float = 0.0
    total_cost: float = 0.0
    # ...

# Events from EventStore are converted to Textual messages
message = create_message_from_event(event)
app.post_message(message)

# Screens handle messages to update their display
class DashboardScreenV3(Screen):
    def on_ac_updated(self, message: ACUpdated) -> None:
        self._update_tree_node(message.ac_id, message.status)
```

**Rules**:
- `app.py` owns `_state` -- screens read from it
- Do not mutate Textual reactive dicts in-place then reassign the same reference
- Use `is not None` (not truthiness) for index checks (falsy-0 pitfall)
- DashboardScreenV3 uses `_tree` (SelectableACTree), not `_ac_tree` (legacy widget)

## 7. Seed Immutability

The `Seed` is frozen after creation. Direction fields (goal, constraints, acceptance_criteria) are immutable.

```python
seed = Seed(
    goal="Build a CLI tool",
    constraints=("Python 3.14+",),
    acceptance_criteria=("Create main.py",),
    ontology_schema=OntologySchema(name="CLI", description="CLI tool"),
    metadata=SeedMetadata(ambiguity_score=0.15),
)

# This raises an error:
seed.goal = "Different goal"  # ValidationError: frozen
```

**Rules**:
- Never attempt to modify a Seed
- The ontology can evolve with consensus, but direction fields cannot
- Use `seed.to_dict()` / `Seed.from_dict()` for serialization

## Summary Table

| Pattern | When to Use | Key File |
|---------|-------------|----------|
| `Result[T, E]` | Any function that can fail | `core/types.py` |
| Frozen dataclass | All data models | Throughout |
| Event sourcing | State changes | `persistence/event_store.py` |
| Protocol classes | Pluggable interfaces | `orchestrator/execution_strategy.py` |
| Three-stage eval | Artifact verification | `evaluation/pipeline.py` |
| TUI SSOT | UI state management | `tui/app.py`, `tui/events.py` |
| Seed immutability | Workflow specification | `core/seed.py` |
