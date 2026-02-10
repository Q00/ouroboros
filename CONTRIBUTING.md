# Contributing to Ouroboros

Thank you for your interest in contributing to Ouroboros! This guide covers everything you need to get started.

## Table of Contents

- [Quick Setup](#quick-setup)
- [Development Workflow](#development-workflow)
- [Ways to Contribute](#ways-to-contribute)
- [Development Environment](#development-environment)
- [Code Style Guide](#code-style-guide)
- [Commit Message Convention](#commit-message-convention)
- [Project Structure](#project-structure)
- [Key Patterns](#key-patterns)
- [Contributor Docs](#contributor-docs)
- [Code of Conduct](#code-of-conduct)

---

## Quick Setup

```bash
# Clone and install
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync

# Verify
uv run ouroboros --version
uv run pytest tests/unit/ -q
```

**Requirements**: Python 3.14+, [uv](https://github.com/astral-sh/uv)

---

## Development Workflow

### 1. Find or Create an Issue

- Check [GitHub Issues](https://github.com/Q00/ouroboros/issues) for open tasks
- For new features, open an issue first to discuss the approach
- Label your issue with appropriate tags: `bug`, `enhancement`, `documentation`, etc.

### 2. Branch

```bash
git checkout -b feat/your-feature   # for new features
git checkout -b fix/your-bugfix     # for bug fixes
git checkout -b docs/your-changes   # for documentation
```

### 3. Code

- Follow the project structure (see [Architecture for Contributors](./docs/contributing/architecture-overview.md))
- Use frozen dataclasses or Pydantic models for data
- Use the `Result[T, E]` type instead of exceptions for expected failures
- Write tests alongside your code

### 4. Test

```bash
# Full unit test suite
uv run pytest tests/unit/ -v

# Specific module
uv run pytest tests/unit/evaluation/ -v

# With coverage
uv run pytest tests/unit/ --cov=src/ouroboros --cov-report=term-missing
```

See [Testing Guide](./docs/contributing/testing-guide.md) for more details.

### 5. Lint and Format

```bash
# Check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Auto-fix
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Type check
uv run mypy src/ouroboros
```

### 6. Submit PR

- Write a clear PR description explaining **what** and **why**
- Reference the related issue (e.g., `Closes #123`)
- Ensure all tests pass and linting is clean
- Wait for code review and address feedback

---

## Ways to Contribute

### Bug Reports

Found a bug? Please open an issue with:

1. **Clear title**: Summarize the bug
2. **Description**: Steps to reproduce, expected vs actual behavior
3. **Environment**: Python version, OS, `uv run ouroboros --version`
4. **Logs**: Relevant error messages or stack traces

```markdown
## Bug Description
[What went wrong]

## Steps to Reproduce
1. Run `ooo interview "test"`
2. Enter X when prompted
3. Observe error

## Expected Behavior
[What should happen]

## Environment
- Python: 3.14.0
- Ouroboros: v0.9.0
- OS: macOS 15.2

## Logs
```
[paste error output]
```
```

### Feature Proposals

Have an idea? Open an issue with:

1. **Problem statement**: What problem does this solve?
2. **Proposed solution**: How should it work?
3. **Alternatives considered**: What other approaches did you think about?
4. **Scope**: Is this a breaking change? Can it be incremental?

### Pull Requests

When submitting a PR:

1. **Small, focused changes**: One logical change per PR
2. **Tests included**: New features need tests
3. **Docs updated**: Update relevant documentation
4. **Clean history**: Squash commits before submitting if needed

### Documentation

Help improve docs by:

- Fixing typos and unclear explanations
- Adding examples to existing features
- Translating documentation (if you speak multiple languages)
- Creating tutorials or guides

### Code Review

Review open PRs to:

- Catch bugs before merge
- Suggest improvements
- Learn the codebase

---

## Development Environment

### Environment Setup

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your API keys
# Required: ANTHROPIC_API_KEY or OPENAI_API_KEY
```

### Running Tests

```bash
# Unit tests (fast, no network)
uv run pytest tests/unit/ -v

# Integration tests (requires MCP server)
uv run pytest tests/integration/ -v

# E2E tests (full system)
uv run pytest tests/e2e/ -v

# Skip slow tests for fast iteration
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e
```

### Testing Specific Features

```bash
# TUI tests
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e -k "tui or tree"

# Evaluation pipeline
uv run pytest tests/unit/evaluation/ -v

# Orchestrator
uv run pytest tests/unit/orchestrator/ -v
```

### Pre-commit Hooks (Optional)

```bash
# Install pre-commit hooks
uv run pre-commit install

# Hooks run automatically on git commit
# Manual run:
uv run pre-commit run --all-files
```

---

## Code Style Guide

### Formatting

- **Line length**: 100 characters
- **Quotes**: Double quotes for strings
- **Indentation**: 4 spaces (no tabs)
- **Tool**: Ruff (auto-formats on save)

```bash
# Format code
uv run ruff format src/ tests/
```

### Type Checking

- **Strict mode**: Enabled (`strict = true`)
- **No untyped defs**: All functions must have type hints
- **Tool**: mypy

```bash
# Type check
uv run mypy src/ouroboros
```

### Linting

Ruff enforces:
- Pycodestyle (E, W)
- Pyflakes (F)
- isort (I)
- flake8-bugbear (B)
- flake8-comprehensions (C4)
- pyupgrade (UP)
- flake8-unused-arguments (ARG)
- flake8-simplify (SIM)

```bash
# Lint
uv run ruff check src/ tests/
```

### Python Version

- **Minimum**: Python 3.14
- **Target**: Python 3.14+
- Use modern Python features (type unions `|`, match statements, etc.)

---

## Commit Message Convention

We follow a simplified semantic commit format:

```
<type>(<scope>): <subject>

[optional body]
```

### Types

| Type | When to Use |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `chore` | Build, tooling, dependency updates |
| `refactor` | Code refactoring (no behavior change) |
| `test` | Test changes |
| `perf` | Performance improvements |

### Scopes

Common scopes: `cli`, `tui`, `evaluation`, `orchestrator`, `mcp`, `plugin`, `core`

### Examples

```bash
# Feature
git commit -m "feat(evaluation): add consensus trigger for seed drift > 0.3"

# Bug fix
git commit -m "fix(tui): resolve crash when AC tree is empty"

# Docs
git commit -m "docs: update CLI reference with new flags"

# Refactor
git commit -m "refactor(orchestrator): extract parallel execution to separate module"
```

### Body (Optional)

For complex changes, add a body explaining the **why**:

```bash
git commit -m "feat(evaluation): add stage 3 consensus trigger

This enables multi-model voting when:
- Seed is modified during execution
- Ontology evolves significantly
- Drift score exceeds 0.3

Closes #42"
```

---

## Project Structure

```
src/ouroboros/
  core/          # Foundation: Result type, Seed, errors, context
  bigbang/       # Phase 0: Interview and seed generation
  routing/       # Phase 1: PAL Router (model tier selection)
  execution/     # Phase 2: Double Diamond execution
  resilience/    # Phase 3: Stagnation detection, lateral thinking
  evaluation/    # Phase 4: Three-stage evaluation pipeline
  secondary/     # Phase 5: TODO registry
  orchestrator/  # Claude Agent SDK integration
  providers/     # LLM provider adapters (LiteLLM)
  persistence/   # Event sourcing, checkpoints
  tui/           # Terminal UI (Textual)
  cli/           # CLI commands (Typer)
  mcp/           # Model Context Protocol server/client
  config/        # Configuration management

tests/
  unit/          # Fast, isolated tests (no network, no DB)
  integration/   # Tests with real dependencies
  e2e/           # End-to-end CLI tests
  fixtures/      # Shared test data

.claude-plugin/  # Claude Code plugin definitions
  agents/        # Custom agent prompts
  skills/        # Plugin skill definitions
  hooks/         # Plugin hooks
```

---

## Key Patterns

Detailed explanations: [Key Patterns](./docs/contributing/key-patterns.md)

### Result Type for Error Handling

```python
from ouroboros.core.types import Result

def validate_score(score: float) -> Result[float, ValidationError]:
    if 0.0 <= score <= 1.0:
        return Result.ok(score)
    return Result.err(ValidationError(f"Score {score} out of range"))

# Consume
result = validate_score(0.85)
if result.is_ok:
    process(result.value)
else:
    log_error(result.error.message)
```

### Frozen Dataclasses

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class CheckResult:
    check_type: CheckType
    passed: bool
    message: str
```

### Event Sourcing

```python
# Events are immutable and append-only
event = create_stage1_completed_event(execution_id="exec_123", ...)
await event_store.append(event)
```

### Protocol Classes

```python
from typing import Protocol

@runtime_checkable
class ExecutionStrategy(Protocol):
    def get_tools(self) -> list[str]: ...
```

---

## Contributor Docs

- [Architecture Overview](./docs/contributing/architecture-overview.md) - How the system fits together
- [Testing Guide](./docs/contributing/testing-guide.md) - How to write and run tests
- [Key Patterns](./docs/contributing/key-patterns.md) - Core patterns with code examples

---

## Getting Help

- **GitHub Issues**: [Report bugs or request features](https://github.com/Q00/ouroboros/issues)
- **GitHub Discussions**: [Ask questions or share ideas](https://github.com/Q00/ouroboros/discussions)

---

## Code of Conduct

### Our Pledge

We pledge to make participation in our community a harassment-free experience for everyone, regardless of age, body size, disability, ethnicity, gender identity and expression, level of experience, nationality, personal appearance, race, religion, or sexual identity and orientation.

### Our Standards

**Positive behavior includes**:
- Being respectful and inclusive
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

**Unacceptable behavior includes**:
- Harassment, trolling, or derogatory comments
- Personal or political attacks
- Public or private harassment
- Publishing private information without permission
- Any other conduct which could reasonably be considered inappropriate

### Enforcement

Project maintainers may remove, edit, or reject comments, commits, code, wiki edits, issues, and other contributions that are not aligned with this Code of Conduct.

**Contact**: For any questions or concerns, please open a GitHub issue with the `conduct` label.

---

## License

By contributing to Ouroboros, you agree that your contributions will be licensed under the [MIT License](LICENSE).
