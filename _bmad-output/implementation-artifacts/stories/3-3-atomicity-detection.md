# Story 3.3: Atomicity Detection

Status: ready-for-dev

## Story

As a developer,
I want clear atomicity criteria,
so that decomposition stops at the right granularity.

## Acceptance Criteria

1. Complexity below threshold = atomic
2. Single-tool solvability = atomic
3. Duration below limit = atomic
4. Atomicity check before each DD cycle
5. Atomicity criteria configurable

## Tasks / Subtasks

- [ ] Task 1: Define atomicity criteria (AC: 1, 2, 3)
  - [ ] Create execution/atomicity.py
  - [ ] Implement complexity check
  - [ ] Implement tool count check
  - [ ] Implement duration estimate
- [ ] Task 2: Implement detection (AC: 4)
  - [ ] Check before DD cycle
  - [ ] Return boolean is_atomic
- [ ] Task 3: Make configurable (AC: 5)
  - [ ] Define AtomicityConfig Pydantic model with fields:
    - complexity_threshold: float (default 0.7)
    - max_tool_count: int (default 3)
    - max_duration_seconds: int (default 300)
  - [ ] Add atomicity section to config.yaml
  - [ ] Load thresholds from config on startup
  - [ ] Allow per-Seed override via seed.atomicity_overrides
  - [ ] Merge seed overrides with global config at runtime
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Atomic = execute directly, no DD overhead
- Non-atomic = run full DD cycle
- Balance granularity vs overhead

### Dependencies

**Requires:**
- Story 3-1 (Double Diamond Cycle Implementation)

**Required By:**
- Story 3-2 (used to determine if decomposition needed)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Atomicity-Criteria]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
