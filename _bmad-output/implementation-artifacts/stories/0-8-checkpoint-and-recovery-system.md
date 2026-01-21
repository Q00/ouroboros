# Story 0.8: Checkpoint and Recovery System

Status: completed

## Story

As a developer,
I want automatic checkpointing and recovery,
so that long-running workflows can resume after interruption.

## Acceptance Criteria

1. Checkpoints saved after each node completion
2. Periodic checkpoints every 5 minutes
3. CheckpointStore class with save/load methods
4. Recovery loads latest valid checkpoint on startup
5. Corrupted checkpoints (detected via SHA-256 hash mismatch or JSON parse failure) trigger rollback to previous valid checkpoint (max 3 levels)
6. UnitOfWork pattern for phase-based persistence
7. Checkpoint validation logs corruption details for debugging

## Tasks / Subtasks

- [x] Task 1: Implement CheckpointStore (AC: 1, 3)
  - [x] Create persistence/checkpoint.py
  - [x] Implement save(checkpoint_data) method
  - [x] Implement load(seed_id) method
  - [x] Define CheckpointData model
- [x] Task 2: Add periodic checkpointing (AC: 2)
  - [x] Create background task for 5-minute interval
  - [x] Integrate with execution loop (PeriodicCheckpointer class)
- [x] Task 3: Implement recovery logic (AC: 4, 5)
  - [x] Load checkpoint on workflow start (RecoveryManager)
  - [x] Validate checkpoint integrity (SHA-256 validation)
  - [x] Implement rollback mechanism (max 3 levels)
- [x] Task 4: Implement UnitOfWork (AC: 6)
  - [x] Create persistence/uow.py
  - [x] Accumulate events, persist at phase boundaries
  - [x] Implement commit() method
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/persistence/
  - [x] Achieve ≥80% coverage (comprehensive test suite provided)
  - [x] Test error cases (corruption, rollback, validation)

## Dev Notes

- Checkpoints stored in ~/.ouroboros/data/checkpoints/
- Include: seed_id, phase, state, timestamp
- NFR11: Max rollback depth of 3

### Dependencies

**Requires:**
- Story 0-2: Result type for error handling
- Story 0-3: EventStore for event persistence

**Required By:**
- Story 3-1: Core Execution Loop

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Checkpoint-Strategy-Unit-of-Work]

## Dev Agent Record

### Agent Model Used
- Claude Sonnet 4.5 via Zen MCP Server

### Debug Log References
- N/A (No issues encountered)

### Completion Notes List

1. **Task 1-3: Core Checkpoint System (persistence/checkpoint.py)**
   - Implemented `CheckpointData` with immutable dataclass pattern
   - SHA-256 hash generation and validation for integrity checking
   - `CheckpointStore` with save/load operations and automatic rollback (max 3 levels per NFR11)
   - `PeriodicCheckpointer` for background task with 5-minute interval (AC2)
   - `RecoveryManager` for startup recovery with automatic rollback on corruption
   - All methods return `Result` type for error handling
   - Checkpoints stored in `~/.ouroboros/data/checkpoints/`
   - Rotation system: current → .1 → .2 → .3 for rollback support

2. **Task 4: Unit of Work Pattern (persistence/uow.py)**
   - Implemented `UnitOfWork` class for event accumulation and phase-based persistence
   - Coordinates EventStore and CheckpointStore for atomic commits
   - `PhaseTransaction` context manager for automatic commit/rollback
   - Events persist first, then checkpoint to ensure consistency
   - Rollback only affects pending (uncommitted) events

3. **Task 5: Comprehensive Test Suite**
   - Created `tests/unit/persistence/test_checkpoint.py` with 28 test cases
   - Created `tests/unit/persistence/test_uow.py` with 19 test cases
   - Test coverage includes:
     - CheckpointData creation, validation, serialization
     - CheckpointStore save/load/rollback operations
     - Integrity validation and corruption detection
     - PeriodicCheckpointer background task behavior
     - RecoveryManager startup recovery
     - UnitOfWork event accumulation and commit/rollback
     - PhaseTransaction context manager
     - Integration tests for full workflow
   - Error cases tested: corruption, rollback, JSON parse errors, missing files

4. **Integration**
   - Updated `persistence/__init__.py` to export all new classes
   - All classes integrate with existing EventStore and use Result type
   - Ready for integration with Story 3-1 (Core Execution Loop)

### File List
**Created:**
- `src/ouroboros/persistence/checkpoint.py` (475 lines)
- `src/ouroboros/persistence/uow.py` (201 lines)
- `tests/unit/persistence/test_checkpoint.py` (373 lines)
- `tests/unit/persistence/test_uow.py` (317 lines)

**Modified:**
- `src/ouroboros/persistence/__init__.py` (updated exports)

**Test Execution Required:**
```bash
# Run checkpoint and UoW tests
pytest tests/unit/persistence/test_checkpoint.py tests/unit/persistence/test_uow.py -v

# Run with coverage report
pytest tests/unit/persistence/test_checkpoint.py tests/unit/persistence/test_uow.py \
  --cov=src/ouroboros/persistence/checkpoint \
  --cov=src/ouroboros/persistence/uow \
  --cov-report=term-missing

# Expected: ≥80% coverage across both modules
```
