---
stepsCompleted: [1, 2, 3, 4, 5, 6, 7, 8]
status: 'complete'
completedAt: '2026-01-13'
inputDocuments:
  - requirement/1_EXECUTIVE_SUMMARY.md
  - requirement/2_FULL_SPECIFICATION.md
  - requirement/3_CONFIG_TEMPLATE.yaml
  - requirement/4_REDDIT_EXAMPLE.md
workflowType: 'architecture'
project_name: 'ouroboros'
user_name: 'Jaegyu.lee'
date: '2026-01-12'
---

# Architecture Decision Document

_This document builds collaboratively through step-by-step discovery. Sections are appended as we work through each architectural decision together._

---

## Project Context Analysis

### Requirements Overview

**Functional Requirements:**
Ouroboros is a 6-phase self-improving AI workflow system:
- Phase 0 (Big Bang): Clarification with Ambiguity Gate ≤0.2
- Phase 1 (PAL): Tiered routing - Frugal(1x), Standard(10x), Frontier(30x)
- Phase 2 (Execution): Recursive Double Diamond with AC decomposition
- Phase 3 (Resilience): Stagnation detection + 5 Lateral Thinking personas
- Phase 4 (Evaluation): 3-stage pipeline (Mechanical→Semantic→Consensus)
- Phase 5 (Consensus): Multi-model voting (3 models, 2/3 majority)
- Phase 6 (Secondary): TODO Registry with async batch processing

**Non-Functional Requirements:**
- Cost efficiency: "Frugal by Default" - 80%+ tasks at 1x cost
- Resilience: Zero-stop operation with lateral thinking fallbacks
- Persistence: SQLite default (PostgreSQL optional) with checkpointing
- Multi-provider: OpenAI, Anthropic, Google abstraction
- Drift control: Continuous measurement with ≤0.3 threshold

**Scale & Complexity:**
- Primary domain: Multi-LLM workflow orchestration
- Complexity level: High/Enterprise
- Estimated architectural components: 15-20 major modules
- Real-time requirements: Stagnation detection, drift monitoring

### Technical Constraints & Dependencies

- Multi-LLM provider support required (PAL abstraction)
- SQLite default, PostgreSQL optional for persistence
- MCP tool integration (greenfield implementation with extensions)
- Token/context limit management across providers

### Cross-Cutting Concerns Identified

1. **Observability**: Cost tracking (1x/10x/30x units), drift metrics, stagnation signals
2. **Configuration**: Model tiers, thresholds, personas, triggers
3. **Persistence**: Checkpointing strategy across all 6 phases
4. **Error Handling**: Escalation chains, lateral thinking, human intervention
5. **Context Management**: Token limits, compression, SubAgent isolation

### Foundational Philosophical Decisions

**1. Design Thinking × Ontology Duality**
- Double Diamond: Proactive diverge→converge pattern driving AC decomposition
- Ontology: Convergence filter active only during Define & Deliver phases

**2. Separation of Concerns in Seed**
```
Seed Structure:
├── Ontology      → World model (concepts, relationships, rules)
├── Constraints   → Operational rules (hard/soft)
└── Evaluation    → Judgment criteria (quality signals)
```
Each requires distinct Consensus trigger for modification.

**3. Immutability Pattern**
- Seed.direction (Goal, Success Metrics, Hard Constraints) = IMMUTABLE
- EffectiveOntology (weights, new concepts, exclude patterns) = MUTABLE with Consensus
- All evolution tracked in EvolutionLog for drift measurement

**4. Recursive Double Diamond**
- Non-atomic ACs run full Double Diamond cycle
- Decomposition occurs at Define (convergence) phase
- Atomic ACs execute directly without DD overhead
- Atomicity criteria: complexity < threshold, single-tool solvable, duration < limit

---

## Architectural Decisions (Party Mode Synthesis)

### Product Positioning

| Version | Type | Characteristics |
|---------|------|-----------------|
| **v1.0** | **Toolkit (Local-First)** | CLI-first, single-instance, SQLite default |
| v2.0+ | Platform (Cloud-Ready) | API-first, multi-tenant, PostgreSQL required |

**Rationale**: Fast initial validation, developer experimentation, complexity management.

### Core Design Patterns

#### 1. PAL Router: Stateless Design
```python
class PALRouter:
    """Stateless routing - state passed in, not stored"""
    def route(self, task: Task, routing_state: RoutingState, config: RoutingConfig) -> RoutingDecision:
        pass  # Pure function - no side effects
```

#### 2. EvolutionLog: Event Sourcing
```python
@dataclass
class OntologyEvent:
    event_id: str
    timestamp: datetime
    event_type: Literal["concept_added", "concept_removed", "weight_modified", "exclude_added"]
    payload: Dict[str, Any]
    consensus_id: str

class EffectiveOntology:
    base: OntologySchema  # Immutable
    events: List[OntologyEvent]  # Append-only log

    def replay(self) -> OntologySchema:
        """Event replay for current state + time-travel debugging"""
```

#### 3. AC Context Management: Hierarchical Isolation
- **Max Depth**: 5 levels
- **Compression**: Applied at depth 3+
- **Pattern**: Parent context summarized, current context full
- **Optimization**: Memoize compressed parent contexts

#### 4. SubAgent Context: Summary + Key Facts
```python
@dataclass
class FilteredContext:
    seed_summary: str      # Always included
    current_ac: str        # Always included
    recent_history: List[str]  # Last 3 iterations
    key_facts: List[str]   # Extracted core facts
```

#### 5. Consensus Protocol: Structured Concurrency
- `asyncio.gather` with individual timeouts
- Fallback to sequential retry on partial failure
- Minimum valid responses required before aggregation

#### 6. Deliberative Consensus Pattern (v0.4.0)

For high-stakes decisions requiring ontological validation:

```python
class DeliberativeConsensus:
    """Two-round adversarial evaluation with philosophical grounding"""

    async def evaluate(self, proposal: Proposal) -> DeliberativeResult:
        # Round 1: Concurrent positions
        advocate_task = self._run_advocate(proposal)
        devil_task = self._run_devil(proposal)  # Uses ontology_questions
        advocate, devil = await asyncio.gather(advocate_task, devil_task)

        # Round 2: Judge renders verdict
        verdict = await self._run_judge(advocate, devil)
        return DeliberativeResult(
            verdict=verdict,  # APPROVED | REJECTED | CONDITIONAL
            is_root_solution=devil.confirmed_root_cause,
        )
```

**Roles:**
- **ADVOCATE**: Argues for the proposal's merits
- **DEVIL'S ADVOCATE**: Challenges using four ontological questions (ESSENCE, ROOT_CAUSE, PREREQUISITES, HIDDEN_ASSUMPTIONS)
- **JUDGE**: Synthesizes positions, renders final verdict

### Testing Strategy

**Priority**: Observability Layer First (E0 Epic)

| Challenge | Solution |
|-----------|----------|
| Stagnation Detection | Deterministic fixtures (no timing dependencies) |
| Consensus Protocol | Contract testing with recorded responses |
| Lateral Personas | Pattern-based validation + golden output similarity |

