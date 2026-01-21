# Story 5.1: Stage 1 - Mechanical Verification

Status: ready-for-dev

## Story

As a developer,
I want zero-cost mechanical checks,
so that obvious issues are caught before expensive LLM evaluation.

## Acceptance Criteria

1. Lint checks applied (ruff)
2. Build validation performed
3. Tests executed
4. Static analysis runs (mypy)
5. Coverage threshold ≥ 0.7 verified (NFR9)
6. Stage 1 is $0 cost (no LLM calls)
7. Missing tools (ruff, mypy, pytest) detected with clear error message listing installation commands

## Tasks / Subtasks

- [ ] Task 1: Create evaluation pipeline (AC: 6)
  - [ ] Create evaluation/pipeline.py
  - [ ] Define EvaluationPipeline class
  - [ ] Implement stage progression
- [ ] Task 2: Implement mechanical checks (AC: 1, 2, 3, 4, 7)
  - [ ] Create evaluation/mechanical.py
  - [ ] Run ruff lint
  - [ ] Run build command
  - [ ] Run pytest
  - [ ] Run mypy
  - [ ] Check tool availability before running
  - [ ] Provide helpful error if tool missing
- [ ] Task 3: Check coverage (AC: 5)
  - [ ] Parse pytest-cov output
  - [ ] Verify ≥ 0.7 threshold
  - [ ] Fail if below threshold
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- $0 cost - no LLM involved
- Always runs first
- Gate for Stage 2

### Dependencies

**Requires:**
- Story 0-1 (project with tooling: ruff, mypy, pytest)

**Required By:**
- Story 5-2 (pipeline gate)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-4-Evaluation-Pipeline]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
