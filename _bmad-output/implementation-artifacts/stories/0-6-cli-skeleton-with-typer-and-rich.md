# Story 0.6: CLI Skeleton with Typer and Rich

Status: review

## Story

As a developer,
I want a beautiful CLI interface with progress indicators,
so that I have clear feedback during long-running operations.

## Acceptance Criteria

1. Typer app created with main command groups
2. `ouroboros --help` shows formatted help text
3. Rich progress bars available for async operations
4. Rich tables for structured data display
5. Rich panels for important messages
6. Console instance shared across CLI modules

## Tasks / Subtasks

- [x] Task 1: Create Typer app skeleton (AC: 1, 2)
  - [x] Create cli/main.py with Typer app
  - [x] Register command groups: run, config, status
  - [x] Add --version option
- [x] Task 2: Set up Rich progress (AC: 3)
  - [x] Create cli/formatters/progress.py
  - [x] Implement SpinnerColumn for async operations
  - [x] Create async-aware progress context manager
- [x] Task 3: Set up Rich tables (AC: 4)
  - [x] Create cli/formatters/tables.py
  - [x] Implement table formatters for structured data
  - [x] Add column alignment and styling helpers
- [x] Task 4: Set up Rich panels (AC: 5)
  - [x] Create cli/formatters/panels.py
  - [x] Implement info/warning/error panel templates
- [x] Task 5: Create shared Console (AC: 6)
  - [x] Create cli/formatters/__init__.py
  - [x] Instantiate shared Console with consistent theme
  - [x] Export console for use across CLI modules
- [x] Task 6: Create entry point (AC: 2)
  - [x] Configure [project.scripts] in pyproject.toml
  - [x] Verify `ouroboros` command works
- [x] Task 7: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage (85% achieved)
  - [x] Test error cases

## Dev Notes

- Typer for CLI framework
- Rich for beautiful output
- Semantic colors: green=success, yellow=warning, red=error, blue=info
- SpinnerColumn for async operations

### Dependencies

**Requires:**
- Story 0-1: Project structure and configuration

**Required By:**
- Story 1-1: Seed Ingestion and Parsing
- Story 4-3: CLI Integration

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#CLI-Output-Rich]

## Dev Agent Record

### Agent Model Used
claude-opus-4-5-20251101

### Debug Log References
N/A

### Completion Notes List
- Implemented CLI skeleton with Typer app and Rich formatters
- Created shared Console with semantic color theme (success=green, warning=yellow, error=red, info=blue)
- Implemented progress spinners with both sync and async context managers
- Implemented table formatters with key-value and status table variants
- Implemented panel templates for info/warning/error/success messages
- Created placeholder command groups: run, config, status
- Configured entry point in pyproject.toml
- All 350 tests pass (including 82 CLI-specific tests)
- Test coverage for CLI module: 85%

### File List
- src/ouroboros/cli/__init__.py
- src/ouroboros/cli/main.py
- src/ouroboros/cli/commands/__init__.py
- src/ouroboros/cli/commands/run.py
- src/ouroboros/cli/commands/config.py
- src/ouroboros/cli/commands/status.py
- src/ouroboros/cli/formatters/__init__.py
- src/ouroboros/cli/formatters/progress.py
- src/ouroboros/cli/formatters/tables.py
- src/ouroboros/cli/formatters/panels.py
- tests/unit/cli/__init__.py
- tests/unit/cli/test_main.py
- tests/unit/cli/formatters/__init__.py
- tests/unit/cli/formatters/test_console.py
- tests/unit/cli/formatters/test_progress.py
- tests/unit/cli/formatters/test_tables.py
- tests/unit/cli/formatters/test_panels.py
