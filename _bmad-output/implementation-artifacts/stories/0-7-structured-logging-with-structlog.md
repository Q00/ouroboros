# Story 0.7: Structured Logging with structlog

Status: review

## Story

As a developer,
I want structured JSON logging,
so that I can analyze logs programmatically and debug issues effectively.

## Acceptance Criteria

1. structlog configured with standard processors
2. Timestamps in ISO 8601 format
3. Log level included in all entries
4. contextvars integration for cross-async context
5. Dev mode: human-readable console output
6. Production mode: JSON output
7. Log files rotate daily with 7-day retention (configurable)

## Tasks / Subtasks

- [x] Task 1: Configure structlog (AC: 1, 2, 3)
  - [x] Create observability/logging.py
  - [x] Configure processors: add_log_level, TimeStamper, StackInfoRenderer
  - [x] Set up custom logger factory with file output
- [x] Task 2: Add contextvars support (AC: 4)
  - [x] Add merge_contextvars processor
  - [x] Create bind_context helper function
  - [x] Create unbind_context and clear_context helpers
- [x] Task 3: Implement output modes (AC: 5, 6)
  - [x] Use ConsoleRenderer for dev mode
  - [x] Use JSONRenderer for production mode
  - [x] Add config option for mode selection (OUROBOROS_LOG_MODE env var)
- [x] Task 4: Implement log rotation (AC: 7)
  - [x] Configure TimedRotatingFileHandler with daily rotation
  - [x] Set retention to 7 days (configurable via LoggingConfig)
  - [x] Add max_log_days and log_dir config options
- [x] Task 5: Write tests
  - [x] Create unit tests in tests/unit/observability/
  - [x] Achieve 96% coverage (exceeds ≥80% requirement)
  - [x] Test error cases

## Dev Notes

- Standard log keys: seed_id, ac_id, depth, iteration, tier
- Event names follow dot.notation (e.g., "ac.execution.started")
- Never log sensitive data (API keys, credentials)

### Dependencies

**Requires:**
- Story 0-1: Project structure and configuration

**Required By:**
- All other stories (used throughout for observability)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#structlog-Configuration]

## Dev Agent Record

### Agent Model Used
Claude Opus 4.5 (claude-opus-4-5-20251101)

### Debug Log References
N/A

### Completion Notes List
- Implemented structlog configuration with full processor chain
- Created custom logger factory (_FileWritingPrintLoggerFactory) for dual output (console + file)
- Added contextvars integration via structlog.contextvars.merge_contextvars
- Implemented LoggingConfig Pydantic model with validation
- Added mode selection via OUROBOROS_LOG_MODE environment variable
- TimedRotatingFileHandler configured for daily rotation with configurable retention
- 45 unit tests with 96% code coverage
- All linting checks pass

### File List
- src/ouroboros/observability/__init__.py
- src/ouroboros/observability/logging.py
- tests/unit/observability/__init__.py
- tests/unit/observability/test_logging.py
