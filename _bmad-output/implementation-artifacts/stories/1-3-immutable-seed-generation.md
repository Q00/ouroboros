# Story 1.3: Immutable Seed Generation

Status: completed

## Story

As a developer,
I want a validated Seed YAML file,
so that I have a clear, immutable specification for execution.

## Acceptance Criteria

1. Seed generated only when Ambiguity ≤ 0.2
2. Seed contains: goal, constraints, acceptanceCriteria, ontologySchema, evaluationPrinciples, exitConditions
3. Seed validated against JSON schema
4. Seed file saved to specified location
5. Seed is immutable after generation (frozen)
6. Seed includes metadata: version, created_at, ambiguity_score

## Tasks / Subtasks

- [x] Task 1: Define Seed schema (AC: 2, 3)
  - [x] Create core/seed.py
  - [x] Define Seed Pydantic model (frozen=True)
  - [x] Include all required fields
  - [x] Use frozen=True on Seed Pydantic model
  - [x] Write test verifying Seed modification raises FrozenInstanceError
- [x] Task 2: Implement generation logic (AC: 1)
  - [x] Create bigbang/seed_generator.py
  - [x] Gate on ambiguity score
  - [x] Transform interview results to Seed
- [x] Task 3: Save and validate (AC: 3, 4, 6)
  - [x] Validate against schema
  - [x] Save as YAML file
  - [x] Include metadata
- [x] Task 4: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (achieved 93%)
  - [x] Test error cases

## Dev Notes

- Seed.direction (Goal, Success Metrics, Hard Constraints) = IMMUTABLE
- EffectiveOntology can evolve with Consensus
- Seed is the "constitution" of the workflow

### Dependencies

**Requires:**
- Story 1-2: Ambiguity Score Calculation (ambiguity score must be ≤ 0.2)

**Required By:**
- Story 3-1: Iteration Engine (uses Seed for iteration)
- Story 6-1: Dashboard Foundation (displays Seed information)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Immutable-Seed-JSON]
- [Source: _bmad-output/planning-artifacts/architecture.md#Immutability-Pattern]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References
N/A

### Completion Notes List
- Implemented immutable Seed Pydantic model with frozen=True
- All nested models (SeedMetadata, OntologySchema, OntologyField, EvaluationPrinciple, ExitCondition) also frozen
- Using tuples for collections to ensure complete immutability
- SeedGenerator gates on ambiguity score (must be <= 0.2)
- SeedGenerator uses LLM to extract structured requirements from interview
- save_seed() and load_seed() functions for YAML persistence
- Comprehensive tests: 39 for Seed model, 24 for SeedGenerator
- Test coverage: 100% for core/seed.py, 91% for bigbang/seed_generator.py (93% overall)

### File List
- src/ouroboros/core/seed.py (new)
- src/ouroboros/bigbang/seed_generator.py (new)
- src/ouroboros/core/__init__.py (updated exports)
- src/ouroboros/bigbang/__init__.py (updated exports)
- tests/unit/core/test_seed.py (new)
- tests/unit/bigbang/test_seed_generator.py (new)