### Configurable Thresholds (Hypothesis → Validation)

```yaml
uncertainty_threshold:
  initial_value: 0.3
  source: "hypothesis"
  validation_plan:
    - A/B test with 0.2, 0.3, 0.4
    - Track user_override_rate as ground truth
  tuning_strategy: "Start conservative, adjust via monitoring"
```

---

## Starter Template Evaluation

### Primary Technology Domain

CLI Tool / Backend Service for Multi-LLM workflow orchestration, targeting PyPI distribution.

### Technical Stack Decision

| Component | Choice | Version |
|-----------|--------|---------|
| **Language** | **Python** | **3.14+** |
| Package Manager | uv | Latest |
| CLI Framework | Typer | Latest |
| Testing | pytest | Latest |
| Async | Native asyncio | - |
| Distribution | PyPI package | - |

### Starter Options Considered

1. **`uv init --package`** - Native uv scaffolding with src/ layout
2. **Copier templates** - Pre-configured but opinionated
3. **GitHub templates** - Various levels of complexity
4. **Manual setup** - Full control, most work

### Selected Starter: `uv init --package`

**Rationale for Selection:**
- Official uv tooling ensures compatibility and long-term support
- Creates proper src/ layout required for PyPI distribution
- Minimal assumptions - Ouroboros has specific architectural needs
- Clean slate allows implementing our defined patterns (Event Sourcing, Stateless Router, etc.)

**Initialization Command:**

```bash
# Create project with package structure
uv init --package ouroboros --python 3.14

# Navigate to project
cd ouroboros

# Add core dependencies
uv add typer
uv add aiohttp httpx  # Async HTTP for LLM providers
uv add pydantic       # Data validation
uv add structlog      # Structured logging

# Add dev dependencies
uv add --dev pytest pytest-asyncio pytest-cov
uv add --dev ruff mypy  # Linting and type checking
uv add --dev pre-commit
```

### Project Structure (Post-Initialization)

```
ouroboros/
├── .python-version          # Python 3.14
├── pyproject.toml           # Project config with [project.scripts]
├── uv.lock                  # Locked dependencies
├── README.md
├── src/
│   └── ouroboros/
│       ├── __init__.py
│       ├── __main__.py      # Entry point
│       ├── cli/             # Typer CLI commands
│       │   ├── __init__.py
│       │   └── main.py
│       ├── core/            # Core domain logic
│       │   ├── seed.py      # Seed, EffectiveOntology
│       │   ├── ac.py        # AcceptanceCriteria
│       │   └── execution.py # Double Diamond loop
│       ├── routing/         # PAL Router
│       │   ├── router.py
│       │   └── tiers.py
│       ├── evaluation/      # 3-Stage Pipeline
│       │   ├── mechanical.py
│       │   ├── semantic.py
│       │   └── consensus.py
│       ├── resilience/      # Stagnation, Lateral Thinking
│       │   ├── stagnation.py
│       │   └── personas.py
│       ├── persistence/     # SQLite/PostgreSQL
│       │   ├── checkpoint.py
│       │   └── events.py    # Event Sourcing
│       ├── providers/       # LLM Provider Abstraction
│       │   ├── base.py
│       │   ├── openai.py
│       │   ├── anthropic.py
│       │   └── google.py
│       └── observability/   # Metrics, Cost Tracking
│           ├── metrics.py
│           └── cost.py
└── tests/
    ├── conftest.py
    ├── fixtures/            # Stagnation, Consensus fixtures
    ├── unit/
    └── integration/
```

### pyproject.toml Configuration

```toml
[project]
name = "ouroboros"
version = "0.1.0"
description = "Self-improving AI workflow system - Frugal by Default, Rigorous in Verification"
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
    "typer>=0.12.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "structlog>=24.0.0",
]

[project.scripts]
ouroboros = "ouroboros.cli.main:app"

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=5.0.0",
    "ruff>=0.8.0",
    "mypy>=1.13.0",
    "pre-commit>=4.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
python_version = "3.14"
strict = true
```

### Python 3.14 Features to Leverage

| Feature | Application in Ouroboros |
|---------|-------------------------|
| **Free-threaded mode (PEP 779)** | Parallel Consensus model calls without GIL |
| **Template strings (PEP 750)** | Cleaner prompt templates for LLM calls |
| **Deferred annotations (PEP 649)** | Faster startup, cleaner recursive type hints |
| **Multiple interpreters (PEP 734)** | Isolated SubAgent execution |

**Note:** Project initialization using this command should be the first implementation story.

---

## Core Architectural Decisions

### Decision Summary

| Category | Decision | Version/Details |
|----------|----------|-----------------|
| **Data Layer** | SQLAlchemy Core | Query builder, no ORM |
| **Event Store** | Single events table | All events unified |
| **Migrations** | Manual SQL scripts | Simple versioning |
| **Credentials** | Config file | `~/.ouroboros/credentials.yaml` |
| **Data Encryption** | None | User responsibility |
| **Seed Files** | Plain YAML | No encryption |
| **LLM Integration** | LiteLLM + OpenRouter | v1.80.15 |
| **Provider Pattern** | Adapter pattern | Wrap LiteLLM |
| **Error Handling** | Result type | Functional, no exceptions for expected |
| **Retry Strategy** | stamina | v25.1.0 |
| **Config Location** | Home directory | `~/.ouroboros/` |
| **Logging** | structlog | Structured logging |
| **CLI Output** | Rich | Beautiful terminal |
| **Publishing** | Both | Manual now, CI later |

### Data Architecture

#### Database Layer: SQLAlchemy Core
- Query builder without ORM overhead
- Direct SQL when needed, builder for complex queries
- Async support via `aiosqlite` + `sqlalchemy[asyncio]`

```python
from sqlalchemy import MetaData, Table, Column, String, JSON, DateTime
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("id", String, primary_key=True),
    Column("aggregate_type", String, index=True),  # "ontology", "execution", "consensus"
    Column("aggregate_id", String, index=True),
    Column("event_type", String),
    Column("payload", JSON),
    Column("timestamp", DateTime),
    Column("consensus_id", String, nullable=True),
)
```

#### Event Store: Single Table
- All events in unified `events` table
- `aggregate_type` column differentiates event categories
- Simple queries, easy backup/restore

#### Migrations: Manual SQL Scripts
```
~/.ouroboros/
├── migrations/
│   ├── 001_initial.sql
│   ├── 002_add_indexes.sql
│   └── version.txt
```

### Authentication & Security

#### Credentials: Config File
```yaml
# ~/.ouroboros/credentials.yaml
providers:
  openrouter:
    api_key: "sk-or-..."
    base_url: "https://openrouter.ai/api/v1"

  # Optional direct provider keys (fallback)
  openai:
    api_key: "sk-..."
  anthropic:
    api_key: "sk-ant-..."
```

