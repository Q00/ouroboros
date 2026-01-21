# Story 0.2: Core Types and Error Handling

Status: review

## Story

As a developer,
I want a consistent Result type and error hierarchy,
so that all components handle errors uniformly without exceptions for expected failures.

## Acceptance Criteria

1. Result[T, E] generic type implemented with is_ok/is_err properties
2. Result.ok() and Result.err() class methods for construction
3. OuroborosError base exception class created
4. Specific error types: ProviderError, ConfigError, PersistenceError, ValidationError
5. Type aliases defined: EventPayload, CostUnits, DriftScore
6. All types are fully typed with mypy --strict compatibility
7. Result type includes unwrap(), unwrap_or(default), map(fn), and map_err(fn) methods

## Tasks / Subtasks

- [x] Task 1: Implement Result type (AC: 1, 2, 7)
  - [x] Create core/types.py module
  - [x] Implement Result[T, E] dataclass with Generic
  - [x] Add is_ok, is_err properties
  - [x] Add ok(), err() class methods
  - [x] Implement unwrap() method (raises if err)
  - [x] Implement unwrap_or(default) method
  - [x] Implement map(fn) for transforming Ok values
  - [x] Implement map_err(fn) for transforming Err values
- [x] Task 2: Implement error hierarchy (AC: 3, 4)
  - [x] Create core/errors.py module
  - [x] Implement OuroborosError base class
  - [x] Implement ProviderError for LLM failures
  - [x] Implement ConfigError for config issues
  - [x] Implement PersistenceError for DB issues
  - [x] Implement ValidationError for schema failures
- [x] Task 3: Define type aliases (AC: 5)
  - [x] Add EventPayload = dict[str, Any]
  - [x] Add CostUnits = int
  - [x] Add DriftScore = float
- [x] Task 4: Ensure mypy compatibility (AC: 6)
  - [x] Run mypy --strict on core/ modules
  - [x] Fix any type errors
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage
  - [x] Test error cases

## Dev Notes

- Result type pattern replaces exceptions for expected failures (rate limits, API errors)
- Exceptions only for programming errors (bugs)
- Pattern matching on Result for flow control
- Keep types.py focused, errors.py separate

### Project Structure Notes

```
src/ouroboros/core/
├── __init__.py
├── types.py      # Result, type aliases
└── errors.py     # OuroborosError hierarchy
```

### Dependencies

**Requires:**
- Story 0-1: Project Initialization with uv

**Required By:**
- Story 0-3: Event Store with SQLAlchemy Core
- Story 0-5: Structured Logging with structlog
- Story 0-8: ProviderRouter and LiteLLM Integration
- Story 0-9: Circuit Breaker and Retry Logic

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Error-Handling-Result-Type]
- [Source: _bmad-output/planning-artifacts/architecture.md#core/types.py]

## Dev Agent Record

### Agent Model Used

Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References

N/A - No debugging issues encountered

### Completion Notes List

- Implemented Result[T, E] generic type using Python 3.14 type parameter syntax with frozen dataclass
- Result type supports: ok()/err() construction, is_ok/is_err properties, unwrap()/unwrap_or(default), map(fn)/map_err(fn)
- Implemented OuroborosError base class with message and details attributes
- Implemented specific error types with domain-relevant attributes:
  - ProviderError: provider, status_code, from_exception() class method
  - ConfigError: config_key, config_file
  - PersistenceError: operation, table
  - ValidationError: field, value
- Type aliases: EventPayload = dict[str, Any], CostUnits = int, DriftScore = float
- All code passes mypy --strict validation
- 100% test coverage achieved (38 tests for core module)
- All 59 tests pass with no regressions

### File List

**New Files:**
- src/ouroboros/core/__init__.py
- src/ouroboros/core/types.py
- src/ouroboros/core/errors.py
- tests/unit/core/__init__.py
- tests/unit/core/test_types.py
- tests/unit/core/test_errors.py

## Change Log

- 2026-01-16: Implemented core types and error handling (Story 0.2)
  - Created Result[T, E] generic type with full API
  - Created OuroborosError hierarchy with 4 specific error types
  - Defined type aliases for EventPayload, CostUnits, DriftScore
  - Achieved 100% test coverage with 38 unit tests
