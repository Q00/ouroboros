# Story 5.4: Consensus Trigger Matrix

Status: ready-for-dev

## Story

As a developer,
I want clear rules for when consensus is required,
so that expensive multi-model evaluation is used appropriately.

## Acceptance Criteria

1. Seed modification triggers consensus
2. Ontology evolution triggers consensus
3. Goal interpretation changes trigger consensus
4. Seed Drift Alert (drift > 0.3) triggers consensus
5. Stage 2 Uncertainty (> 0.3) triggers consensus
6. Lateral Thinking Adoption triggers consensus

## Tasks / Subtasks

- [ ] Task 1: Define trigger conditions (AC: 1-6)
  - [ ] Create consensus/triggers.py
  - [ ] Define ConsensusTrigger enum
  - [ ] Implement check_trigger() function
- [ ] Task 2: Integrate with evaluation (AC: 4, 5)
  - [ ] Check drift threshold
  - [ ] Check uncertainty threshold
- [ ] Task 3: Integrate with execution (AC: 1, 2, 3, 6)
  - [ ] Detect Seed modifications
  - [ ] Detect Ontology changes
  - [ ] Detect goal reinterpretation
  - [ ] Detect lateral adoption
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- 6 trigger conditions total
- Prevent consensus abuse
- Cost control mechanism

### Dependencies

**Requires:**
- Story 5-3 (consensus)
- Story 6-1 (drift measurement)

**Required By:**
- None (defines when to invoke 5-3)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Consensus-Trigger-Matrix]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
