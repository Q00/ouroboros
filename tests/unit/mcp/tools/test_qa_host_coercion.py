"""QA accepts host-serialized JSON scalars and arrays without weakening validation."""

from unittest.mock import AsyncMock

from jsonschema import ValidationError, validate
import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo

ARGS = {"artifact": "example", "quality_bar": "works"}


def handler():
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.9}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    return QAHandler(llm_adapter=adapter, llm_backend="claude_code"), adapter


@pytest.mark.parametrize("threshold", ["0.8", "0", "1", "8e-1", " 0.8 "])
async def test_json_numeric_string_is_accepted(threshold):
    qa, adapter = handler()
    args = {**ARGS, "pass_threshold": threshold, "iteration_history": '[{"iteration": 1}]'}
    validate(args, qa.definition.to_input_schema())
    result = await qa.handle(args)
    assert result.is_ok
    assert result.value.meta["pass_threshold"] == float(threshold)
    adapter.complete.assert_awaited_once()


@pytest.mark.parametrize(
    "value", ["true", "null", "NaN", "Infinity", "1.1", "-0.1", "1e999", '"0.8"', "", "garbage"]
)
async def test_invalid_serialized_threshold_is_rejected_before_dispatch(value):
    qa, adapter = handler()
    result = await qa.handle({**ARGS, "pass_threshold": value})
    assert result.is_err
    adapter.complete.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    ["{}", "[1]", "[null]", '"[]"', "not json", "", '[{"score": NaN}]', '[{"score": Infinity}]'],
)
async def test_invalid_serialized_history_is_rejected_before_dispatch(value):
    qa, adapter = handler()
    result = await qa.handle({**ARGS, "iteration_history": value})
    assert result.is_err
    adapter.complete.assert_not_awaited()


def test_sdk_schema_rejects_out_of_range_number():
    with pytest.raises(ValidationError):
        validate({**ARGS, "pass_threshold": 1.1}, QAHandler().definition.to_input_schema())


async def test_sdk_tool_boundary_accepts_serialized_values():
    pytest.importorskip("mcp.server")
    from ouroboros.mcp.server.adapter import MCPServerAdapter
    from ouroboros.mcp.telemetry_boundary import call_sdk_tool

    qa, adapter = handler()
    server = MCPServerAdapter()
    server.register_tool(qa)
    values = {**ARGS, "pass_threshold": "0.8", "iteration_history": "[]"}
    validate(values, qa.definition.to_input_schema())
    result = await call_sdk_tool(server, "ouroboros_qa", values)
    assert not result.is_error
    assert result.meta["pass_threshold"] == 0.8
    adapter.complete.assert_awaited_once()
