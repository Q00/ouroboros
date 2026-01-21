# Story 1.2: Ambiguity Score Calculation

Status: completed

## Story

As a developer,
I want automatic ambiguity measurement,
so that I know when my requirements are clear enough to proceed.

## Acceptance Criteria

1. Ambiguity score calculated (0.0 - 1.0)
2. Score ≤ 0.2 allows Seed generation (NFR6)
3. Score > 0.2 triggers additional questions
4. Score breakdown displays component weights (goal: 40%, constraint: 30%, success criteria: 30%) with justification text for each
5. Score components: goal clarity, constraint clarity, success criteria clarity
6. Score displayed after each round

## Tasks / Subtasks

- [x] Task 1: Implement scoring algorithm (AC: 1, 5)
  - [x] Create bigbang/ambiguity.py
  - [x] Calculate goal_clarity score
  - [x] Calculate constraint_clarity score
  - [x] Calculate success_criteria_clarity score
  - [x] Combine into overall score
- [x] Task 2: Implement threshold gate (AC: 2, 3)
  - [x] Check score against 0.2 threshold
  - [x] Generate clarification questions if above threshold
- [x] Task 3: Add explainability (AC: 4, 6)
  - [x] Show component scores
  - [x] Explain what needs clarification
  - [x] Display after each round
- [x] Task 4: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (100% achieved)
  - [x] Test error cases

## Dev Notes

- Threshold 0.2 per PRD ambiguity scale
- LLM used for scoring
- Make scoring reproducible

### Dependencies

**Requires:**
- Story 1-1: Interview Protocol Engine (interview data for scoring)

**Required By:**
- Story 1-3: Immutable Seed Generation

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Ambiguity-Measurement-Algorithm]

## Dev Agent Record

### Agent Model Used
- Claude Opus 4.5

### Debug Log References
N/A

### Completion Notes List
- Implemented AmbiguityScorer class using LiteLLMAdapter for LLM-based scoring
- Created AmbiguityScore dataclass with overall_score and ScoreBreakdown
- ScoreBreakdown contains ComponentScore for goal, constraint, and success criteria clarity
- Each ComponentScore includes name, clarity_score, weight, and justification text
- Scoring uses temperature=0.1 for reproducibility
- is_ready_for_seed() helper function checks score <= 0.2 threshold
- generate_clarification_questions() suggests questions for low-scoring components
- format_score_display() formats score for display after interview rounds
- Achieved 100% test coverage with 45 comprehensive unit tests
- All tests pass, ruff and mypy checks pass

### File List
- src/ouroboros/bigbang/ambiguity.py (new)
- src/ouroboros/bigbang/__init__.py (updated exports)
- tests/unit/bigbang/test_ambiguity.py (new)
