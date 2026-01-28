# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-01-28

### Added

#### Documentation
- **CLI Reference** (`docs/cli-reference.md`) - Complete command reference with examples
- **Prerequisites section** in README with Python 3.14+ requirement
- **Contributing section** with links to Issues and Discussions
- **OSS badges** - PyPI version, Python version, License

#### Interview System
- **Tiered confirmation system** for interview rounds:
  - Rounds 1-3: Auto-continue (minimum context gathering)
  - Rounds 4-15: Ask "Continue?" after each round
  - Rounds 16+: Ask "Continue?" with diminishing returns warning
- **No hard round limit** - User controls when to stop
- New constants: `MIN_ROUNDS_BEFORE_EARLY_EXIT`, `SOFT_LIMIT_WARNING_THRESHOLD`

### Changed

#### Interview Engine
- Removed `MAX_INTERVIEW_ROUNDS` hard limit (was 10)
- `is_complete` now only checks status (user-controlled completion)
- `record_response()` no longer auto-completes at max rounds
- System prompt simplified to show "Round N" instead of "Round N of 10"

#### CLI Init Command
- Extracted `_run_interview_loop()` helper to eliminate code duplication (~60 lines)
- State saved immediately after status mutation for consistency
- Updated welcome message to reflect no round limit

### Removed
- Korean-language requirement documents (`requirement/` folder)
- Hard round limit enforcement in interview engine

### Fixed
- Code duplication in init.py interview continuation flow

## [0.2.0] - 2026-01-27

### Added

#### Security Module (`ouroboros.core.security`)
- New security utilities module with comprehensive protection features
- **API Key Management**
  - `mask_api_key()` - Safely mask API keys for logging (shows only last 4 chars)
  - `validate_api_key_format()` - Basic format validation for API keys
- **Sensitive Data Detection**
  - `is_sensitive_field()` - Detect sensitive field names (api_key, password, token, etc.)
  - `is_sensitive_value()` - Detect values that look like secrets
  - `mask_sensitive_value()` - Mask potentially sensitive values
  - `sanitize_for_logging()` - Create sanitized copies of dicts for safe logging
- **Input Validation**
  - `InputValidator` class with size limits for DoS prevention:
    - `MAX_INITIAL_CONTEXT_LENGTH` = 50KB
    - `MAX_USER_RESPONSE_LENGTH` = 10KB
    - `MAX_SEED_FILE_SIZE` = 1MB
    - `MAX_LLM_RESPONSE_LENGTH` = 100KB

#### Logging Security
- Automatic sensitive data masking in structlog processor chain
- API keys, passwords, tokens are now automatically redacted in all log outputs
- Nested dictionaries are recursively sanitized
- Pattern-based detection for values starting with `sk-`, `pk-`, `Bearer`, etc.

### Changed

#### Interview Engine
- Input validation now uses `InputValidator` for consistent size limits
- `start_interview()` validates initial context length
- `record_response()` validates user response length

#### LiteLLM Adapter
- LLM responses are now validated and truncated if exceeding size limits
- Warning logged when response truncation occurs

#### CLI Run Command
- Seed file size is now validated before loading
- Protection against oversized seed files

### Security

- **API Key Management**: Keys are masked in logs, showing only provider prefix and last 4 characters
- **Input Validation**: All external inputs have size limits to prevent DoS attacks
- **Log Sanitization**: Sensitive data is automatically masked in all log outputs
- **Credentials Protection**: `credentials.yaml` continues to use chmod 600 permissions

### Tests

- Added comprehensive test suite for security module (39 tests)
- Added sensitive data masking tests for logging module (5 tests)
- All 1341 tests passing

## [0.1.1] - 2026-01-15

### Added
- Initial release with core Ouroboros workflow system
- Big Bang (Phase 0) - Interview and Seed generation
- PAL Router (Phase 1) - Progressive Adaptive LLM selection
- Double Diamond (Phase 2) - Execution engine
- Resilience (Phase 3) - Stagnation detection and lateral thinking
- Evaluation (Phase 4) - Mechanical, semantic, and consensus evaluation
- Secondary Loop (Phase 5) - TODO registry and batch scheduler
- Orchestrator (Epic 8) - Claude Agent SDK integration
- CLI interface with Typer
- Event sourcing with SQLite persistence
- Structured logging with structlog

### Fixed
- Various bug fixes and stability improvements

## [0.1.0] - 2026-01-01

### Added
- Initial project structure
- Core types and error hierarchy
- Basic configuration system
