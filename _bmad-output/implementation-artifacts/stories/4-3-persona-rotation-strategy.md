# Story 4.3: Persona Rotation Strategy

Status: ready-for-dev

## Story

As a developer,
I want intelligent persona selection,
so that the most appropriate lateral thinking approach is tried first.

## Acceptance Criteria

1. Selection considers stagnation pattern type
2. Previously failed personas deprioritized
3. Rotation continues until progress
4. All personas exhausted triggers human intervention
5. Selection history tracked

## Tasks / Subtasks

- [ ] Task 1: Implement selection logic (AC: 1)
  - [ ] Match stagnation pattern to best persona
  - [ ] Spinning → Contrarian
  - [ ] Oscillation → Architect
  - [ ] No Drift → Researcher
  - [ ] Diminishing → Simplifier
- [ ] Task 2: Track history (AC: 2, 5)
  - [ ] Track failed personas per stagnation
  - [ ] Deprioritize failed ones
- [ ] Task 3: Handle exhaustion (AC: 3, 4)
  - [ ] Rotate through remaining personas
  - [ ] Signal human intervention if all fail:
    - Display Rich panel with "Human Intervention Required"
    - Show stagnation pattern and failed personas
    - Pause execution awaiting user input
    - Log intervention request with full context
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Smart selection reduces attempts
- Never infinite retry
- Human intervention as last resort

### Dependencies

**Requires:**
- Story 4-1 (stagnation detection)
- Story 4-2 (personas)

**Required By:**
- None (coordinates 4-1 and 4-2)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Persona-Selection]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
