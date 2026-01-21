# Story 4.1: Stagnation Detection (4 Patterns)

Status: ready-for-dev

## Story

As a developer,
I want automatic stagnation detection,
so that stuck workflows are identified early.

## Acceptance Criteria

1. Spinning pattern detected (same output repeated)
2. Oscillation pattern detected (A→B→A→B)
3. No Drift pattern detected (no progress toward goal)
4. Diminishing Returns detected (progress slowing)
5. Detection runs after each iteration
6. Stagnation triggers resilience response

## Tasks / Subtasks

- [ ] Task 1: Define patterns (AC: 1, 2, 3, 4)
  - [ ] Create resilience/patterns.py
  - [ ] Define StagnationPattern enum
  - [ ] Implement pattern detection logic
- [ ] Task 2: Implement detector (AC: 5)
  - [ ] Create resilience/stagnation.py
  - [ ] Run detection after each iteration
  - [ ] Track history for pattern matching
- [ ] Task 3: Trigger response (AC: 6)
  - [ ] Signal stagnation to execution engine
  - [ ] Enable lateral thinking fallback
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Deterministic fixtures for testing
- No timing dependencies
- Part of Phase 3
- Pattern detection thresholds:
  - Spinning: 3+ identical outputs in a row
  - Oscillation: A→B→A pattern detected within 6 iterations
  - No Drift: drift_delta < 0.01 for 5 consecutive iterations
  - Diminishing Returns: progress rate < 10% of initial rate

### Dependencies

**Requires:**
- Story 0-3 (EventStore for history tracking)

**Required By:**
- Story 4-3 (rotation strategy needs detection)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-3-Resilience-System]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
