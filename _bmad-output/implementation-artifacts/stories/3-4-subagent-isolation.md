# Story 3.4: SubAgent Isolation

Status: review

## Story

As a developer,
I want isolated execution contexts for subtasks,
so that SubAgent failures don't pollute the main context.

## Acceptance Criteria

1. SubAgents receive filtered context ✅
2. Filtered context: seed_summary, current_ac, recent_history, key_facts ✅
3. Main context not modified by SubAgent ✅
4. SubAgent results validated before integration ✅
5. Failed SubAgent doesn't crash main execution ✅

## Tasks / Subtasks

- [x] Task 1: Define FilteredContext (AC: 1, 2)
  - [x] Add to core/context.py
  - [x] Include seed_summary (via parent_summary field)
  - [x] Include current_ac
  - [x] Include recent_history (last 3)
  - [x] Include key_facts (via relevant_facts field)
- [x] Task 2: Implement isolation (AC: 3)
  - [x] Create SubAgent executor (subagent.py module)
  - [x] Run in isolated scope (immutable frozen dataclass)
- [x] Task 3: Validate results (AC: 4, 5)
  - [x] Validate before merge using Result type
  - [x] Handle failures gracefully:
    - Return Result.err() on validation failure
    - Log failure with SubAgent context
    - Do not propagate exceptions to parent
  - [x] Emit lifecycle events (started, completed, failed, validated)
  - [x] Continue with other children on failure (resilience)
- [x] Task 4: Write tests
  - [x] Create unit tests in tests/unit/execution/test_subagent_isolation.py
  - [x] Achieve 100% test coverage for new code
  - [x] Test error cases (unsuccessful result, missing phases, validation failure)

## Dev Notes

- Isolation prevents context pollution
- Key pattern for reliability
- Part of Phase 2 execution

### Dependencies

**Requires:**
- Story 3-1 (Double Diamond Cycle Implementation)
- Story 0-9 (Context Compression)

**Required By:**
- None (leaf story in Epic 3)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#SubAgent-Context-Summary-Key-Facts]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5

### Debug Log References
- 921 tests passing (no regressions)
- Linting passed (ruff check)

### Completion Notes List
- Added `recent_history` field to FilteredContext dataclass
- Updated `create_filtered_context()` to include recent history (last 3 items)
- Created new `subagent.py` module with:
  - `validate_child_result()` - structural validation of child results
  - SubAgent lifecycle events (started, completed, failed, validated)
  - `SubAgentError` and `ValidationError` error types
- Modified `run_cycle_with_decomposition()` in double_diamond.py to:
  - Emit SubAgent lifecycle events
  - Validate child results before integration
  - Handle failures gracefully (continue with other children)
- Added 13 new tests for SubAgent isolation

### File List
- src/ouroboros/core/context.py (modified - added recent_history to FilteredContext)
- src/ouroboros/execution/subagent.py (new - SubAgent validation and events)
- src/ouroboros/execution/double_diamond.py (modified - integrated SubAgent isolation)
- src/ouroboros/execution/__init__.py (modified - export SubAgent utilities)
- tests/unit/core/test_context.py (modified - added FilteredContext tests)
- tests/unit/execution/test_subagent_isolation.py (new - SubAgent isolation tests)

## Change Log
- 2026-01-20: Story 3.4 SubAgent Isolation implemented. All 5 ACs satisfied.
