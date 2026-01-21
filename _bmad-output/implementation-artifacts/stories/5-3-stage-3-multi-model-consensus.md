# Story 5.3: Stage 3 - Multi-Model Consensus

Status: ready-for-dev

## Story

As a developer,
I want multi-model consensus for critical decisions,
so that important outputs have diverse verification.

## Acceptance Criteria

1. Three different models evaluate output
2. 2/3 majority agreement required
3. Disagreements logged with reasoning
4. Uses Frontier tier (30x cost)
5. Only triggered by consensus conditions
6. Structured concurrency with timeouts
7. Single model unavailability falls back to 2-model consensus (requires unanimous agreement)
8. Two+ models unavailable aborts consensus and escalates to human review

## Tasks / Subtasks

- [ ] Task 1: Implement consensus protocol (AC: 1, 2)
  - [ ] Create consensus/protocol.py
  - [ ] Query 3 different models
  - [ ] Aggregate votes
  - [ ] Determine 2/3 majority
- [ ] Task 2: Handle disagreements (AC: 3)
  - [ ] Log each model's reasoning
  - [ ] Capture disagreement details
- [ ] Task 3: Configure execution (AC: 4, 6)
  - [ ] Use Frontier tier
  - [ ] asyncio.gather with timeouts
  - [ ] Fallback to sequential on partial failure
- [ ] Task 4: Handle model unavailability (AC: 7, 8)
  - [ ] Detect single model failure (timeout, error)
  - [ ] Fall back to 2-model consensus requiring unanimous agreement
  - [ ] Detect two+ model failures
  - [ ] Abort consensus and escalate to human review
  - [ ] Log unavailability events with model names and error details
- [ ] Task 5: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- $$$$ cost (Frontier tier)
- Only on trigger conditions
- Never for routine evaluation

### Dependencies

**Requires:**
- Story 5-2
- Story 2-1 (Frontier tier)

**Required By:**
- Story 5-4 (trigger matrix)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-5-Consensus-Protocol]
- [Source: _bmad-output/planning-artifacts/architecture.md#Consensus-Protocol-Structured-Concurrency]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
