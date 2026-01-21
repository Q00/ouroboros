# Story 6.2: Automatic Retrospective

Status: ready-for-dev

## Story

As a developer,
I want periodic direction checks,
so that accumulated drift is caught and corrected.

## Acceptance Criteria

1. Retrospective triggers every 3 iterations
2. Current state compared to original Seed
3. Drift components analyzed
4. Course correction recommendations generated
5. High drift triggers human notification
6. Retrospective results logged

## Tasks / Subtasks

- [ ] Task 1: Implement trigger (AC: 1)
  - [ ] Track iteration count starting from 1
  - [ ] Trigger at iterations 3, 6, 9, ... (multiples of 3)
  - [ ] First retrospective runs after iteration 3 completes
- [ ] Task 2: Compare and analyze (AC: 2, 3)
  - [ ] Load original Seed
  - [ ] Compare current state
  - [ ] Break down drift components
- [ ] Task 3: Generate recommendations (AC: 4, 5)
  - [ ] Suggest corrections
  - [ ] Notify if high drift
- [ ] Task 4: Log results (AC: 6)
  - [ ] Store retrospective event
  - [ ] Include all analysis
- [ ] Task 5: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Every 3 iterations per PRD
- Part of Phase 3 resilience
- Prevents accumulated drift

### Dependencies

**Requires:**
- Story 6-1 (drift measurement)
- Story 0-5 (LLM for analysis)

**Required By:**
- None (end of chain)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Retrospective]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
