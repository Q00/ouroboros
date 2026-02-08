# Testing Guide

How to write and run tests for Ouroboros.

## Test Structure

```
tests/
  conftest.py              # Shared fixtures (event_store, mock adapters)
  fixtures/                # Test data files
  unit/                    # Fast, isolated tests
    core/                  # tests for src/ouroboros/core/
    evaluation/            # tests for src/ouroboros/evaluation/
    orchestrator/          # tests for src/ouroboros/orchestrator/
    tui/                   # tests for src/ouroboros/tui/
    ...                    # mirrors src/ structure
  integration/             # Tests with real dependencies
    mcp/                   # MCP integration tests
  e2e/                     # End-to-end CLI tests
    test_cli_commands.py
    test_full_workflow.py
    test_session_persistence.py
```

## Running Tests

### All Unit Tests

```bash
uv run pytest tests/unit/ -v
```

### Specific Module

```bash
# Evaluation pipeline
uv run pytest tests/unit/evaluation/ -v

# Orchestrator (including parallel execution, strategies)
uv run pytest tests/unit/orchestrator/ -v

# TUI
uv run pytest tests/unit/tui/ -v
```

### With Coverage

```bash
uv run pytest tests/unit/ --cov=src/ouroboros --cov-report=term-missing
```

### Skip Slow Tests

MCP tests require network and external servers. Skip them for fast iteration:

```bash
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e
```

### TUI-Specific Tests

```bash
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e -k "tui or tree"
```

### E2E Tests

End-to-end tests exercise the full CLI:

```bash
uv run pytest tests/e2e/ -v
```

Note: `test_run_workflow_verbose` is a known pre-existing failure; do not block on it.

## Writing Tests

### Naming Conventions

- Test files: `test_<module>.py`
- Test classes: `Test<Feature>`
- Test functions: `test_<behavior>` or `test_<scenario>_<expected_result>`

```python
# tests/unit/evaluation/test_mechanical.py
class TestMechanicalVerifier:
    async def test_lint_check_passes_on_clean_code(self): ...
    async def test_coverage_below_threshold_fails(self): ...
    async def test_timeout_returns_failure(self): ...
```

### Async Tests

The project uses `asyncio_mode = "auto"` in pytest config, so async tests just need the `async` keyword:

```python
async def test_semantic_evaluator_returns_result():
    evaluator = SemanticEvaluator(mock_adapter, config)
    result = await evaluator.evaluate(context)
    assert result.is_ok
```

### Testing with Result Type

Always check `is_ok` / `is_err` before accessing `.value` or `.error`:

```python
async def test_pipeline_returns_approval():
    result = await pipeline.evaluate(context)
    assert result.is_ok
    eval_result = result.value
    assert eval_result.final_approved is True
    assert eval_result.highest_stage_completed == 2

async def test_pipeline_returns_error_on_bad_input():
    result = await pipeline.evaluate(bad_context)
    assert result.is_err
    assert "validation" in result.error.message.lower()
```

### Mocking LLM Adapters

For unit tests, mock the LLM adapter to avoid real API calls:

```python
from unittest.mock import AsyncMock, MagicMock
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message

def make_mock_adapter(response_content: str) -> MagicMock:
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(
        CompletionResponse(content=response_content, model="test-model")
    ))
    return adapter

async def test_semantic_evaluator():
    adapter = make_mock_adapter('{"score": 0.9, "ac_compliance": true, ...}')
    evaluator = SemanticEvaluator(adapter)
    result = await evaluator.evaluate(context)
    assert result.is_ok
```

### Testing Frozen Dataclasses

Since most data models are frozen, test construction and property access:

```python
def test_check_result_is_immutable():
    result = CheckResult(check_type=CheckType.LINT, passed=True, message="OK")
    with pytest.raises(AttributeError):
        result.passed = False  # Should fail -- frozen

def test_mechanical_result_failed_checks():
    checks = (
        CheckResult(check_type=CheckType.LINT, passed=True, message="OK"),
        CheckResult(check_type=CheckType.TEST, passed=False, message="Failed"),
    )
    result = MechanicalResult(passed=False, checks=checks)
    assert len(result.failed_checks) == 1
    assert result.failed_checks[0].check_type == CheckType.TEST
```

### Testing TUI Components

Use Textual's test framework:

```python
from textual.app import App, ComposeResult

async def test_dashboard_renders():
    """Test that dashboard screen mounts without error."""
    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield DashboardScreenV3()

    async with TestApp().run_test() as pilot:
        # Verify the screen renders
        assert pilot.app.screen is not None
```

### Common Pitfalls

1. **Falsy-0 checks**: Always use `is not None` for index comparisons
   ```python
   # Bad
   if current_ac_index:  # Fails when index is 0
   # Good
   if current_ac_index is not None:
   ```

2. **Reactive mutation in Textual**: Do not mutate a reactive dict in-place then reassign the same reference. Create a new dict.

3. **Frozen dataclass creation**: Cannot set attributes after construction. Build all values before creating the object.

## Test Categories

| Category | Location | Speed | Dependencies | When to Run |
|----------|----------|-------|--------------|-------------|
| Unit | `tests/unit/` | Fast (<30s) | None | Every change |
| Integration | `tests/integration/` | Medium | Network, MCP | Before PR |
| E2E | `tests/e2e/` | Slow | Full system | Before release |

## CI Commands

```bash
# What CI runs (approximately):
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/ouroboros --ignore-missing-imports
uv run pytest tests/unit/ -v --cov=src/ouroboros
```
