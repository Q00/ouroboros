# Core Module API Reference

The core module (`ouroboros.core`) provides foundational types, error handling, and the immutable Seed specification.

## Import

```python
from ouroboros.core import (
    # Types
    Result,
    EventPayload,
    CostUnits,
    DriftScore,
    # Errors
    OuroborosError,
    ProviderError,
    ConfigError,
    PersistenceError,
    ValidationError,
    # Seed
    Seed,
    SeedMetadata,
    OntologySchema,
    OntologyField,
    EvaluationPrinciple,
    ExitCondition,
    # Context
    WorkflowContext,
    ContextMetrics,
    CompressionResult,
    FilteredContext,
    count_tokens,
    count_context_tokens,
    get_context_metrics,
    compress_context,
    compress_context_with_llm,
    create_filtered_context,
)
```

## Result Type

`Result[T, E]` is a generic type that represents either success (`Ok`) or failure (`Err`). It is used for expected failures instead of exceptions.

### Class: `Result[T, E]`

A frozen dataclass representing success or failure.

#### Class Methods

##### `Result.ok(value: T) -> Result[T, E]`

Create a successful Result containing the given value.

```python
result = Result.ok(42)
assert result.is_ok
assert result.value == 42
```

##### `Result.err(error: E) -> Result[T, E]`

Create a failed Result containing the given error.

```python
result = Result.err("something went wrong")
assert result.is_err
assert result.error == "something went wrong"
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `is_ok` | `bool` | True if this Result is Ok (success) |
| `is_err` | `bool` | True if this Result is Err (failure) |
| `value` | `T` | The Ok value (raises ValueError if Err) |
| `error` | `E` | The Err value (raises ValueError if Ok) |

#### Methods

##### `unwrap() -> T`

Return the Ok value or raise ValueError if Err.

```python
result = Result.ok(42)
value = result.unwrap()  # Returns 42

