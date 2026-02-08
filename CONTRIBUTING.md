# Contributing to Ouroboros

Thank you for your interest in contributing to Ouroboros! This guide covers everything you need to get started.

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

## Development Workflow

### 1. Find or Create an Issue

- Check [GitHub Issues](https://github.com/Q00/ouroboros/issues) for open tasks
- For new features, open an issue first to discuss the approach

### 2. Branch

```bash
git checkout -b feat/your-feature   # or fix/your-bugfix
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
uv run mypy src/ouroboros --ignore-missing-imports
```

### 6. Submit PR

- Write a clear PR description explaining what and why
- Reference the related issue
- Ensure all tests pass and linting is clean

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
```

## Code Style

- **Formatter**: Ruff (`line-length = 100`, double quotes)
- **Linter**: Ruff (pycodestyle, pyflakes, isort, flake8-bugbear, pyupgrade)
- **Type checker**: mypy (`strict = true`)
- **Test framework**: pytest with `asyncio_mode = "auto"`

## Key Patterns

Detailed explanations: [Key Patterns](./docs/contributing/key-patterns.md)

- **Result type** for error handling (not exceptions)
- **Frozen dataclasses** and **Pydantic frozen models** for immutability
- **Event sourcing** for state persistence
- **Protocol classes** for pluggable strategies

## Contributor Docs

- [Architecture Overview](./docs/contributing/architecture-overview.md) - How the system fits together
- [Testing Guide](./docs/contributing/testing-guide.md) - How to write and run tests
- [Key Patterns](./docs/contributing/key-patterns.md) - Core patterns with code examples

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
