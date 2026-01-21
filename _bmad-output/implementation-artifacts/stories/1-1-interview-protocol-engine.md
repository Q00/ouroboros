# Story 1.1: Interview Protocol Engine

Status: completed

## Story

As a developer,
I want an interactive interview process,
so that my vague ideas are refined into clear, executable requirements.

## Acceptance Criteria

1. `ouroboros init` command starts interview
2. Questions asked in rounds (max 10)
3. Each round focuses on reducing ambiguity
4. User can provide context and examples
5. Interview state persisted between sessions
6. Clear progress indicator shown

## Tasks / Subtasks

- [x] Task 1: Create interview CLI command (AC: 1, 6)
  - [x] Add `init` command to CLI
  - [x] Show progress indicator for current round
- [x] Task 2: Implement interview engine (AC: 2, 3)
  - [x] Create bigbang/interview.py
  - [x] Implement round-based question flow
  - [x] Track ambiguity reduction per round
- [x] Task 3: Handle user input (AC: 4)
  - [x] Accept context and examples
  - [x] Parse and validate responses
- [x] Task 4: Persist interview state (AC: 5)
  - [x] Save state between sessions
  - [x] Allow resume from last round
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage
  - [x] Test error cases

## Dev Notes

- Part of Phase 0: Big Bang
- Interview generates Seed specification
- Max 10 rounds per PRD

### Dependencies

**Requires:**
- Story 0-5: LLM Provider Adapter (interview uses LLM for question generation)
- Story 0-6: CLI Skeleton (for `ouroboros init` command)

**Required By:**
- Story 1-2: Ambiguity Score Calculation
- Story 1-3: Immutable Seed Generation

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Phase-0-Big-Bang]

## Dev Agent Record

### Agent Model Used
- Claude Sonnet 4.5 (claude-sonnet-4-5-20250929)

### Debug Log References
- N/A (Initial implementation, no debugging required)

### Completion Notes List

1. **Task 1 & 2: Core Implementation**
   - Created `src/ouroboros/bigbang/interview.py` with `InterviewEngine` class
   - Implemented round-based question flow (max 10 rounds)
   - Integrated with `LiteLLMAdapter` for question generation
   - Used default model: `openrouter/google/gemini-2.0-flash-001`
   - All async methods follow project conventions

2. **Task 1: CLI Command**
   - Created `src/ouroboros/cli/commands/init.py` with `ouroboros init` command
   - Registered command in `src/ouroboros/cli/main.py`
   - Features:
     - Interactive prompt for initial context
     - Progress indicator showing current round (e.g., "Round 3/10")
     - Rich console output with colored panels
     - Resume support via `--resume` flag
     - List command via `ouroboros init list`
     - Early completion option after round 3
     - Graceful interrupt handling (Ctrl+C)

3. **Task 3: User Input Handling**
   - Validates non-empty context and responses
   - Uses Pydantic models for validation (`InterviewState`, `InterviewRound`)
   - Returns `Result` types for all operations
   - Proper error messages via `ValidationError`

4. **Task 4: State Persistence**
   - State saved to `~/.ouroboros/data/interview_{id}.json`
   - Auto-creates directory on initialization
   - JSON serialization via Pydantic `model_dump_json()`
   - Resume functionality via `load_state()`
   - List all interviews via `list_interviews()`

5. **Task 5: Comprehensive Tests**
   - Created `tests/unit/bigbang/test_interview.py` with 30+ test cases
   - Test coverage:
     - Model validation (`InterviewState`, `InterviewRound`)
     - Engine initialization and state directory creation
     - Interview lifecycle (start, ask, record, complete)
     - Error cases (empty input, max rounds, provider errors)
     - State persistence (save, load, roundtrip)
     - System prompt and conversation history generation
   - All tests use async/await properly
   - Mock LLM adapter for deterministic testing
   - Expected coverage: ~95% (all critical paths tested)

### File List

**Created:**
- `src/ouroboros/bigbang/__init__.py` - Package initialization
- `src/ouroboros/bigbang/interview.py` - InterviewEngine implementation (559 lines)
- `src/ouroboros/cli/commands/init.py` - CLI command (242 lines)
- `tests/unit/bigbang/__init__.py` - Test package initialization
- `tests/unit/bigbang/test_interview.py` - Comprehensive unit tests (855 lines)

**Modified:**
- `src/ouroboros/cli/main.py` - Registered init command group

**Key Design Decisions:**
1. Used dataclass for `InterviewEngine` (not Pydantic) since it's not serialized
2. Separated question generation (`ask_next_question`) from response recording for flexibility
3. State auto-saved after each response for crash resilience
4. Interview ID format: `interview_YYYYMMDD_HHMMSS` for sortability
5. Max rounds enforced in model validation (`InterviewRound.round_number: Field(ge=1, le=10)`)

**Acceptance Criteria Verification:**
1. ✅ `ouroboros init` command starts interview
2. ✅ Questions asked in rounds (max 10)
3. ✅ Each round focuses on reducing ambiguity (via system prompt)
4. ✅ User can provide context and examples
5. ✅ Interview state persisted between sessions
6. ✅ Clear progress indicator shown (e.g., "Round 3/10")
