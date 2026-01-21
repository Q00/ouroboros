# Ouroboros Project Context

> AI Agent Implementation Guide - Read this BEFORE writing any code.

## Core Philosophy

> "Code is read more often than written. Optimize for cognitive load, not keystrokes."

**Ouroboros** is a self-improving AI workflow system with 6 phases:
1. Big Bang (Clarification)
2. PAL Router (Tiered Routing)
3. Execution (Double Diamond)
4. Resilience (Stagnation Detection)
5. Evaluation (3-Stage Pipeline)
6. Consensus (Multi-Model Voting)

---

## Critical Rules

### Python & Async

| Rule | Details |
|------|---------|
| **Version** | Python 3.14+ required |
| **Async I/O** | ALL I/O operations MUST be `async def` |
| **Sync CPU** | CPU-bound operations (parsing, validation) stay sync |
| **Blocking** | Use `asyncio.to_thread()` for blocking in async context |
| **Event Loop** | NEVER block the event loop |

```python
# DO: Async for I/O
async def fetch_completion(messages: list[Message]) -> Result[Response, Error]:
    return await llm_adapter.complete(messages)

# DO: Sync for CPU-bound
def parse_seed(yaml_content: str) -> Seed:
    return Seed.model_validate(yaml.safe_load(yaml_content))

# DO: Thread pool for blocking in async
async def process_heavy():
    return await asyncio.to_thread(heavy_cpu_computation)

# DON'T: Block event loop
async def bad():
    result = heavy_computation()  # BLOCKS!
```

### Naming Conventions

| Component | Format | Example |
|-----------|--------|---------|
| Files | `snake_case.py` | `pal_router.py` |
| Classes | `PascalCase` | `EffectiveOntology` |
| Functions | `snake_case` | `calculate_drift` |
| Variables | `snake_case` | `current_context` |
| Constants | `UPPER_CASE` | `MAX_AC_DEPTH` |
| Events | `dot.notation.past_tense` | `ontology.concept.added` |
| DB Tables | `snake_case`, plural | `events`, `checkpoints` |
| JSON Fields | `snake_case` | `seed_id`, `created_at` |

### Import Rules (CRITICAL)

```python
# DO: Absolute imports only
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.routing.router import PALRouter

# DON'T: Relative imports across packages
from ..core.seed import Seed  # FORBIDDEN
from .router import PALRouter  # Only within same package
```

**Layered Dependencies:**
```
CLI Layer (cli/)
    ↓ can import
Application Layer (execution/, bigbang/, secondary/)
    ↓ can import
Domain Layer (core/, routing/, evaluation/, resilience/, consensus/)
    ↓ can import
Infrastructure Layer (providers/, persistence/, observability/, config/)
```

- Lower layers NEVER import upper layers
- Domain phases NEVER import each other directly
- Communication between phases via ExecutionEngine orchestrator

---

## Error Handling

**Use Result type for expected failures:**

```python
from ouroboros.core.types import Result

# DO: Separate retriable logic from Result conversion
@stamina.retry(on=litellm.RateLimitError, attempts=3)
async def _raw_complete(self, messages: list[dict], model: str) -> Response:
    """Exceptions bubble up for stamina retry"""
    return await litellm.acompletion(model=model, messages=messages)

async def complete(self, messages: list[Message]) -> Result[Response, ProviderError]:
    """Safe wrapper - converts exceptions to Result type"""
    try:
        response = await self._raw_complete([m.to_dict() for m in messages], model)
        return Result.ok(response)
    except litellm.APIError as e:
        return Result.err(ProviderError.from_exception(e))

# DO: Pattern match on Result
result = await adapter.complete(messages)
if result.is_ok:
    process(result.value)
else:
    handle_error(result.error)

# DON'T: Put try/except inside @stamina.retry decorated method
@stamina.retry(on=litellm.RateLimitError)
async def bad_complete(self, messages):
    try:
        return await litellm.acompletion(...)  # Exception caught below!
    except litellm.APIError:
        return Result.err(...)  # stamina NEVER retries - sees success!
```

---

## Event Sourcing

**All state changes via events:**

```python
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class BaseEvent(BaseModel, frozen=True):
    """Immutable event - ALL events inherit this"""
    id: str
    type: str  # "domain.entity.action_past_tense"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    aggregate_type: str
    aggregate_id: str
    data: dict[str, Any]

# Event naming: domain.entity.verb_past_tense
"ontology.concept.added"       # DO
"ontology.concept.weight_modified"  # DO - precise verb
"ontology.concept.updated"     # DON'T - ambiguous
"ConceptAdded"                 # DON'T - no namespace
```

---

## Data Boundaries (ORM vs Pydantic)

**CRITICAL: Never leak ORM objects outside repositories**

```python
# DO: Convert at boundary using .mappings() for SQLAlchemy Core compatibility
async def get_events(self, aggregate_id: str) -> list[Event]:
    async with self.session() as session:
        result = await session.execute(
            select(events_table).where(events_table.c.aggregate_id == aggregate_id)
        )
        rows = result.mappings().all()  # Returns list[RowMapping] for Pydantic
        return [Event.model_validate(dict(row)) for row in rows]

# DON'T: Use .scalars() with Core tables - returns only first column
async def bad_get_events(self, aggregate_id: str) -> list[Event]:
    result = await session.execute(select(events_table).where(...))
    rows = result.scalars().all()  # WRONG - returns first column only!
    return [Event.model_validate(row) for row in rows]  # Will fail!
```

