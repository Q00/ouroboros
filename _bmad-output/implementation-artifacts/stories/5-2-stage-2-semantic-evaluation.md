# Story 5.2: Stage 2 - Semantic Evaluation

Status: ready-for-dev

## Story

As a developer,
I want LLM-based semantic evaluation,
so that outputs are verified for goal alignment and AC compliance.

## Acceptance Criteria

1. AC compliance verified via LLM
2. Goal alignment scored (0.0 - 1.0)
3. Drift from original intent measured
4. Uses Standard tier (10x cost)
5. Returns pass/fail with explanation
6. Uncertainty > 0.3 triggers Stage 3
7. Standard tier unavailability falls back to Frugal tier with increased scrutiny threshold (0.2 instead of 0.3)

## Tasks / Subtasks

- [ ] Task 1: Implement semantic evaluator (AC: 1, 2, 3)
  - [ ] Create evaluation/semantic.py
  - [ ] Implement evaluate() method
  - [ ] Check AC compliance
  - [ ] Score goal alignment
  - [ ] Measure drift
- [ ] Task 2: Configure tier usage (AC: 4)
  - [ ] Force Standard tier
  - [ ] Track cost
- [ ] Task 3: Handle uncertainty (AC: 5, 6)
  - [ ] Return detailed explanation
  - [ ] Trigger Stage 3 if uncertainty > 0.3
- [ ] Task 4: Handle Standard tier fallback (AC: 7)
  - [ ] Detect Standard tier unavailability (rate limit, error)
  - [ ] Fall back to Frugal tier
  - [ ] Lower scrutiny threshold to 0.2 (instead of 0.3)
  - [ ] Log fallback event with reason
- [ ] Task 5: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- $$ cost (Standard tier)
- Always runs after Stage 1 pass
- Measures semantic quality

### Dependencies

**Requires:**
- Story 5-1 (Stage 1 must pass first)
- Story 0-5 (LLM)
- Story 2-1 (Standard tier)

**Required By:**
- Story 5-3

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Stage-2-Semantic-Evaluation]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