#### Security Model
- No encryption (local toolkit, user's responsibility)
- Plain YAML seed files
- Config file permissions: `chmod 600`

### API & Communication

#### LLM Integration: LiteLLM + OpenRouter
- **LiteLLM v1.80.15** - Unified interface for all providers
- **OpenRouter** - Primary gateway, access to 100+ models
- Single API key, model routing via model string

```python
import litellm

# OpenRouter routing
response = await litellm.acompletion(
    model="openrouter/anthropic/claude-3-opus",
    messages=[{"role": "user", "content": prompt}],
    api_key=config.openrouter_api_key,
)
```

#### Provider Abstraction: Adapter Pattern

```python
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")

@dataclass
class Result(Generic[T, E]):
    """Functional error handling - explicit success flag prevents None edge cases"""
    _is_success: bool
    value: T | None = None
    error: E | None = None

    @property
    def is_ok(self) -> bool:
        return self._is_success

    @property
    def is_err(self) -> bool:
        return not self._is_success

    @classmethod
    def ok(cls, value: T) -> "Result[T, E]":
        return cls(_is_success=True, value=value)

    @classmethod
    def err(cls, error: E) -> "Result[T, E]":
        return cls(_is_success=False, error=error)


class LLMAdapter(ABC):
    """Base adapter for LLM providers"""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig
    ) -> Result[CompletionResponse, ProviderError]:
        pass


class LiteLLMAdapter(LLMAdapter):
    """Adapter wrapping LiteLLM"""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @stamina.retry(on=litellm.RateLimitError, attempts=3)
    async def _raw_complete(
        self,
        messages: list[dict],
        model: str,
        **kwargs
    ) -> litellm.ModelResponse:
        """Raw LLM call - exceptions bubble up for stamina retry"""
        return await litellm.acompletion(
            model=model,
            messages=messages,
            **kwargs,
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig
    ) -> Result[CompletionResponse, ProviderError]:
        """Safe wrapper - converts exceptions to Result type"""
        try:
            response = await self._raw_complete(
                messages=[m.to_dict() for m in messages],
                model=config.model,
                **self.config.to_litellm_kwargs(),
            )
            return Result.ok(CompletionResponse.from_litellm(response))
        except litellm.APIError as e:
            return Result.err(ProviderError.from_exception(e))
```

#### Error Handling: Result Type
- No exceptions for expected failures (rate limits, API errors)
- Exceptions only for programming errors
- Pattern matching on Result for flow control

#### Retry Strategy: stamina v25.1.0
- Exponential backoff with jitter
- Integrates with structlog for observability
- Async-native

### Infrastructure & Deployment

#### Configuration Location: `~/.ouroboros/`

```
~/.ouroboros/
├── config.yaml           # Main configuration
├── credentials.yaml      # API keys (chmod 600)
├── data/
│   └── ouroboros.db      # SQLite database
├── logs/
│   └── ouroboros.log     # Structured logs
├── migrations/
│   └── *.sql             # Migration scripts
└── cache/
    └── *.json            # Response cache (optional)
```

#### Logging: structlog
- Structured JSON logs for machine parsing
- Human-readable console output via Rich
- Log levels: DEBUG, INFO, WARNING, ERROR

```python
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if dev else structlog.processors.JSONRenderer(),
    ],
)
```

#### CLI Output: Rich
- Progress bars for long operations
- Tables for structured data
- Syntax highlighting for code/config
- Spinners for async operations

```python
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

console = Console()

# Progress tracking
with Progress() as progress:
    task = progress.add_task("Executing ACs...", total=len(acs))
    for ac in acs:
        await execute_ac(ac)
        progress.advance(task)
```

#### Publishing Strategy
- **Now**: Manual via `uv publish`
- **Later**: GitHub Actions on tag push

```yaml
# Future: .github/workflows/publish.yml
on:
  push:
    tags: ["v*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv build && uv publish
```

### Updated Dependencies

```toml
[project]
dependencies = [
    "typer>=0.12.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "structlog>=24.0.0",
    "litellm>=1.80.0",
    "sqlalchemy[asyncio]>=2.0.0",
    "aiosqlite>=0.20.0",
    "stamina>=25.1.0",
    "rich>=13.0.0",
    "pyyaml>=6.0.0",
]
```

### Decision Impact Analysis

**Implementation Sequence:**
1. Project initialization (`uv init --package`)
2. Config/credentials loading (`~/.ouroboros/`)
3. Database schema + event store
4. LiteLLM adapter with Result type
5. CLI with Rich output
6. Core domain logic (Seed, AC, etc.)

**Cross-Component Dependencies:**
- structlog → used by stamina, all components
- Result type → used by adapters, evaluation, consensus
- Event store → used by EffectiveOntology, checkpointing
- Rich → used by CLI, progress tracking

---

## Implementation Patterns & Consistency Rules

_Patterns derived from Python ecosystem standards (PEP 8), tech stack conventions, and domain-driven design principles. Validated via deep architectural analysis._

### Pattern Summary

| Category | Decision | Rationale |
|----------|----------|-----------|
| **Code Naming** | Strict PEP 8 | Python ecosystem standard |
| **Event Naming** | `dot.notation.past_tense` | Namespacing + semantic clarity |
| **Database** | snake_case, plural tables | SQLAlchemy convention |
| **JSON Fields** | snake_case | Pydantic default, consistency |
| **Datetime** | ISO 8601 strings | Human-readable, sortable |
| **Optional Fields** | Include null, never omit | Explicit > implicit |
| **Async** | Async-first for I/O | Non-blocking LLM calls |
| **Context** | contextvars | Cross-async propagation |
| **Checkpoints** | Phase-based UoW | Efficient persistence |

### Naming Patterns

#### Python Code Naming (PEP 8 Strict)

| Component | Format | Example |
|-----------|--------|---------|
| **Modules/Files** | `snake_case.py` | `pal_router.py`, `event_store.py` |
| **Classes** | `PascalCase` | `EffectiveOntology`, `PALRouter` |
| **Functions** | `snake_case` | `calculate_drift`, `route_task` |
| **Variables** | `snake_case` | `current_context`, `ac_depth` |
| **Constants** | `UPPER_CASE` | `MAX_AC_DEPTH`, `DRIFT_THRESHOLD` |
| **Type Aliases** | `PascalCase` | `EventPayload`, `RoutingResult` |

#### Event Type Naming

**Format:** `{domain}.{entity}.{action_past_tense}`

```python
# CORRECT - dot notation, past tense, namespaced
"ontology.concept.added"
"execution.ac.completed"
"consensus.vote.recorded"
"resilience.stagnation.detected"
"routing.tier.escalated"

# INCORRECT
"OntologyConceptAdded"      # No namespace, hard to filter
"ONTOLOGY_CONCEPT_ADDED"    # Not event sourcing convention
"ontology.concept.add"      # Present tense (command, not event)
"concept_added"             # No domain namespace
```

#### Database Naming

```python
# Tables: plural, snake_case
events          # not: event, Events
checkpoints     # not: checkpoint, Checkpoints

# Columns: snake_case
aggregate_id    # not: aggregateId
created_at      # not: createdAt
event_type      # not: eventType

# Indexes: idx_{table}_{columns}
idx_events_aggregate_id
idx_events_created_at
idx_checkpoints_seed_id
```

### Structure Patterns

#### Test Organization

```
tests/                          # Mirror src/ouroboros/
├── conftest.py                 # Shared fixtures
├── fixtures/
│   ├── stagnation.py           # Deterministic stagnation scenarios
│   ├── consensus.py            # Recorded provider responses
│   └── events.py               # Sample events
├── unit/
│   ├── core/
│   │   ├── test_seed.py
│   │   └── test_ac.py
│   ├── routing/
│   │   └── test_router.py
│   └── evaluation/
│       └── test_mechanical.py
└── integration/
    ├── test_execution_flow.py
    └── test_checkpoint_recovery.py
```

#### Module Boundaries

```python
# CORRECT - Absolute imports from package root
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result, EventPayload
from ouroboros.routing.router import PALRouter
from ouroboros.evaluation.mechanical import MechanicalEvaluator

# INCORRECT - Relative imports across packages
from ..core.seed import Seed          # Never cross package boundaries
from .router import PALRouter         # Only within same package
```

#### Shared Types Location

```
src/ouroboros/
├── core/
│   ├── types.py              # Shared types: Result, EventPayload, etc.
│   ├── errors.py             # Error types: OuroborosError, ProviderError
│   └── protocols.py          # Protocol definitions: LLMAdapter, Evaluator
```

### Format Patterns

#### Event Payload Structure

```python
@dataclass
class Event:
    """Base event structure - ALL events follow this format"""
    id: str                    # UUID
    type: str                  # "domain.entity.action_past_tense"
    timestamp: str             # ISO 8601: "2026-01-12T10:30:00Z"
    aggregate_type: str        # "ontology", "execution", "consensus"
    aggregate_id: str          # ID of the aggregate this event belongs to
    data: dict[str, Any]       # Type-specific payload

# Example
{
    "id": "evt_abc123",
    "type": "ontology.concept.added",
    "timestamp": "2026-01-12T10:30:00Z",
    "aggregate_type": "ontology",
    "aggregate_id": "onto_xyz789",
    "data": {
        "concept_name": "AI Agent",
        "weight": 1.0,
        "consensus_id": "cons_def456"
    }
}
```

#### JSON Field Naming

```python
# CORRECT - snake_case everywhere
{
    "seed_id": "seed_123",
    "created_at": "2026-01-12T10:30:00Z",
    "max_iterations": 100,
    "cost_units": 15
}

# INCORRECT - camelCase or mixed
{
    "seedId": "seed_123",       # Not Python convention
    "createdAt": "...",         # Inconsistent with code
}
```

#### Optional Fields

```python
# CORRECT - Include null explicitly
{
    "consensus_id": null,       # Explicitly no consensus yet
    "error_message": null       # Explicitly no error
}

# INCORRECT - Omit keys
{
    # consensus_id missing - ambiguous: not set or intentionally null?
}
```

### Communication Patterns

#### structlog Configuration

```python
import structlog
from contextvars import ContextVar

# Context variables for cross-async propagation
execution_ctx: ContextVar[dict] = ContextVar("execution_ctx", default={})

def configure_logging(dev_mode: bool = False):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer() if dev_mode
                else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

# Standard log keys
log = structlog.get_logger()
log.info(
    "ac.execution.started",        # event name
    seed_id="seed_123",
    ac_id="ac_456",
    depth=2,
    iteration=5,
    tier="frugal",
)
```

#### Rich CLI Output

```python
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

console = Console()

# Semantic colors
SUCCESS = "green"
WARNING = "yellow"
ERROR = "red"
INFO = "blue"

# Progress for long operations
with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    console=console,
) as progress:
    task = progress.add_task("Executing ACs...", total=len(acs))
    for ac in acs:
        await execute_ac(ac)
        progress.advance(task)

# Tables for structured output
table = Table(title="Execution Summary")
table.add_column("Phase", style="cyan")
table.add_column("Status", style="green")
table.add_column("Cost", justify="right")
console.print(table)

# Panels for important messages
console.print(Panel(
    "[bold green]Execution Complete[/bold green]\n"
    f"Total cost: {cost} units",
    title="Ouroboros",
))
```

### Process Patterns

#### Async Boundaries

```python
# RULE: Async for I/O, Sync for CPU-bound

# CORRECT - Async for LLM calls, DB operations
async def execute_ac(ac: AcceptanceCriteria) -> Result[ExecutionResult, Error]:
    response = await llm_adapter.complete(messages)  # I/O
    await event_store.append(event)                   # I/O
    return Result.ok(result)

# CORRECT - Sync for parsing, validation
def parse_seed(yaml_content: str) -> Seed:
    return Seed.model_validate(yaml.safe_load(yaml_content))

# INCORRECT - Blocking in async context
async def bad_example():
    result = heavy_cpu_computation()  # Blocks event loop!

# CORRECT - Thread pool for CPU-bound in async
async def good_example():
    result = await asyncio.to_thread(heavy_cpu_computation)
```

#### Context Propagation

```python
from contextvars import ContextVar
from dataclasses import dataclass
from contextlib import asynccontextmanager

@dataclass
class ExecutionContext:
    seed_id: str
    ac_id: str | None = None
    iteration: int = 0
    depth: int = 0

_ctx: ContextVar[ExecutionContext] = ContextVar("execution_context")

@asynccontextmanager
async def execution_scope(seed_id: str):
    """Context manager for execution scope - flows through async calls"""
    ctx = ExecutionContext(seed_id=seed_id)
    token = _ctx.set(ctx)
    structlog.contextvars.bind_contextvars(
        seed_id=seed_id,
    )
    try:
        yield ctx
    finally:
        _ctx.reset(token)
        structlog.contextvars.unbind_contextvars("seed_id")

def get_context() -> ExecutionContext:
    return _ctx.get()

# Usage
async def run_seed(seed: Seed):
    async with execution_scope(seed.id):
        ctx = get_context()
        # ctx automatically available in all nested async calls
```

#### Checkpoint Strategy (Unit of Work)

```python
@dataclass
class UnitOfWork:
    """Accumulate events, persist at phase boundaries"""
    events: list[Event] = field(default_factory=list)

    def record(self, event: Event) -> None:
        self.events.append(event)

    async def commit(self, store: EventStore) -> None:
        """Persist all accumulated events atomically"""
        async with store.transaction():
            for event in self.events:
                await store.append(event)
        self.events.clear()

# Phase-based checkpointing
async def execute_phase(phase: Phase, uow: UnitOfWork):
    """Execute phase, checkpoint on completion"""
    result = await phase.execute()

    uow.record(Event(
        type=f"execution.phase.{phase.name}.completed",
        data={"result": result.to_dict()}
    ))

    # Checkpoint at phase boundary
    await uow.commit(event_store)
    await checkpoint_store.save(
        CheckpointData(
            seed_id=get_context().seed_id,
            phase=phase.name,
            state=current_state.to_dict(),
        )
    )
```

#### Ontological Questioning Pattern (v0.4.0)

```python
from enum import Enum

class OntologyQuestionType(str, Enum):
    """Four fundamental questions for deep analysis"""
    ESSENCE = "essence"              # "What IS this, really?"
    ROOT_CAUSE = "root_cause"        # "Is this the root cause or a symptom?"
    PREREQUISITES = "prerequisites"  # "What must exist first?"
    HIDDEN_ASSUMPTIONS = "hidden_assumptions"  # "What are we assuming?"

@dataclass
class OntologyQuestion:
    type: OntologyQuestionType
    question: str
    context: str

    def format_prompt(self) -> str:
        """Format for LLM consumption"""
        return f"[{self.type.value.upper()}] {self.question}\nContext: {self.context}"

# Usage in Contrarian Persona
class ContrarianPersona:
    def challenge(self, proposal: str) -> list[OntologyQuestion]:
        return [
            OntologyQuestion(
                type=OntologyQuestionType.ROOT_CAUSE,
                question="Is this addressing the root cause or treating a symptom?",
                context=proposal,
            ),
            # ... other questions
        ]
```

**Application Points:**
- `resilience/personas.py`: Contrarian uses ontological questions
- `evaluation/consensus.py`: Devil's Advocate role
- `bigbang/ontology.py`: Interview framework discovery

### Anti-Patterns (MUST AVOID)

#### 1. Zombie Objects (Detached ORM State)

```python
# INCORRECT - Passing SQLAlchemy objects deep into logic
async def bad_handler(session: AsyncSession):
    event = await session.get(EventModel, event_id)
    await session.close()
    process_event(event)  # DetachedInstanceError risk!

# CORRECT - Convert to Pydantic at boundary
async def good_handler(session: AsyncSession):
    event_model = await session.get(EventModel, event_id)
    event = Event.model_validate(event_model)  # Convert immediately
    await session.close()
    process_event(event)  # Safe - Pydantic model
```

#### 2. God-Contexts

```python
# INCORRECT - Massive context object everywhere
def bad_function(ctx: GodContext):  # Hides all dependencies
    ctx.db.query(...)
    ctx.llm.complete(...)
    ctx.config.get(...)

# CORRECT - Explicit dependencies
def good_function(
    db: EventStore,
    llm: LLMAdapter,
    config: Config,
):
    ...
```

#### 3. Ambiguous Event Verbs

```python
# INCORRECT - Vague verbs
"ontology.concept.updated"    # What was updated?
"execution.ac.processed"      # What does processed mean?

# CORRECT - Precise verbs
"ontology.concept.weight_modified"
"ontology.concept.relationship_added"
"execution.ac.decomposed"
"execution.ac.marked_atomic"
```

#### 4. Async Wrapper Lie

```python
# INCORRECT - CPU-bound in async def
async def bad_parse(content: str):
    return heavy_parsing_logic(content)  # Blocks event loop!

# CORRECT - Offload to thread pool
async def good_parse(content: str):
    return await asyncio.to_thread(heavy_parsing_logic, content)
```

### Enforcement Guidelines

**All AI Agents MUST:**
1. Follow PEP 8 naming strictly - ruff enforces this
2. Use dot.notation.past_tense for all events
3. Convert ORM models to Pydantic at repository boundaries
4. Use `async` only for I/O operations
5. Propagate context via contextvars, not function parameters
6. Checkpoint at phase boundaries, not per-operation

**Verification:**
- `ruff check` - Enforces PEP 8, bans bad patterns
- `mypy --strict` - Type consistency
- Pre-commit hooks catch violations before merge

---

## Project Structure & Boundaries

_Complete project structure mapping Ouroboros 6-phase system to domain-driven modules with strict layered dependencies._

### Complete Project Directory Structure

```
ouroboros/
├── .python-version              # 3.14
├── pyproject.toml               # uv/hatchling config
├── uv.lock                      # Locked dependencies
├── README.md
├── LICENSE
├── .gitignore
├── .pre-commit-config.yaml      # Pre-commit hooks
├── ruff.toml                    # Linter config
│
├── src/
│   └── ouroboros/
│       ├── __init__.py          # Package version, exports
│       ├── __main__.py          # python -m ouroboros entry
│       ├── py.typed             # PEP 561 marker
│       │
│       ├── cli/                 # CLI Layer
│       │   ├── __init__.py
│       │   ├── main.py          # Typer app, command registration
│       │   ├── commands/
│       │   │   ├── __init__.py
│       │   │   ├── run.py       # ouroboros run <seed.yaml>
│       │   │   ├── validate.py  # ouroboros validate <seed.yaml>
│       │   │   ├── status.py    # ouroboros status [--seed-id]
│       │   │   ├── resume.py    # ouroboros resume <checkpoint-id>
│       │   │   ├── config.py    # ouroboros config [show|set|init]
│       │   │   └── story.py     # v0.4.0: ouroboros story - narrative generation from universe
│       │   └── formatters/
│       │       ├── __init__.py
│       │       ├── tables.py    # Rich table formatters
│       │       ├── progress.py  # Progress bars/spinners
│       │       └── panels.py    # Rich panels for output
│       │
│       ├── core/                # Core Domain Layer
│       │   ├── __init__.py
│       │   ├── types.py         # Result, EventPayload, shared types
│       │   ├── errors.py        # OuroborosError hierarchy
│       │   ├── protocols.py     # Protocol definitions (LLMAdapter, etc.)
│       │   ├── seed.py          # Seed, OntologySchema, Constraints
│       │   ├── ontology.py      # EffectiveOntology, OntologyEvent
│       │   ├── ontology_questions.py  # v0.4.0: Four ontological probes (ESSENCE, ROOT_CAUSE, PREREQUISITES, HIDDEN_ASSUMPTIONS)
│       │   ├── ac.py            # AcceptanceCriteria, ACNode, ACTree
│       │   └── context.py       # ExecutionContext, contextvars
│       │
│       ├── events/              # Event Definitions (Pure Data)
│       │   ├── __init__.py
│       │   ├── base.py          # BaseEvent, EventMeta
│       │   ├── ontology.py      # ontology.* events
│       │   ├── execution.py     # execution.* events
│       │   ├── consensus.py     # consensus.* events
│       │   ├── routing.py       # routing.* events
│       │   └── resilience.py    # resilience.* events
│       │
│       ├── bigbang/             # Phase 0: Big Bang
│       │   ├── __init__.py
│       │   ├── clarifier.py     # Clarification engine
│       │   ├── ambiguity.py     # Ambiguity Gate (≤0.2 threshold)
│       │   ├── interview.py     # Interview protocol
│       │   ├── ontology.py      # v0.4.0: Ontological framework discovery during interview
│       │   └── seed_generator.py # Generate Seed from interview
│       │
│       ├── routing/             # Phase 1: PAL Router
│       │   ├── __init__.py
│       │   ├── router.py        # PALRouter (stateless)
│       │   ├── tiers.py         # Tier enum, TierConfig
│       │   ├── complexity.py    # Complexity estimation
│       │   └── escalation.py    # Escalation rules
│       │
│       ├── execution/           # Phase 2: Execution
│       │   ├── __init__.py
│       │   ├── engine.py        # ExecutionEngine main loop
│       │   ├── double_diamond.py # Double Diamond phases
│       │   ├── decomposition.py # AC decomposition logic
│       │   ├── atomicity.py     # Atomic detection
│       │   └── iteration.py     # Iteration management
│       │
│       ├── resilience/          # Phase 3: Resilience
│       │   ├── __init__.py
│       │   ├── stagnation.py    # StagnationDetector (4 patterns)
│       │   ├── patterns.py      # StagnationPattern enum
│       │   ├── lateral.py       # LateralThinkingEngine
│       │   └── personas.py      # 5 Lateral Thinking personas
│       │
│       ├── evaluation/          # Phase 4: Evaluation
│       │   ├── __init__.py
│       │   ├── models.py        # v0.4.0: EvaluationResult, ConsensusResult, Verdict
│       │   ├── pipeline.py      # EvaluationPipeline orchestrator
│       │   ├── mechanical.py    # Stage 1: MechanicalEvaluator ($0)
│       │   ├── semantic.py      # Stage 2: SemanticEvaluator ($$)
│       │   ├── consensus.py     # v0.4.0: DeliberativeConsensus (Advocate/Devil/Judge)
│       │   └── stage_result.py  # StageResult (legacy, see models.py)
│       │
│       ├── consensus/           # Phase 5: Consensus
│       │   ├── __init__.py
│       │   ├── protocol.py      # ConsensusProtocol
│       │   ├── voting.py        # Multi-model voting (2/3 majority)
│       │   ├── triggers.py      # Consensus trigger matrix
│       │   └── session.py       # ConsensusSession state
│       │
│       ├── secondary/           # Phase 6: Secondary Loop
│       │   ├── __init__.py
│       │   ├── todo_registry.py # TODORegistry
│       │   ├── batch.py         # Batch processing
│       │   └── scheduler.py     # Async scheduling
│       │
│       ├── providers/           # Infrastructure: LLM Providers
│       │   ├── __init__.py
│       │   ├── base.py          # LLMAdapter ABC
│       │   ├── litellm_adapter.py # LiteLLMAdapter (main)
│       │   ├── models.py        # Message, CompletionResponse
│       │   └── config.py        # ProviderConfig, model mappings
│       │
│       ├── persistence/         # Infrastructure: Storage
│       │   ├── __init__.py
│       │   ├── event_store.py   # EventStore (SQLAlchemy Core)
│       │   ├── checkpoint.py    # CheckpointStore
│       │   ├── schema.py        # SQLAlchemy table definitions
│       │   ├── migrations/
│       │   │   ├── __init__.py
│       │   │   ├── runner.py    # Manual migration runner
│       │   │   └── scripts/     # SQL migration files
│       │   │       └── 001_initial.sql
│       │   └── uow.py           # UnitOfWork pattern
│       │
│       ├── observability/       # Infrastructure: Monitoring
│       │   ├── __init__.py
│       │   ├── metrics.py       # OuroborosMetrics
│       │   ├── cost.py          # CostTracker (1x/10x/30x)
│       │   ├── drift.py         # DriftMeasurement
│       │   └── logging.py       # structlog configuration
│       │
│       └── config/              # Infrastructure: Configuration
│           ├── __init__.py
│           ├── loader.py        # Config loading from ~/.ouroboros/
│           ├── models.py        # Config Pydantic models
│           ├── defaults.py      # Default configurations
│           └── validation.py    # Config validation
│
├── tests/
│   ├── conftest.py              # Shared fixtures, pytest config
│   ├── fixtures/
│   │   ├── __init__.py
│   │   ├── seeds.py             # Sample Seed fixtures
│   │   ├── events.py            # Sample Event fixtures
│   │   ├── stagnation.py        # Stagnation scenario fixtures
│   │   ├── consensus.py         # Recorded consensus responses
│   │   └── providers.py         # Mock provider responses
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── core/
│   │   │   ├── test_seed.py
│   │   │   ├── test_ontology.py
│   │   │   ├── test_ac.py
│   │   │   └── test_types.py
│   │   ├── routing/
│   │   │   ├── test_router.py
│   │   │   └── test_complexity.py
│   │   ├── execution/
│   │   │   ├── test_engine.py
│   │   │   └── test_decomposition.py
│   │   ├── evaluation/
│   │   │   ├── test_mechanical.py
│   │   │   └── test_semantic.py
│   │   ├── resilience/
│   │   │   ├── test_stagnation.py
│   │   │   └── test_lateral.py
│   │   ├── consensus/
│   │   │   └── test_voting.py
│   │   ├── persistence/
│   │   │   ├── test_event_store.py
│   │   │   └── test_checkpoint.py
│   │   └── providers/
│   │       └── test_litellm_adapter.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_execution_flow.py
│   │   ├── test_checkpoint_recovery.py
│   │   ├── test_consensus_integration.py
│   │   └── test_cli_commands.py
│   └── e2e/
│       ├── __init__.py
│       └── test_reddit_brain.py
│
└── examples/                    # Sample seeds and configs
    ├── reddit_brain/
    │   ├── seed.yaml
    │   └── config.yaml
    └── simple/
        └── hello_world.yaml
```

### Architectural Boundaries

#### Layered Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI Layer                               │
│                    cli/commands/, cli/formatters/               │
├─────────────────────────────────────────────────────────────────┤
│                      Application Layer                          │
│           execution/, bigbang/, secondary/                      │
├─────────────────────────────────────────────────────────────────┤
│                        Domain Layer                             │
│    core/, routing/, evaluation/, resilience/, consensus/        │
│                        events/                                  │
├─────────────────────────────────────────────────────────────────┤
│                     Infrastructure Layer                        │
│         providers/, persistence/, observability/, config/       │
└─────────────────────────────────────────────────────────────────┘
```

#### Dependency Rules

| Layer | Can Import From | Cannot Import From |
|-------|-----------------|-------------------|
| **CLI** | Application, Domain, Infrastructure | - |
| **Application** | Domain, Infrastructure | CLI |
| **Domain** | core/, events/ only | Application, CLI, Infrastructure |
| **Infrastructure** | core/, events/ only | Application, CLI, Domain phases |

**Critical Rule:** Domain phase packages (routing/, evaluation/, etc.) NEVER import from each other directly. Communication happens via:
- Event Bus (events passed through Application layer)
- Workflow Orchestrator (execution/engine.py coordinates phases)

### Phase to Module Mapping

| Ouroboros Phase | Package | Key Files |
|-----------------|---------|-----------|
| **Phase 0: Big Bang** | `bigbang/` | clarifier.py, ambiguity.py, interview.py |
| **Phase 1: PAL Router** | `routing/` | router.py, tiers.py, complexity.py |
| **Phase 2: Execution** | `execution/` | engine.py, double_diamond.py, decomposition.py |
| **Phase 3: Resilience** | `resilience/` | stagnation.py, lateral.py, personas.py |
| **Phase 4: Evaluation** | `evaluation/` | pipeline.py, mechanical.py, semantic.py |
| **Phase 5: Consensus** | `consensus/` | protocol.py, voting.py, triggers.py |
| **Phase 6: Secondary** | `secondary/` | todo_registry.py, batch.py |

### Integration Points

#### Internal Communication

```python
# Phase communication via ExecutionEngine (orchestrator)
# execution/engine.py

class ExecutionEngine:
    """Orchestrates phase transitions - phases don't know about each other"""

    async def run(self, seed: Seed) -> ExecutionResult:
        # Phase 0: Big Bang (if needed)
        if seed.needs_clarification:
            seed = await self.bigbang.clarify(seed)

        # Phase 1-2: Route and Execute
        for ac in seed.acceptance_criteria:
            tier = self.router.route(ac)  # Phase 1
            result = await self.execute_ac(ac, tier)  # Phase 2

            # Phase 3: Check resilience
            if self.stagnation.detect(result):
                result = await self.lateral.think(ac)

            # Phase 4: Evaluate
            eval_result = await self.evaluator.evaluate(result)

            # Phase 5: Consensus (if triggered)
            if eval_result.needs_consensus:
                await self.consensus.vote(result)
```

#### Event Flow

```python
# Events are pure data - safe to import anywhere
from ouroboros.events.execution import ACCompletedEvent
from ouroboros.events.consensus import VoteRecordedEvent

# Event store persists all events
await event_store.append(ACCompletedEvent(
    ac_id=ac.id,
    result=result.to_dict(),
    cost_units=15,
))
```

### File Organization Patterns

#### Configuration Files (Root)

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata, dependencies, tool config |
| `uv.lock` | Locked dependency versions |
| `ruff.toml` | Linter configuration (separate for clarity) |
| `.pre-commit-config.yaml` | Pre-commit hooks |
| `.python-version` | Python 3.14 specification |

#### User Configuration (~/.ouroboros/)

```
~/.ouroboros/
├── config.yaml           # Main configuration
├── credentials.yaml      # API keys (chmod 600)
├── data/
│   └── ouroboros.db      # SQLite database
├── logs/
│   └── ouroboros.log     # Structured logs
└── migrations/
    └── version.txt       # Applied migration version
```

### Key File Responsibilities

#### core/types.py
```python
"""Shared types used across all modules"""
from typing import TypeVar, Generic
from dataclasses import dataclass

T = TypeVar("T")
E = TypeVar("E")

@dataclass
class Result(Generic[T, E]):
    """Functional error handling - no exceptions for expected failures"""
    ...

# Type aliases
EventPayload = dict[str, Any]
CostUnits = int
DriftScore = float
```

#### core/protocols.py
```python
"""Protocol definitions - contracts for implementations"""
from typing import Protocol

class LLMAdapter(Protocol):
    async def complete(self, messages: list[Message], config: CompletionConfig) -> Result[...]: ...

class Evaluator(Protocol):
    async def evaluate(self, result: ExecutionResult) -> EvaluationResult: ...

class EventStore(Protocol):
    async def append(self, event: Event) -> None: ...
    async def replay(self, aggregate_id: str) -> list[Event]: ...
```

#### events/base.py
```python
"""Base event structure - all events inherit from this"""
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid

class BaseEvent(BaseModel, frozen=True):
    """Immutable event base - safe to import anywhere"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    aggregate_type: str
    aggregate_id: str

    model_config = {"frozen": True}
```

### Development Workflow Integration

#### Commands

```bash
# Development
uv run ouroboros --help           # CLI help
uv run pytest                     # Run tests
uv run ruff check src/            # Lint
uv run mypy src/                  # Type check

# Build & Publish
uv build                          # Create wheel
uv publish                        # Publish to PyPI
```

#### Pre-commit Hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic>=2.0]
```

---

## Architecture Validation Results

_Comprehensive validation of coherence, requirements coverage, and implementation readiness._

### Coherence Validation ✅

**Technology Compatibility:**
All chosen technologies are compatible and work together seamlessly:
- Python 3.14 + native asyncio
- SQLAlchemy Core 2.x with async support + aiosqlite
- LiteLLM 1.80.15 with OpenRouter integration
- Typer CLI + Rich output + structlog logging
- Pydantic 2.x for validation + stamina for retries
- uv for fast package management

**Pattern Consistency:**
All patterns align with technology choices:
- Event Sourcing pattern supported by SQLAlchemy Core's flexible queries
- Result type complements Python's type system
- Adapter pattern works well with LiteLLM's unified interface
- contextvars integrates naturally with asyncio
- Phase-based checkpointing aligns with SQLAlchemy transactions

**Structure Alignment:**
Project structure supports all architectural decisions:
- Layered architecture prevents circular dependencies
- Each Ouroboros phase has dedicated package
- events/ package enables safe imports everywhere
- Infrastructure layer properly isolated from domain

### Requirements Coverage Validation ✅

**Ouroboros 6-Phase System:**

| Phase | Package | Status |
|-------|---------|--------|
| Phase 0: Big Bang | `bigbang/` | ✅ Covered |
| Phase 1: PAL Router | `routing/` | ✅ Covered |
| Phase 2: Execution | `execution/` | ✅ Covered |
| Phase 3: Resilience | `resilience/` | ✅ Covered |
| Phase 4: Evaluation | `evaluation/` | ✅ Covered |
| Phase 5: Consensus | `consensus/` | ✅ Covered |
| Phase 6: Secondary | `secondary/` | ✅ Covered |

**Non-Functional Requirements:**

| NFR | Architectural Support | Status |
|-----|----------------------|--------|
| Cost Efficiency | CostTracker, tiered routing | ✅ |
| Resilience | StagnationDetector, LateralThinking | ✅ |
| Persistence | EventStore, CheckpointStore | ✅ |
| Multi-Provider | LiteLLM + OpenRouter adapter | ✅ |
| Drift Control | DriftMeasurement, Event Sourcing | ✅ |
| Observability | structlog, metrics, cost tracking | ✅ |

**Philosophical Requirements:**

| Requirement | Implementation | Status |
|-------------|---------------|--------|
| Design Thinking (Double Diamond) | execution/double_diamond.py | ✅ |
| Ontology as Convergence Filter | core/ontology.py + events/ | ✅ |
| Seed Immutability + Evolution | EffectiveOntology + Event Sourcing | ✅ |
| Recursive AC Decomposition | execution/decomposition.py | ✅ |

### Implementation Readiness Validation ✅

**Decision Completeness:**
- ✅ All critical technologies documented with specific versions
- ✅ All patterns include code examples
- ✅ Anti-patterns explicitly documented
- ✅ Enforcement guidelines specified (ruff, mypy, pre-commit)

**Structure Completeness:**
- ✅ Complete directory tree with all files defined
- ✅ All modules mapped to Ouroboros phases
- ✅ Test structure mirrors source structure
- ✅ examples/ directory for sample seeds

**Pattern Completeness:**
- ✅ Naming patterns: PEP 8, events, database, JSON
- ✅ Structure patterns: layered architecture, test organization
- ✅ Communication patterns: structlog, Rich CLI, events
- ✅ Process patterns: async boundaries, context propagation, checkpointing

### Gap Analysis Results

**Critical Gaps:** None found

**Important Gaps (Non-blocking, deferred to v2.0):**
| Gap | Rationale for Deferral |
|-----|------------------------|
| MCP tool integration details | v1.0 focuses on core workflow; MCP is v2.0 extensibility feature |
| Human intervention interface | v1.0 is autonomous-focused; human override is v2.0 feature |

**Nice-to-Have (Future enhancements):**
| Enhancement | Target Version |
|-------------|----------------|
| Docker/containerization | v2.0 (Platform) |
| API server mode | v2.0 (Platform) |
| Web dashboard | v2.0+ |

### Architecture Completeness Checklist

**✅ Requirements Analysis**
- [x] Project context thoroughly analyzed
- [x] Scale and complexity assessed (High/Enterprise)
- [x] Technical constraints identified (multi-provider, persistence, cost)
- [x] Cross-cutting concerns mapped (observability, config, error handling)
- [x] Philosophical foundations documented (Design Thinking × Ontology)

**✅ Architectural Decisions**
- [x] Critical decisions documented with versions
- [x] Technology stack fully specified (Python 3.14, uv, Typer, etc.)
- [x] Integration patterns defined (LiteLLM adapter, Event Sourcing)
- [x] Performance considerations addressed (async-first, tiered routing)
- [x] Security model defined (config file credentials, no encryption v1.0)

**✅ Implementation Patterns**
- [x] Naming conventions established (PEP 8, dot.notation events)
- [x] Structure patterns defined (layered architecture)
- [x] Communication patterns specified (structlog, contextvars)
- [x] Process patterns documented (async boundaries, UoW checkpointing)
- [x] Anti-patterns documented (4 explicit anti-patterns)

**✅ Project Structure**
- [x] Complete directory structure defined (70+ files mapped)
- [x] Component boundaries established (strict layered imports)
- [x] Integration points mapped (ExecutionEngine orchestrator)
- [x] Requirements to structure mapping complete (phase→package)

### Architecture Readiness Assessment

**Overall Status:** ✅ READY FOR IMPLEMENTATION

**Confidence Level:** Very High

**Key Strengths:**
1. Clean separation of concerns with layered architecture
2. Event Sourcing enables time-travel debugging and drift tracking
3. Stateless components enable easy testing and scaling
4. Comprehensive patterns prevent AI agent conflicts
5. All 6 Ouroboros phases have dedicated, focused packages
6. Modern Python 3.14 features leveraged appropriately

**Areas for Future Enhancement (v2.0+):**
1. MCP tool integration for extensibility
2. Human intervention interface for override scenarios
3. Docker containerization for platform deployment
4. API server mode for multi-tenant usage
5. Web dashboard for monitoring

### Implementation Handoff

**AI Agent Guidelines:**
1. Follow all architectural decisions exactly as documented
2. Use implementation patterns consistently across all components
3. Respect project structure and layered import boundaries
4. Use dot.notation.past_tense for all events
5. Convert ORM models to Pydantic at boundaries
6. Use async only for I/O operations
7. Checkpoint at phase boundaries using UnitOfWork
8. Refer to this document for all architectural questions

**First Implementation Priority:**

```bash
# Step 1: Initialize project
uv init --package ouroboros --python 3.14
cd ouroboros

# Step 2: Add dependencies
uv add typer httpx pydantic structlog litellm "sqlalchemy[asyncio]" aiosqlite stamina rich pyyaml
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy pre-commit

# Step 3: Create directory structure
# Follow the Project Structure section exactly

# Step 4: Implement in this order:
# 1. core/types.py (Result, shared types)
# 2. core/errors.py (error hierarchy)
# 3. events/base.py (BaseEvent)
# 4. config/loader.py (~/.ouroboros/ setup)
# 5. persistence/schema.py (events table)
# 6. observability/logging.py (structlog config)
# 7. providers/litellm_adapter.py (LLM integration)
# 8. cli/main.py (basic CLI skeleton)
```

---

## Architecture Completion Summary

### Workflow Completion

**Architecture Decision Workflow:** COMPLETED ✅
**Total Steps Completed:** 8
**Date Completed:** 2026-01-13
**Document Location:** `_bmad-output/planning-artifacts/architecture.md`

### Final Architecture Deliverables

**Complete Architecture Document**
- All architectural decisions documented with specific versions
- Implementation patterns ensuring AI agent consistency
- Complete project structure with all files and directories (70+ files)
- Requirements to architecture mapping
- Validation confirming coherence and completeness

**Implementation Ready Foundation**
- 25+ architectural decisions made
- 15+ implementation patterns defined
- 12 architectural packages specified
- All 6 Ouroboros phases fully supported

**AI Agent Implementation Guide**
- Technology stack with verified versions (Python 3.14, LiteLLM 1.80.15, etc.)
- Consistency rules that prevent implementation conflicts
- Project structure with clear layered boundaries
- Integration patterns and communication standards

### Quality Assurance Checklist

**✅ Architecture Coherence**
- [x] All decisions work together without conflicts
- [x] Technology choices are compatible
- [x] Patterns support the architectural decisions
- [x] Structure aligns with all choices

**✅ Requirements Coverage**
- [x] All functional requirements are supported (6 Ouroboros phases)
- [x] All non-functional requirements are addressed (cost, resilience, persistence)
- [x] Cross-cutting concerns are handled (observability, config, errors)
- [x] Integration points are defined (ExecutionEngine orchestrator)

**✅ Implementation Readiness**
- [x] Decisions are specific and actionable
- [x] Patterns prevent agent conflicts
- [x] Structure is complete and unambiguous
- [x] Examples are provided for clarity
- [x] Anti-patterns explicitly documented

### Project Success Factors

**Clear Decision Framework**
Every technology choice was made collaboratively with clear rationale, leveraging Zen deep thinking for validation.

**Consistency Guarantee**
Implementation patterns and rules ensure that multiple AI agents will produce compatible, consistent code that works together seamlessly.

**Complete Coverage**
All project requirements are architecturally supported, with clear mapping from Ouroboros 6-phase system to technical implementation.

**Solid Foundation**
The Python 3.14 + uv + Typer stack with Event Sourcing and Domain-Driven Design provides a production-ready foundation following current best practices.

---

**Architecture Status:** ✅ READY FOR IMPLEMENTATION

**Next Phase:** Begin implementation using the architectural decisions and patterns documented herein.

**Document Maintenance:** Update this architecture when major technical decisions are made during implementation.

---

_Last Updated: 2026-02-02 (v0.4.0 additions)_

