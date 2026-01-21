# Story 2.2: Complexity-Based Routing

Status: completed

## Story

As a developer,
I want automatic tier selection based on task complexity,
so that simple tasks don't waste expensive model calls.

## Acceptance Criteria

1. Complexity estimated for each task (0.0 - 1.0)
2. Complexity < 0.4 routes to Frugal
3. Complexity 0.4-0.7 routes to Standard
4. Complexity > 0.7 routes to Frontier
5. Complexity calculated from: token count (30% weight), tool dependency count (30%), AC nesting depth (40%)

## Tasks / Subtasks

- [x] Task 1: Implement complexity estimation (AC: 1, 5)
  - [x] Create routing/complexity.py
  - [x] Estimate based on task size, depth, dependencies
  - [x] Return normalized score 0.0-1.0
- [x] Task 2: Implement PAL Router (AC: 2, 3, 4)
  - [x] Create routing/router.py
  - [x] Implement PALRouter.route() method
  - [x] Apply threshold-based routing logic
- [x] Task 3: Make router stateless
  - [x] Pass state in, not stored
  - [x] Pure function routing
- [x] Task 4: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (achieved 100%)
  - [x] Test error cases

## Dev Notes

- PALRouter is stateless per Architecture
- 80%+ should route to Frugal (NFR1)
- Complexity is a hypothesis - may need tuning

### Dependencies

**Requires:**
- Story 2-1 (tier config)

**Required By:**
- Story 2-3 (escalation on failure)
- Story 2-4 (downgrade on success)
- Story 3-1 (task decomposition)

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Decision-Framework]
- [Source: _bmad-output/planning-artifacts/architecture.md#PAL-Router-Stateless-Design]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References
N/A

### Completion Notes List
- Implemented TaskContext dataclass with token_count, tool_dependencies, and ac_depth fields
- Implemented ComplexityScore dataclass with score (0.0-1.0) and breakdown dictionary
- Implemented estimate_complexity() function with weighted scoring:
  - Token count: 30% weight (normalized to MAX_TOKEN_THRESHOLD=4000)
  - Tool dependencies: 30% weight (normalized to MAX_TOOL_THRESHOLD=5)
  - AC depth: 40% weight (normalized to MAX_DEPTH_THRESHOLD=5)
- Implemented PALRouter class with route() method
- Implemented RoutingDecision dataclass for rich routing results
- Router is completely stateless - all decisions based on input TaskContext
- Routing thresholds: < 0.4 -> Frugal, 0.4-0.7 -> Standard, > 0.7 -> Frontier
- 58 unit tests for complexity and router modules (100% coverage)
- Uses Result type for error handling
- Integrated with observability/logging.py

### File List
- src/ouroboros/routing/complexity.py (new)
- src/ouroboros/routing/router.py (new)
- src/ouroboros/routing/__init__.py (updated exports)
- tests/unit/routing/test_complexity.py (new)
- tests/unit/routing/test_router.py (new)
