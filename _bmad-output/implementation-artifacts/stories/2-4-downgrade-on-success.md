# Story 2.4: Downgrade on Success

Status: completed

## Story

As a developer,
I want automatic tier downgrade after sustained success,
so that we continuously optimize for cost efficiency.

## Acceptance Criteria

1. Track consecutive successes per task pattern
2. Downgrade to lower tier after 5 consecutive successes
3. Downgrade decisions logged
4. Frugal tier tasks remain at Frugal
5. Similar task patterns (identified by ≥80% embedding similarity) inherit tier preference from successful completions

## Tasks / Subtasks

- [x] Task 1: Track success state (AC: 1)
  - [x] Track consecutive_successes per pattern
  - [x] Reset on failure
- [x] Task 2: Implement downgrade logic (AC: 2, 4)
  - [x] Check success count >= 5
  - [x] Downgrade to next lower tier
  - [x] Don't downgrade below Frugal
- [x] Task 3: Add pattern learning (AC: 5)
  - [x] Identify similar task patterns (using Jaccard similarity for MVP)
  - [x] Apply learned tier preference
- [x] Task 4: Add logging (AC: 3)
  - [x] Log downgrade decisions
  - [x] Track cost savings
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (99% achieved)
  - [x] Test error cases

## Dev Notes

- 5 successes = downgrade (from PRD)
- Goal: 80%+ on Frugal
- Continuous optimization
- For MVP, Jaccard similarity is used instead of embeddings for pattern matching

### Dependencies

**Requires:**
- Story 2-2 (router)

**Required By:**
- None (end of chain)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Downgrade-Rules]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References
N/A

### Completion Notes List
- Created `routing/downgrade.py` with SuccessTracker, PatternMatcher, and DowngradeManager classes
- SuccessTracker tracks consecutive successes per task pattern with reset_on_failure support
- DowngradeManager implements 5-consecutive-success downgrade threshold
- PatternMatcher uses Jaccard similarity (tokenized word overlap) for pattern matching
- Similarity threshold set to 80% as per requirements
- Comprehensive logging added for downgrade decisions and cost savings tracking
- Test coverage: 99% (68 tests)

### File List
- `src/ouroboros/routing/downgrade.py` - Main implementation
- `src/ouroboros/routing/__init__.py` - Updated exports
- `tests/unit/routing/test_downgrade.py` - Comprehensive unit tests (68 tests, 99% coverage)
