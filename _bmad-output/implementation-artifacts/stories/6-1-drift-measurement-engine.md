# Story 6.1: Drift Measurement Engine

Status: ready-for-dev

## Story

As a developer,
I want continuous drift calculation,
so that deviation from original goals is quantified.

## Acceptance Criteria

1. Goal drift calculated (deviation from objectives)
2. Constraint drift calculated (constraint violations)
3. Ontology drift calculated (concept evolution)
4. Combined drift uses weighted formula: (goal * 0.5) + (constraint * 0.3) + (ontology * 0.2)
5. Combined drift threshold ≤ 0.3 (NFR5)
6. Drift measured after each iteration
7. Drift events stored in event log

## Tasks / Subtasks

- [ ] Task 1: Implement drift calculations (AC: 1, 2, 3)
  - [ ] Create observability/drift.py
  - [ ] Implement calculate_goal_drift()
  - [ ] Implement calculate_constraint_drift()
  - [ ] Implement calculate_ontology_drift()
- [ ] Task 2: Combine and threshold (AC: 4, 5)
  - [ ] Calculate combined drift using weighted formula
  - [ ] Apply weights: goal=0.5, constraint=0.3, ontology=0.2
  - [ ] Check combined score against 0.3 threshold
  - [ ] Alert on threshold breach
- [ ] Task 3: Add observability (AC: 6, 7)
  - [ ] Measure drift after each iteration
  - [ ] Store drift events with all component scores
  - [ ] Include goal_drift, constraint_drift, ontology_drift, combined_drift in event payload
- [ ] Task 4: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Drift = deviation from Seed
- ≤ 0.3 is acceptable
- > 0.3 may trigger consensus
- Drift formula: combined = (goal_drift * 0.5) + (constraint_drift * 0.3) + (ontology_drift * 0.2)

### Dependencies

**Requires:**
- Story 0-3 (EventStore)
- Story 1-3 (Seed for comparison)

**Required By:**
- Story 5-4
- Story 6-2

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Drift-Measurement]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
