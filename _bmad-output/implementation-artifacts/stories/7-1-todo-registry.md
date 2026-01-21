# Story 7.1: TODO Registry

Status: ready-for-dev

## Story

As a developer,
I want to capture discovered improvements without disrupting flow,
so that good ideas aren't lost but don't derail primary work.

## Acceptance Criteria

1. TODOs registered during execution
2. Each TODO has context and priority
3. TODOs don't interrupt primary execution
4. TODOs visible in status reports
5. TODOs persisted to database
6. TODO count tracked in metrics

## Tasks / Subtasks

- [ ] Task 1: Define TODO model (AC: 2)
  - [ ] Create secondary/todo_registry.py
  - [ ] Define TODO model with fields:
    - id: UUID
    - description: str
    - context: str (where discovered)
    - priority: Enum (HIGH, MEDIUM, LOW)
    - created_at: datetime
    - status: Enum (PENDING, IN_PROGRESS, DONE, SKIPPED)
- [ ] Task 2: Implement registry (AC: 1, 3, 5)
  - [ ] Implement register() method
  - [ ] Non-blocking registration
  - [ ] Persist to event store
- [ ] Task 3: Add visibility (AC: 4, 6)
  - [ ] Include in status command
  - [ ] Track count in metrics
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Defer non-critical improvements
- Avoid "yak shaving"
- Process after primary goal

### Dependencies

**Requires:**
- Story 0-3 (EventStore for persistence)

**Required By:**
- Story 7-2

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-6-Secondary-Loop]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
