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

### Focused Tests

Unit MCP tests use local fixtures and mocks; they do not inherently require
external servers. Install the `mcp-test` dependency group when running them:

```bash
uv sync --python 3.12 --dev --group mcp-test --group litellm-test
uv run --python 3.12 --no-sync pytest tests/unit/mcp/ -q
```

### TUI-Specific Tests

```bash
uv run pytest tests/unit/tui/ -q
```

### E2E Tests

End-to-end tests exercise the full CLI:

```bash
uv run pytest tests/e2e/ -v
```

Check each integration or E2E test's fixtures and markers for its external
requirements. Compare unexpected failures with the same command and environment
on `main`; a historical failure note is not permission to ignore a regression.

### HOME and Worker Isolation

`tests/conftest.py` redirects `HOME` to a temporary session directory before
test collection. This covers import-time configuration, database defaults, and
logging paths that resolve through `Path.home()` or `expanduser("~")`; child
processes inherit that home. `UV_CACHE_DIR` retains an explicit cache location
so packaging tests can use uv without relying on the real home.

The autouse `isolate_ouroboros_home` fixture gives each test another temporary
home. Tests needing a specific location may override `HOME` with `monkeypatch`
after that fixture. Do not restore the developer's home to make a test pass.
This contract describes the HOME-based paths; it does not claim isolation of
every platform-specific profile variable or external credential store.

Parallel execution is opt-in locally with pytest-xdist. Each worker runs the
collection-time isolation and per-test fixtures independently. The
`isolate_heartbeat_locks_per_test_worker` fixture also gives each worker private
heartbeat lock and cancellation directories, preventing illustrative session
IDs from colliding across workers while preserving lease semantics within one.

```bash
uv run --python 3.12 --no-sync pytest tests/unit/test_home_isolation.py -n 2 --dist loadgroup -q
```

Use `--dist loadgroup` for parallel runs that include wheel-building tests.
The collection hook marks consumers of the shared `built_wheel` fixture,
including indirect consumers, as `slow` and groups them on one worker. This
lets them reuse one session-scoped wheel instead of building one per worker.

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
| Unit | `tests/unit/` | Usually fastest when focused | Test dependencies; optional groups by module | Every change |
| Integration | `tests/integration/` | Varies | Real local components or external services, as specified by fixtures | Before PR |
| E2E | `tests/e2e/` | Varies | CLI/system prerequisites specified by each test | Before release |

The collection hook marks every test under `tests/integration/` as
`integration`, so `-m integration` selects that directory's tests. Marker
filters are independent of directory selection: `slow` wheel consumers also
exist under `tests/unit/`.

## CI Commands

```bash
# Python 3.12 pull-request test job in .github/workflows/test.yml
uv sync --python 3.12 --dev --group mcp-test --group litellm-test
uv run --python 3.12 --no-sync pytest tests/ -n 4 --dist loadgroup --durations=25 -m "not performance and not slow" -q

# Python 3.12 push test job: includes slow tests and collects coverage
uv run --python 3.12 --no-sync pytest tests/ -n 4 --dist loadgroup --durations=25 -m "not performance" --cov=src/ouroboros --cov-report=xml -q

# Separate lint and type-check gates
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/ouroboros
```

Pushes to main/develop also test Python 3.13 and 3.14. The 3.14 job omits
`litellm-test`. Pull requests exclude `performance` and `slow` tests; push
correctness jobs exclude only `performance`. Performance tests run separately
on pushes with `-m performance -n 0` in an advisory job. The workflow is
authoritative for the matrix and platform-specific jobs; see
[CI Gates](ci-gates.md).
