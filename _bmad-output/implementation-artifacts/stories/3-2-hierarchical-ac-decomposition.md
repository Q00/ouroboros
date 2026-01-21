# Story 3.2: Hierarchical AC Decomposition

Status: ready-for-dev

## Story

As a developer,
I want automatic decomposition of complex ACs,
so that large tasks are broken into manageable atomic units.

## Acceptance Criteria

1. Non-atomic ACs detected and decomposed
2. Decomposition at Define phase
3. Child ACs run own Double Diamond
4. Max depth 5 levels (NFR10)
5. Parent-child relationship tracked
6. Compression at depth 3+
7. Decomposition failures (max depth reached, cyclic detection) halt with clear error and parent AC context

## Tasks / Subtasks

- [ ] Task 1: Implement decomposition (AC: 1, 2)
  - [ ] Create execution/decomposition.py
  - [ ] Detect non-atomic ACs
  - [ ] Generate child ACs
- [ ] Task 2: Build AC tree (AC: 5)
  - [ ] Create core/ac.py with ACNode, ACTree
  - [ ] Track parent-child relationships
- [ ] Task 3: Enforce limits (AC: 4, 6)
  - [ ] Check depth limit
  - [ ] Apply compression at depth 3+
- [ ] Task 4: Handle decomposition failures (AC: 7)
  - [ ] Detect max depth reached condition
  - [ ] Detect cyclic decomposition (child equals parent)
  - [ ] Halt with DecompositionError including parent AC context
  - [ ] Log failure with full AC path for debugging
- [ ] Task 5: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Decomposition is recursive
- Stop at atomic or max depth
- Depth 3+ gets compressed context

### Dependencies

**Requires:**
- Story 3-1 (Double Diamond Cycle Implementation)

**Required By:**
- None (leaf story in Epic 3)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#AC-Decomposition]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
