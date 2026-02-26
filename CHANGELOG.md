# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.14.1] - 2025-02-27

### Fixed
- **interview**: Fix empty response bypass in ClaudeCodeAdapter — empty content now always triggers error regardless of session_id
- **interview**: Fix sub-agent turn exhaustion — increase max_turns from 1 to 3 so the agent can use tools and still generate the question

### Maintenance
- **style**: Apply ruff format to 4 files
- **ci**: Resolve ruff and mypy CI failures

## [0.13.4] - 2025-02-24

### Fixed
- **mcp**: Initialize EventStore in ExecuteSeedHandler before passing to OrchestratorRunner

## [0.13.3] - 2025-02-24

### Fixed
- **mcp**: Remove double-registration in CLI that overwrote dependency-injected handlers with empty ones
- **mcp**: Return proper MCP error responses (isError:true) instead of error text in success
- **mcp**: Catch `pydantic.ValidationError` in ExecuteSeed, MeasureDrift, Evaluate handlers
- **mcp**: Initialize EventStore before EvolutionaryLoop.evolve_step accesses it
- **mcp**: Forward host/port CLI args to server for SSE transport
- **mcp**: Remove dead code (discarded EvaluationPipeline/LateralThinker instances)
- **mcp**: Remove invalid `llm_adapter` kwarg from ClaudeAgentAdapter init
- **orchestrator**: Handle DependencyAnalyzer error with all-parallel fallback instead of crash
- **seed**: Add Pydantic aliases (`type` for field_type, `criteria` for evaluation_criteria)
- **eval**: Change EvaluationPipeline/SeedGenerator type annotations from LiteLLMAdapter to LLMAdapter Protocol
- **security**: Validate nested string values in InputValidator, not just top-level
- **security**: Use MappingProxyType for frozen dataclass AuthContext.metadata
- **protocol**: Add credentials param to MCPServer protocol to match implementation

### Changed
- **build**: Use dynamic version from `__init__.py` via hatchling (single source of truth)

## [0.13.2] - 2025-02-24

### Fixed
- **adapter**: Handle unknown message types (`rate_limit_event`) from Claude Agent SDK with retry logic
- **interview**: Ensure first response is a direct question, not introduction
- **mcp**: Correct uvx command syntax to use `--python 3.14 --from ouroboros-ai` for proper version resolution

## [Unreleased]

### Added

#### Plugin System - Agent Orchestration Framework (Phase 1)

**Agent System (`ouroboros.plugin.agents`)**
- `AgentRegistry` - Dynamic agent discovery with custom `.md` file support from `.claude-plugin/agents/`
- `AgentPool` - Reusable agent pool with load balancing, auto-scaling, and health monitoring
- `AgentRole` enum - Type-safe role categorization (ANALYSIS, PLANNING, EXECUTION, REVIEW, DOMAIN, PRODUCT, COORDINATION)
- `AgentSpec` - Frozen dataclass for agent specifications with tools, capabilities, and model preferences
- 4 builtin agents: `executor`, `planner`, `verifier`, `analyst`

**Skill System (`ouroboros.plugin.skills`)**
- `SkillRegistry` - Hot-reloadable skill discovery from `.claude-plugin/skills/`
- `MagicKeywordDetector` - "ooo:" prefix and trigger keyword routing
- `SkillExecutor` - Context-aware skill execution with history tracking
- `SkillDocumentation` - Auto-generated documentation from SKILL.md files
- 9 new execution mode skills:
  - `autopilot` - Autonomous execution from idea to working code
  - `ultrawork` - Maximum parallelism with parallel agent orchestration
  - `ralph` - Self-referential loop with verifier verification (includes ultrawork)
  - `ultrapilot` - Parallel autopilot with file ownership partitioning
  - `ecomode` - Token-efficient execution using haiku and sonnet
  - `swarm` - N coordinated agents using Claude Code native teams
  - `pipeline` - Sequential agent chaining with data passing
  - `tutorial` - Interactive guided tour for new users
  - `swarm` - Team coordination mode

**Orchestration (`ouroboros.plugin.orchestration`)**
- `ModelRouter` - PAL (Progressive Auto-escalation) routing with tier selection
- `Scheduler` - Parallel task execution with dependency resolution via `TaskGraph`
- `RoutingContext` - Complexity-aware routing with learning from history
- `ScheduledTask` - Task wrapper with priority, dependencies, and timeout support

**State Management (`ouroboros.plugin.state`)**
- `StateStore` - Atomic JSON file storage with schema migrations
- `StateManager` - Session persistence with checkpoint/recovery
- `RecoveryManager` - Auto-resume hooks after interruptions
- `StateCompression` - Smart context compression when approaching limits
- Support for multiple modes: AUTOPILOT, RALPH, ULTRAWORK, ULTRAPILOT, ECOMODE, SWARM, PIPELINE

**TUI HUD Components (`ouroboros.tui.components`)**
- `AgentsPanel` - Real-time agent pool status visualization
- `TokenTracker` - Per-agent token usage with cost estimation
- `ProgressBar` - Multi-phase progress with animated spinners
- `EventLog` - Scrolling event history with color-coded severity
- `HUDDashboard` - Unified HUD screen integrating all components

**Documentation**
- `docs/compare-alternatives.md` - Comparison with other AI agents and frameworks
- `docs/onboarding-metrics.md` - User onboarding metrics and optimization strategies
- `docs/marketing/` - Marketing assets (social media templates, star campaign, why ouroboros)
- `docs/screenshots/` - Screenshot capture guides and production scripts
- `docs/videos/` - Video production guides and demo scripts
- Updated `CONTRIBUTING.md` - Full development setup and contribution guide
- Updated `docs/architecture.md` - Plugin system architecture documentation
- Updated `docs/getting-started.md` - Enhanced onboarding experience

**Developer Experience**
- GitHub workflows: `.github/workflows/lint.yml`, `test.yml`, `release.yml`
- `playground/` directory with example models and configurations
- 161 new passing tests (149 unit + 12 integration)

**Skill Files Updated**
- Updated `help`, `setup`, `welcome` skills with progressive disclosure
- Added 8 new skill SKILL.md files (autopilot, ultrawork, ralph, ultrapilot, ecomode, swarm, pipeline, tutorial)

### Changed
- Updated CLI onboarding flow to reference new plugin system
- Enhanced skill discovery with automatic trigger keyword indexing
- Improved state persistence across /clear and session restarts

### Tests
- 161 new tests for plugin system (149 unit + 12 integration)
- All existing TUI and tree tests continue to pass (190 tests)
- Total test count: 1731 passing tests

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