result = Result.err("error")
value = result.unwrap()  # Raises ValueError
```

##### `unwrap_or(default: T) -> T`

Return the Ok value or the provided default if Err.

```python
result = Result.err("error")
value = result.unwrap_or(0)  # Returns 0
```

##### `map(fn: Callable[[T], U]) -> Result[U, E]`

Transform the Ok value using the given function.

```python
result = Result.ok(10)
doubled = result.map(lambda x: x * 2)
assert doubled.value == 20
```

##### `map_err(fn: Callable[[E], F]) -> Result[T, F]`

Transform the Err value using the given function.

```python
result = Result.err("error")
wrapped = result.map_err(lambda e: Exception(e))
```

##### `and_then(fn: Callable[[T], Result[U, E]]) -> Result[U, E]`

Chain Result-producing operations (flatMap/bind).

```python
def divide(a: int, b: int) -> Result[int, str]:
    if b == 0:
        return Result.err("division by zero")
    return Result.ok(a // b)

result = Result.ok(10).and_then(lambda x: divide(x, 2))
assert result.value == 5
```

---

## Error Hierarchy

All Ouroboros-specific exceptions inherit from `OuroborosError`.

```
OuroborosError (base)
+-- ProviderError     - LLM provider failures
+-- ConfigError       - Configuration issues
+-- PersistenceError  - Database/storage issues
+-- ValidationError   - Data validation failures
```

### Class: `OuroborosError`

Base exception for all Ouroboros errors.

```python
class OuroborosError(Exception):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `message` | `str` | Human-readable error description |
| `details` | `dict[str, Any]` | Additional context about the error |

### Class: `ProviderError`

Error from LLM provider operations.

```python
class ProviderError(OuroborosError):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        provider: str | None = None,
    ) -> ProviderError: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `provider` | `str | None` | Name of the LLM provider |
| `status_code` | `int | None` | HTTP status code if applicable |

### Class: `ConfigError`

Error from configuration operations.

```python
class ConfigError(OuroborosError):
    def __init__(
        self,
        message: str,
        *,
        config_key: str | None = None,
        config_file: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `config_key` | `str | None` | Configuration key that caused the error |
| `config_file` | `str | None` | Path to the config file |

### Class: `PersistenceError`

Error from database and storage operations.

```python
class PersistenceError(OuroborosError):
    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        table: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `operation` | `str | None` | Operation that failed (e.g., "insert", "query") |
| `table` | `str | None` | Database table involved |

### Class: `ValidationError`

Error from data validation operations.

```python
class ValidationError(OuroborosError):
    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        value: Any | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `field` | `str | None` | Field that failed validation |
| `value` | `Any | None` | The invalid value |
| `safe_value` | `str` | Safe representation for logging (masks sensitive data) |

---

## Seed Specification

The Seed is the "constitution" of a workflow - an immutable specification generated from the Big Bang interview phase.

### Class: `Seed`

Immutable specification for workflow execution.

```python
class Seed(BaseModel, frozen=True):
    # Direction - IMMUTABLE
    goal: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    # Structure
    ontology_schema: OntologySchema

    # Evaluation
    evaluation_principles: tuple[EvaluationPrinciple, ...]

    # Termination
    exit_conditions: tuple[ExitCondition, ...]

    # Metadata
    metadata: SeedMetadata
```

| Field | Type | Description |
|-------|------|-------------|
| `goal` | `str` | Primary objective of the workflow |
| `constraints` | `tuple[str, ...]` | Hard constraints that must be satisfied |
| `acceptance_criteria` | `tuple[str, ...]` | Specific criteria for success |
| `ontology_schema` | `OntologySchema` | Schema for workflow outputs |
| `evaluation_principles` | `tuple[EvaluationPrinciple, ...]` | Principles for evaluation |
| `exit_conditions` | `tuple[ExitCondition, ...]` | Conditions for termination |
| `metadata` | `SeedMetadata` | Generation metadata |

#### Methods

##### `to_dict() -> dict[str, Any]`

Convert seed to dictionary for serialization.

##### `Seed.from_dict(data: dict[str, Any]) -> Seed`

Create seed from dictionary.

### Class: `SeedMetadata`

Metadata about the Seed generation.

```python
class SeedMetadata(BaseModel, frozen=True):
    seed_id: str  # Default: auto-generated
    version: str  # Default: "1.0.0"
    created_at: datetime  # Default: now
    ambiguity_score: float  # Required, 0.0 to 1.0
    interview_id: str | None
```

### Class: `OntologySchema`

Schema defining the structure of workflow outputs.

```python
class OntologySchema(BaseModel, frozen=True):
    name: str
    description: str
    fields: tuple[OntologyField, ...]
```

### Class: `OntologyField`

A field in the ontology schema.

```python
class OntologyField(BaseModel, frozen=True):
    name: str
    field_type: str  # "string", "number", "boolean", "array", "object"
    description: str
    required: bool  # Default: True
```

### Class: `EvaluationPrinciple`

A principle for evaluating workflow outputs.

```python
class EvaluationPrinciple(BaseModel, frozen=True):
    name: str
    description: str
    weight: float  # Default: 1.0, range 0.0 to 1.0
```

### Class: `ExitCondition`

Defines when the workflow should terminate.

```python
class ExitCondition(BaseModel, frozen=True):
    name: str
    description: str
    evaluation_criteria: str
```

### Example: Creating a Seed

```python
from ouroboros.core import (
    Seed,
    SeedMetadata,
    OntologySchema,
    OntologyField,
    EvaluationPrinciple,
    ExitCondition,
)

seed = Seed(
    goal="Build a CLI task management tool",
    constraints=(
        "Python 3.14+",
        "No external database dependencies",
    ),
    acceptance_criteria=(
        "Tasks can be created with title and due date",
        "Tasks can be listed",
        "Tasks can be marked as complete",
    ),
    ontology_schema=OntologySchema(
        name="TaskManager",
        description="Task management ontology",
        fields=(
            OntologyField(
                name="tasks",
                field_type="array",
                description="List of tasks",
            ),
        ),
    ),
    evaluation_principles=(
        EvaluationPrinciple(
            name="completeness",
            description="All requirements are met",
            weight=1.0,
        ),
    ),
    exit_conditions=(
        ExitCondition(
            name="all_criteria_met",
            description="All acceptance criteria satisfied",
            evaluation_criteria="100% criteria pass",
        ),
    ),
    metadata=SeedMetadata(ambiguity_score=0.15),
)

# Seed is immutable - this raises an error:
# seed.goal = "New goal"  # ValidationError

# Serialize to dict
data = seed.to_dict()

# Deserialize from dict
restored_seed = Seed.from_dict(data)
```

---

## Type Aliases

### `EventPayload`

Type alias for event payload data - arbitrary JSON-serializable dict.

```python
EventPayload = dict[str, Any]
```

### `CostUnits`

Type alias for cost tracking - integer units (e.g., token counts).

```python
CostUnits = int
```

### `DriftScore`

Type alias for drift measurement - float between 0.0 and 1.0.

```python
DriftScore = float
```
