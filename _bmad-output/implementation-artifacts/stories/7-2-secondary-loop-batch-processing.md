# Story 7.2: Secondary Loop Batch Processing

Status: ready-for-dev

## Story

As a developer,
I want automatic TODO processing after primary goals,
so that improvements are implemented efficiently.

## Acceptance Criteria

1. Secondary loop activates after primary goal achieved
2. TODOs processed in priority order
3. Each TODO runs through execution path
4. Batch processing optimizes efficiency
5. Progress tracked and logged
6. Optional: user can skip secondary loop
7. Failed TODO items are marked as failed with error details, not blocking other TODOs
8. Batch processing summary shows success/failure counts

## Tasks / Subtasks

- [ ] Task 1: Detect primary completion (AC: 1)
  - [ ] Create secondary/scheduler.py
  - [ ] Detect goal achievement
  - [ ] Trigger secondary loop
- [ ] Task 2: Process TODOs (AC: 2, 3)
  - [ ] Sort by priority
  - [ ] Execute each TODO
  - [ ] Use appropriate tier
- [ ] Task 3: Optimize batching (AC: 4, 5)
  - [ ] Group similar TODOs
  - [ ] Track progress
  - [ ] Log completion
- [ ] Task 4: Add user control (AC: 6)
  - [ ] Allow skip option via --skip-secondary flag
  - [ ] Defer to next session
- [ ] Task 5: Handle TODO failures (AC: 7, 8)
  - [ ] Catch exceptions during TODO execution
  - [ ] Mark failed TODO with status=FAILED and error details
  - [ ] Continue processing remaining TODOs (non-blocking)
  - [ ] Generate batch summary:
    - Total TODOs processed
    - Success count
    - Failure count with error list
    - Skipped count
- [ ] Task 6: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Only after primary success
- Batch for efficiency
- Optional execution

### Dependencies

**Requires:**
- Story 7-1 (TODO registry)
- Story 3-1 (execution path)

**Required By:**
- None (end of chain)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Secondary-Loop-Batch-Processing]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
