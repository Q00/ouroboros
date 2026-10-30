"""Claude 5.5 requests survive both adapter and installed LiteLLM transforms."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config import get_default_config
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole

MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
FORMATS = [
    None,
    {"type": "json_object"},
    {"type": "json_schema", "json_schema": {"type": "object"}},
]


@pytest.fixture(autouse=True)
def isolate_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ouroboros.providers.profiles.load_config", get_default_config)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("response_format", FORMATS)
@pytest.mark.parametrize("sampling", [(0.7, 1.0), (0.2, 0.8)])
async def test_direct_55_omits_sampling_and_prefill_without_corrupting_json(
    model: str, response_format: dict | None, sampling: tuple[float, float]
) -> None:
    adapter = AnthropicAdapter(api_key="test-only-placeholder")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"ok":true}')],
            model=model,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    adapter._client = client
    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(
            model=model,
            temperature=sampling[0],
            top_p=sampling[1],
            response_format=response_format,
            reasoning_effort="high",
        ),
    )
    assert result.is_ok
    assert result.value.content == '{"ok":true}'
    request = client.messages.create.await_args.kwargs
    assert not {"temperature", "top_p", "extra_body"} & request.keys()
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "high"}
    if response_format:
        assert "JSON" in request["system"]
        if response_format["type"] == "json_schema":
            assert '"type":"object"' in request["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-5", "claude-custom-proxy"]
)
async def test_explicit_legacy_direct_keeps_sampling_prefill_and_suffix_repair(model: str) -> None:
    adapter = AnthropicAdapter(api_key="test-only-placeholder")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text='"ok":true}')],
            model=model,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    adapter._client = client
    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(
            model=model, temperature=0.2, top_p=0.8, response_format={"type": "json_object"}
        ),
    )
    assert result.is_ok
    assert result.value.content == '{"ok":true}'
    request = client.messages.create.await_args.kwargs
    assert request["extra_body"] == {"temperature": 0.2, "top_p": 0.8}
    assert request["messages"][-1] == {"role": "assistant", "content": "{"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("route", ["", "anthropic/", "openrouter/anthropic/"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
@pytest.mark.parametrize("response_format", FORMATS)
async def test_litellm_55_actual_http_body(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    route: str,
    effort: str | None,
    response_format: dict | None,
) -> None:
    pytest.importorskip("litellm")
    import httpx

    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    is_openrouter = route.startswith("openrouter/")
    wire_model = f"anthropic/{model.replace('5-5', '5.5')}" if is_openrouter else model
    requests: list[dict] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: object
    ) -> httpx.Response:
        requests.append(json.loads(request.content))
        if is_openrouter:
            body = {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": wire_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            body = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": wire_model,
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    routed_model = f"{route}{model.replace('5-5', '5.5') if is_openrouter else model}"
    result = await LiteLLMAdapter(api_key="test-only-placeholder").complete(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(
            model=routed_model,
            temperature=0.2,
            top_p=0.8,
            reasoning_effort=effort,
            response_format=response_format,
        ),
    )
    assert result.is_ok
    assert result.value.content == '{"ok":true}'
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == wire_model
    assert not {"temperature", "top_p", "top_k", "extra_body"} & request.keys()
    assert request["messages"][-1]["role"] == "user"
    assert "budget_tokens" not in json.dumps(request)
    if effort:
        if is_openrouter:
            assert request["reasoning"] == {"effort": effort}
        else:
            assert request["thinking"] == {"type": "adaptive"}
            assert request["output_config"] == {"effort": effort}
    else:
        assert not {"thinking", "output_config", "reasoning"} & request.keys()
    if response_format:
        if is_openrouter:
            assert request["response_format"] == response_format
        else:
            assert "JSON" in json.dumps(request["system"])
            assert "tools" not in request


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["xhigh", "max"])
async def test_openrouter_55_preserves_distinct_effort_values(
    monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    # A raw CompletionConfig remains permissive at runtime; this does not add
    # max to the typed profile contract or treat xhigh as an Anthropic alias.
    await test_litellm_55_actual_http_body(
        monkeypatch, "claude-opus-5-5", "openrouter/anthropic/", effort, {"type": "json_object"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "thinking", "output_config"),
    [
        ("claude-haiku-4-5", {"type": "enabled", "budget_tokens": 4096}, None),
        ("claude-sonnet-4-6", {"type": "adaptive"}, {"effort": "high"}),
    ],
)
async def test_legacy_litellm_keeps_request_arguments(
    monkeypatch: pytest.MonkeyPatch, model: str, thinking: dict, output_config: dict | None
) -> None:
    pytest.importorskip("litellm")
    import httpx

    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    requests: list[dict] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: object
    ) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    result = await LiteLLMAdapter(api_key="test-only-placeholder").complete(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(model=model, temperature=0.2, top_p=0.8, reasoning_effort="high"),
    )
    assert result.is_ok
    assert len(requests) == 1
    assert requests[0]["model"] == model
    assert requests[0]["temperature"] == 0.2
    assert "top_p" not in requests[0]
    assert requests[0]["thinking"] == thinking
    assert requests[0].get("output_config") == output_config


@pytest.mark.parametrize(
    ("model", "top_p"),
    [
        ("claude-opus-5", None),
        ("claude-custom-proxy", None),
        ("openrouter/anthropic/claude-opus-5", None),
        ("openai/gpt-5.4", 0.8),
    ],
)
def test_other_models_keep_litellm_arguments(model: str, top_p: float | None) -> None:
    pytest.importorskip("litellm")
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    request = LiteLLMAdapter(api_key="test-only-placeholder")._build_completion_kwargs(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(
            model=model,
            temperature=0.2,
            top_p=0.8,
            reasoning_effort="high",
            response_format={"type": "json_object"},
        ),
    )
    assert request["model"] == model
    assert request["temperature"] == 0.2
    assert request.get("top_p") == top_p
    assert request["reasoning_effort"] == "high"
    assert request["response_format"] == {"type": "json_object"}
    assert not {"thinking", "output_config", "allowed_openai_params", "extra_body"} & request.keys()


def test_direct_litellm_xhigh_keeps_existing_capability_validation() -> None:
    pytest.importorskip("litellm")
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    request = LiteLLMAdapter(api_key="test-only-placeholder")._build_completion_kwargs(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(model="claude-opus-5-5", reasoning_effort="xhigh"),
    )
    assert request["reasoning_effort"] == "xhigh"
    assert not {"thinking", "output_config", "allowed_openai_params"} & request.keys()