---

## Context Propagation

**Use contextvars for metadata ONLY:**

```python
from contextvars import ContextVar
import structlog

# DO: Metadata only (IDs, tracing)
execution_id: ContextVar[str] = ContextVar("execution_id")
seed_id: ContextVar[str] = ContextVar("seed_id")

# DO: Bind to structlog
structlog.contextvars.bind_contextvars(
    execution_id=exec_id,
    seed_id=seed.id,
)

# DON'T: Business state in contextvars
current_phase_state: ContextVar[PhaseState] = ContextVar(...)  # BAD
# Pass business state explicitly as function arguments
```

---

## Testing

| Rule | Details |
|------|---------|
| **Structure** | `tests/` mirrors `src/ouroboros/` |
| **Framework** | pytest + pytest-asyncio |
| **Fixtures** | Deterministic, no timing dependencies |
| **Providers** | Contract testing with recorded responses |
| **Async Mode** | `asyncio_mode = "auto"` in pytest config |

```python
# tests/fixtures/consensus.py
RECORDED_RESPONSES = {
    "simple_approval": {
        "openai": {"verdict": "approve", "confidence": 0.9},
        "anthropic": {"verdict": "approve", "confidence": 0.85},
    },
}

# tests/unit/consensus/test_voting.py
async def test_consensus_approval(recorded_responses):
    result = await voting.aggregate(recorded_responses["simple_approval"])
    assert result.verdict == "approve"
```

---

## Anti-Patterns (FORBIDDEN)

### 1. Zombie Objects
```python
# DON'T: ORM outside session
async def bad():
    event = await session.get(EventModel, id)
    await session.close()
    return event  # ZOMBIE - will crash on attribute access

# DO: Convert immediately
async def good():
    event_model = await session.get(EventModel, id)
    return Event.model_validate(event_model)
```

### 2. God-Contexts
```python
# DON'T: Massive context object
def bad(ctx: GodContext):
    ctx.db.query(...)
    ctx.llm.complete(...)

# DO: Explicit dependencies
def good(db: EventStore, llm: LLMAdapter):
    ...
```

### 3. Ambiguous Event Verbs
```python
# DON'T
"ontology.concept.updated"    # What was updated?
"execution.ac.processed"      # What does processed mean?

# DO
"ontology.concept.weight_modified"
"execution.ac.decomposed"
"execution.ac.marked_atomic"
```

### 4. Async Wrapper Lie
```python
# DON'T: CPU-bound in async def
async def bad_parse(content: str):
    return heavy_parsing(content)  # BLOCKS EVENT LOOP

# DO: Thread pool
async def good_parse(content: str):
    return await asyncio.to_thread(heavy_parsing, content)
```

### 5. Silent Failures
```python
# DON'T
try:
    risky_operation()
except:
    pass  # SILENT FAILURE

# DO
try:
    risky_operation()
except Exception:
    log.exception("risky_operation failed", operation="risky")
    raise
```

### 6. God Objects
```python
# DON'T: Generic names
utils.py
manager.py
helper.py

# DO: Specific names
synthesis_engine.py
drift_calculator.py
complexity_estimator.py
```

---

## Phase Protocol

Each Ouroboros phase implements a strict interface:

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any

class PhaseContext(BaseModel):
    execution_id: str
    seed_id: str
    payload: dict[str, Any]  # Validated in execute() - see IPhase docstring

class PhaseResult(BaseModel):
    success: bool
    data: dict[str, Any]  # Phase-specific output, validated by caller
    events: list[BaseEvent]

class IPhase(ABC):
    @abstractmethod
    async def execute(self, ctx: PhaseContext) -> PhaseResult:
        """Execute phase - emit events, don't modify other phases.

        CRITICAL: The first line of execute() MUST validate ctx.payload
        against a phase-specific Pydantic model:

            input_data = RoutingInput.model_validate(ctx.payload)

        This ensures type safety across phase boundaries despite dict payload.
        """
        ...
```

**Phases communicate via events and ExecutionEngine, NEVER direct imports.**

---

## Quick Reference

### File Locations
| What | Where |
|------|-------|
| Shared types | `core/types.py` |
| Error hierarchy | `core/errors.py` |
| Protocols | `core/protocols.py` |
| Event definitions | `events/*.py` |
| Config loading | `config/loader.py` |
| User config | `~/.ouroboros/` |

### Commands
```bash
uv run ouroboros run seed.yaml      # Run workflow
uv run ouroboros validate seed.yaml # Validate seed
uv run pytest                        # Run tests
uv run ruff check src/              # Lint
uv run mypy src/                    # Type check
```

### Key Dependencies
| Package | Purpose |
|---------|---------|
| `typer` | CLI framework |
| `rich` | Terminal output |
| `structlog` | Structured logging |
| `pydantic` | Data validation |
| `sqlalchemy[asyncio]` | Database |
| `litellm` | LLM provider abstraction |
| `stamina` | Retry logic |

---

## Architecture Reference

Full architecture document: `_bmad-output/planning-artifacts/architecture.md`

**When in doubt, check the architecture document.**
