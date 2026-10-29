"""The QA stop threshold must belong to the advertised finite score scale."""

from unittest.mock import AsyncMock

from jsonschema import ValidationError, validate
import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo

_ARGS = {"artifact": "example", "quality_bar": "works"}


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_qa_schema_rejects_out_of_range_threshold(threshold):
    with pytest.raises(ValidationError):
        validate({**_ARGS, "pass_threshold": threshold}, QAHandler().definition.to_input_schema())


@pytest.mark.parametrize("backend", ["claude", "opencode"])
@pytest.mark.parametrize(
    "threshold", [-0.01, 1.01, float("nan"), float("inf"), True, None, [], "invalid"]
)
async def test_invalid_qa_threshold_returns_error_before_dispatch(backend, threshold):
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.0}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    handler = QAHandler(llm_adapter=adapter, agent_runtime_backend=backend, opencode_mode="plugin")
    result = await handler.handle({**_ARGS, "pass_threshold": threshold})
    assert result.is_err
    assert "pass_threshold" in str(result.error)
    adapter.complete.assert_not_awaited()


@pytest.mark.parametrize(
    ("threshold", "score", "passed"), [(0, 0, True), (1, 1, True), (1, 0.99, False)]
)
async def test_qa_threshold_boundaries_preserve_score_comparison(threshold, score, passed):
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(content=f'{{"score": {score}}}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    handler = QAHandler(llm_adapter=adapter, llm_backend="claude_code")
    result = await handler.handle({**_ARGS, "pass_threshold": threshold})
    assert result.is_ok
    assert result.value.meta["passed"] is passed


async def test_qa_omitted_threshold_keeps_default():
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.8}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    result = await QAHandler(llm_adapter=adapter, llm_backend="claude_code").handle(_ARGS)
    assert result.is_ok
    assert result.value.meta["pass_threshold"] == 0.8
    assert result.value.meta["passed"] is True
