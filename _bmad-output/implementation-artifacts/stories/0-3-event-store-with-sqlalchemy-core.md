# Story 0.3: Event Store with SQLAlchemy Core

Status: review

## Story

As a developer,
I want an event sourcing infrastructure,
so that all state changes are captured as immutable events for replay and debugging.

## Acceptance Criteria

1. Single unified `events` table created with SQLAlchemy Core
2. Table schema: id, aggregate_type, aggregate_id, event_type, payload (JSON), timestamp, consensus_id
3. Indexes on aggregate_type and aggregate_id for efficient queries
4. EventStore class with append() and replay() async methods
5. All operations use aiosqlite for async support
6. Event type naming follows dot.notation.past_tense convention
7. append() and replay() operations use database transactions for atomicity

## Tasks / Subtasks

- [x] Task 1: Define database schema (AC: 1, 2, 3)
  - [x] Create persistence/schema.py
  - [x] Define events Table with all columns
  - [x] Add composite indexes
- [x] Task 2: Implement EventStore class (AC: 4, 5, 7)
  - [x] Create persistence/event_store.py
  - [x] Implement async append(event) method
  - [x] Implement async replay(aggregate_id) method
  - [x] Use create_async_engine with aiosqlite
  - [x] Wrap append() in transaction with rollback on failure
  - [x] Wrap replay() in read-only transaction for consistency
- [x] Task 3: Define base event structure (AC: 6)
  - [x] Create events/base.py
  - [x] Implement BaseEvent Pydantic model (frozen=True)
  - [x] Include id, type, timestamp, aggregate_type, aggregate_id fields
- [x] Task 4: Create initial migration (AC: 1)
  - [x] Create persistence/migrations/scripts/001_initial.sql
  - [x] Implement migration runner
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage
  - [x] Test error cases

## Dev Notes

- SQLAlchemy Core only (no ORM) - query builder for flexibility
- Event types: "ontology.concept.added", "execution.ac.completed", etc.
- All events immutable (frozen Pydantic models)
- Convert to Pydantic at repository boundaries

### Project Structure Notes

```
src/ouroboros/
├── persistence/
│   ├── __init__.py
│   ├── schema.py          # Table definitions
│   ├── event_store.py     # EventStore class
│   └── migrations/
│       ├── __init__.py
│       ├── runner.py
│       └── scripts/
│           └── 001_initial.sql
└── events/
    ├── __init__.py
    └── base.py            # BaseEvent
```

### Dependencies

**Requires:**
- Story 0-1: Project Initialization with uv
- Story 0-2: Core Types and Error Handling

**Required By:**
- Story 0-8: ProviderRouter and LiteLLM Integration
- Story 4-1: Ontology Event Store (Epic 4)
- Story 6-1: Execution Event Store (Epic 6)
- Story 7-1: Integration Event Store (Epic 7)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Event-Store-Single-Table]
- [Source: _bmad-output/planning-artifacts/architecture.md#Event-Payload-Structure]

## Dev Agent Record

### Agent Model Used

Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References

N/A - No debugging issues encountered

### Completion Notes List

- Implemented `events` table with SQLAlchemy Core: id, aggregate_type, aggregate_id, event_type, payload (JSON), timestamp, consensus_id
- Added 5 indexes: aggregate_type, aggregate_id, composite (aggregate_type, aggregate_id), event_type, timestamp
- Implemented EventStore class with async append() and replay() methods using aiosqlite
- Both operations use transactions (via engine.begin() context manager) for atomicity
- Implemented BaseEvent as frozen Pydantic model with dot.notation.past_tense convention
- Added to_db_dict() and from_db_row() for database serialization
- Created migration script (001_initial.sql) and runner
- 33 tests for persistence/events modules, 89% coverage on event_store.py
- All 92 project tests pass with no regressions

### File List

**New Files:**
- src/ouroboros/persistence/__init__.py
- src/ouroboros/persistence/schema.py
- src/ouroboros/persistence/event_store.py
- src/ouroboros/persistence/migrations/__init__.py
- src/ouroboros/persistence/migrations/runner.py
- src/ouroboros/persistence/migrations/scripts/001_initial.sql
- src/ouroboros/events/__init__.py
- src/ouroboros/events/base.py
- tests/unit/persistence/__init__.py
- tests/unit/persistence/test_schema.py
- tests/unit/persistence/test_event_store.py
- tests/unit/events/__init__.py
- tests/unit/events/test_base.py

## Change Log

- 2026-01-16: Implemented event sourcing infrastructure (Story 0.3)
  - Created events table with SQLAlchemy Core
  - Implemented EventStore with async append/replay
  - Created BaseEvent frozen Pydantic model
  - Added 33 unit tests for persistence/events
