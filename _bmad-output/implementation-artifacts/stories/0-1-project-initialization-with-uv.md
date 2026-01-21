# Story 0.1: Project Initialization with uv

Status: done

## Story

As a developer,
I want to initialize the Ouroboros project with proper Python 3.14 packaging,
so that I have a clean, modern project structure ready for development.

## Acceptance Criteria

1. Project created with `uv init --package ouroboros --python 3.14`
2. src/ouroboros/ layout structure established
3. pyproject.toml configured with all dependencies from Architecture:
   - typer>=0.12.0
   - httpx>=0.27.0
   - pydantic>=2.0.0
   - structlog>=24.0.0
   - litellm>=1.80.0
   - sqlalchemy[asyncio]>=2.0.0
   - aiosqlite>=0.20.0
   - stamina>=25.1.0
   - rich>=13.0.0
   - pyyaml>=6.0.0
4. Dev dependencies configured: pytest, pytest-asyncio, pytest-cov, ruff, mypy, pre-commit
5. .python-version set to 3.14

## Tasks / Subtasks

- [x] Task 1: Initialize project (AC: 1)
  - [x] Run `uv init --package ouroboros --python 3.14`
  - [x] Verify src/ouroboros/ structure created
- [x] Task 2: Configure dependencies (AC: 3, 4)
  - [x] Add runtime dependencies to pyproject.toml
  - [x] Add dev dependencies using `uv add --dev`
- [x] Task 3: Configure tooling (AC: 5)
  - [x] Set up ruff.toml configuration
  - [x] Set up .pre-commit-config.yaml
  - [x] Configure mypy in pyproject.toml
- [x] Task 4: Create initial module structure
  - [x] Create src/ouroboros/__init__.py with version
  - [x] Create src/ouroboros/__main__.py entry point
  - [x] Create src/ouroboros/py.typed marker
- [x] Task 5: Verify setup (AC: 1, 2, 3, 4, 5)
  - [x] Run `uv sync` to install dependencies
  - [x] Run `uv run python -c "import ouroboros"` to verify import
  - [x] Run `uv run pytest --collect-only` to verify test setup
- [x] Task 6: Write tests
  - [x] Create unit tests in tests/unit/
  - [x] Achieve ≥80% coverage
  - [x] Test error cases

## Dev Notes

- Architecture specifies Python 3.14+ for free-threaded mode, template strings, deferred annotations
- Use hatchling as build backend per Architecture
- Line length 100 per ruff.toml configuration
- asyncio_mode = "auto" for pytest

### Project Structure Notes

Follow Architecture doc structure:
```
ouroboros/
├── .python-version (3.14)
├── pyproject.toml
├── uv.lock
├── src/ouroboros/
│   ├── __init__.py
│   ├── __main__.py
│   └── py.typed
└── tests/
    └── conftest.py
```

### Dependencies

**Requires:**
- None (first story)

**Required By:**
- Story 0-2: Core Types and Error Handling
- Story 0-3: Event Store with SQLAlchemy Core
- Story 0-4: Configuration and Credentials Management
- Story 0-5 through 0-9 (all Epic 0 stories)
- Epic 1-7 (indirect dependency - all subsequent epics)

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Starter-Template-Evaluation]
- [Source: _bmad-output/planning-artifacts/architecture.md#pyproject.toml-Configuration]

## Dev Agent Record

### Agent Model Used

glm-4.7 (claude-opus-4-5-20251101)

### Debug Log References

No issues encountered during implementation.

### Completion Notes List

✅ **Story 0-1 Implementation Complete**

**Implemented Features:**
1. Project initialized with `uv init --package ouroboros --python 3.14`
2. All runtime dependencies configured (typer, httpx, pydantic, structlog, litellm, sqlalchemy, aiosqlite, stamina, rich, pyyaml)
3. All dev dependencies configured (pytest, pytest-asyncio, pytest-cov, ruff, mypy, pre-commit)
4. Tooling configured:
   - ruff.toml with line-length=100
   - .pre-commit-config.yaml with hooks for ruff, mypy
   - mypy with strict mode enabled
   - pytest with asyncio_mode="auto"
5. Initial module structure created:
   - src/ouroboros/__init__.py with __version__ attribute
   - src/ouroboros/__main__.py entry point
   - src/ouroboros/py.typed marker for PEP 561
6. Verification complete:
   - uv sync successful
   - Package can be imported
   - pytest collects 21 tests
7. Tests written:
   - 21 tests total (20 unit tests, 1 integration test)
   - 100% coverage achieved

**Test Results:**
- All 21 tests pass
- 100% code coverage
- All linting checks pass (ruff)
- Entry point verified working

### Code Review Notes

**Adversarial Code Review Completed:** 2026-01-15
- Original issues identified: 10 (2 HIGH, 3 MEDIUM, 5 LOW)
- All issues fixed and verified via zen review (gemini-3-pro-preview)

**Key Fixes Applied:**
1. Build backend changed from uv_build to hatchling (architecture compliance)
2. .gitignore created with comprehensive Python patterns
3. ruff.toml removed, config consolidated in pyproject.toml
4. Pre-commit ruff version updated to v0.14.11
5. mypy --ignore-missing-imports removed from pre-commit for strict mode consistency
6. sys.path manipulation removed from conftest.py
7. Proper __all__ exports added
8. uv.lock removed from .gitignore (apps should commit lock files)

### File List

**New Files:**
- .python-version
- .pre-commit-config.yaml
- .gitignore
- pyproject.toml
- README.md
- src/ouroboros/__init__.py
- src/ouroboros/__main__.py
- src/ouroboros/py.typed
- tests/conftest.py
- tests/integration/test_entry_point.py
- tests/unit/test_dependencies_configured.py
- tests/unit/test_main_entry_point.py
- tests/unit/test_module_structure.py
- tests/unit/test_project_initialization.py
- tests/unit/test_tooling_configuration.py

**Modified Files:**
- None (this is the first implementation story)
