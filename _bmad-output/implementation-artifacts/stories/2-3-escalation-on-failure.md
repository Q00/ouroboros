# Story 2.3: Escalation on Failure

Status: completed

## Story

As a developer,
I want automatic escalation when tasks fail,
so that difficult tasks eventually get the power they need.

## Acceptance Criteria

1. Track consecutive failures per task pattern
2. Escalate to next tier after 2 consecutive failures
3. Escalation events logged with cost impact
4. Frontier tier failures emit STAGNATION_DETECTED event for resilience system to handle
5. Escalation resets on success

## Tasks / Subtasks

- [x] Task 1: Track failure state (AC: 1, 5)
  - [x] Create routing/escalation.py
  - [x] Track consecutive_failures per pattern
  - [x] Reset on success
- [x] Task 2: Implement escalation logic (AC: 2)
  - [x] Check failure count >= 2
  - [x] Upgrade to next tier
- [x] Task 3: Handle Frontier escalation (AC: 4)
  - [x] Detect when already at Frontier
  - [x] Trigger lateral thinking path (emit STAGNATION_DETECTED event)
- [x] Task 4: Add logging (AC: 3)
  - [x] Log escalation events
  - [x] Include cost impact in log
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (achieved 100%)
  - [x] Test error cases

## Dev Notes

- 2 failures = escalate (from PRD)
- Frugal → Standard → Frontier → Lateral
- Never infinite retry (anti-pattern)
- Note: Lateral thinking integration handled by Epic 4 (Resilience). This story emits events only.

### Dependencies

**Requires:**
- Story 2-2 (router)

**Required By:**
- Story 4-1 (via events)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Escalation-Rules]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5

### Debug Log References
N/A

### Completion Notes List
- Implemented FailureTracker dataclass with consecutive_failures tracking and reset_on_success()
- Implemented EscalationManager class with tier escalation logic (Frugal -> Standard -> Frontier)
- Implemented StagnationEvent for STAGNATION_DETECTED event emission when Frontier fails
- Added comprehensive logging with cost impact information
- Achieved 100% test coverage with 40 test cases

### File List
- src/ouroboros/routing/escalation.py (new)
- src/ouroboros/routing/__init__.py (updated)
- tests/unit/routing/test_escalation.py (new)
