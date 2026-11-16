"""Claude sampling and prefill go only to families documented to accept them.

Claude Opus 4.7 and later, Sonnet 5, every 5.5 model, and all Fable/Mythos
models reject ``temperature`` / ``top_p`` / ``top_k`` with HTTP 400. Claude 4
through 4.6 reject ``temperature`` together with ``top_p``. Assistant prefill
returns 400 from Claude 4.6 onward. The gate fails safe: an alias, the
``default`` sentinel, or any unrecognised Claude id gets neither.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config import get_default_config
from ouroboros.config._model_defaults import DEFAULT_SONNET_MODEL
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.anthropic_models import (
    claude_accepts_prefill,
    claude_accepts_sampling,
    claude_accepts_temperature_and_top_p,
    omit_claude_sampling,
)
from ouroboros.providers.base import CompletionConfig, Message, MessageRole

# Claude 3.x: temperature and top_p together, and assistant prefill.
CLAUDE_3 = [
    "claude-3-5-haiku-latest",
    "claude-3-7-sonnet-20250219",
    "claude-3-opus-20240229",
    "claude-3-haiku-20240307",
    "anthropic/claude-3-5-sonnet-20241022",
    "openrouter/anthropic/claude-3.5-sonnet",
    "openrouter/anthropic/claude-3.7-sonnet:thinking",
    "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
    "vertex_ai/claude-3-5-sonnet-v2@20241022",
]
# Claude 4 through 4.5: one of temperature / top_p, and assistant prefill.
CLAUDE_4_PREFILL = [
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-sonnet-4-0",
    "claude-opus-4-1",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5",
    "anthropic/claude-haiku-4-5",
    "openrouter/anthropic/claude-sonnet-4.5",
    "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "bedrock/converse/global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "vertex_ai/claude-opus-4-1@20250805",
]
# Claude 4.6: one of temperature / top_p, no prefill.
CLAUDE_4_6 = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-6-20260205",
    "anthropic/claude-sonnet-4-6",
    "openrouter/anthropic/claude-opus-4.6",
    "bedrock/us.anthropic.claude-opus-4-6-v1",
    "vertex_ai/claude-sonnet-4-6",
]
# Neither sampling nor prefill: current, aliased, sentinel and unknown ids.
MODERN_OR_UNKNOWN = [
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5",
    "claude-fable-5-2",
    "claude-mythos-5-1",
    "claude-mythos-5-20260101",
    "anthropic/claude-sonnet-5",
    "openrouter/anthropic/claude-sonnet-5",
    "openrouter/anthropic/claude-opus-4.8",
    "openrouter/anthropic/claude-opus-5.5",
    "bedrock/us.anthropic.claude-opus-4-7-v1:0",
    "bedrock/converse/anthropic.claude-opus-5-5",
    "vertex_ai/claude-sonnet-5",
    "opus",
    "sonnet",
    "haiku",
    "default",
    "claude-opus-6",
    "claude-sonnet-5-7",
    "claude-opus-4-10",
    "claude-custom-proxy",
]
NON_CLAUDE = [
    "gpt-6.1-sol",
    "groq/llama-4",
    "gemini/gemini-3-pro",
    "openrouter/meta-llama/llama-4-maverick",
]

TABLE = (
    [(model, "claude-3") for model in CLAUDE_3]
    + [(model, "claude-4") for model in CLAUDE_4_PREFILL]
    + [(model, "claude-4.6") for model in CLAUDE_4_6]
    + [(model, "modern") for model in MODERN_OR_UNKNOWN]
    + [(model, "non-claude") for model in NON_CLAUDE]
)


@pytest.fixture(autouse=True)
def isolate_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ouroboros.providers.profiles.load_config", get_default_config)


@pytest.mark.parametrize(("model", "family"), TABLE)
def test_family_predicates(model: str, family: str) -> None:
    legacy = family in ("claude-3", "claude-4", "claude-4.6")
    assert claude_accepts_sampling(model) is legacy
    assert claude_accepts_temperature_and_top_p(model) is (family == "claude-3")
    assert claude_accepts_prefill(model) is (family in ("claude-3", "claude-4"))
    assert omit_claude_sampling(model) is (family == "modern")


@pytest.mark.parametrize(("model", "family"), TABLE)
@pytest.mark.parametrize("effort", [None, "high"])
def test_litellm_kwargs_follow_the_family(model: str, family: str, effort: str | None) -> None:
    pytest.importorskip("litellm")
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    kwargs = LiteLLMAdapter(api_key="test-only-placeholder")._build_completion_kwargs(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(model=model, temperature=0.2, top_p=0.8, reasoning_effort=effort),
    )
    if family == "non-claude":
        # Unchanged: non-Claude routes keep both sampling controls.
        assert kwargs["temperature"] == 0.2
        assert kwargs["top_p"] == 0.8
    elif family == "modern":
        assert not {"temperature", "top_p", "top_k"} & kwargs.keys()
    else:
        # Claude 4.x rejects the pair, so Claude routes never get top_p.
        assert kwargs["temperature"] == 0.2
        assert "top_p" not in kwargs


@pytest.mark.parametrize(("model", "family"), TABLE)
@pytest.mark.parametrize("effort", [None, "high"])
def test_proof_worker_accepts_exactly_what_the_adapter_builds(
    model: str, family: str, effort: str | None
) -> None:
    pytest.importorskip("litellm")
    from ouroboros.providers import litellm_proof_worker as worker
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    config = CompletionConfig(model=model, temperature=0.2, top_p=0.8, reasoning_effort=effort)
    dispatch = SimpleNamespace(
        api_key="test-only", api_base="https://provider.example/v1", timeout=30.0
    )
    kwargs = LiteLLMAdapter(api_key="test-only")._build_completion_kwargs(
        [Message(role=MessageRole.USER, content="hello")], config, _sealed_dispatch=dispatch
    )
    assert worker._valid_completion_kwargs(kwargs, config)
    if family == "modern":
        assert not worker._valid_completion_kwargs({**kwargs, "temperature": 0.2}, config)
        assert not worker._valid_completion_kwargs({**kwargs, "top_p": 0.8}, config)
    else:
        missing_temperature = {k: v for k, v in kwargs.items() if k != "temperature"}
        assert not worker._valid_completion_kwargs(missing_temperature, config)


# Ids the direct adapter sends as-is (it strips ``anthropic/`` and
# ``openrouter/anthropic/``); everything else falls back to its default model.
DIRECT_TABLE = [
    ("claude-3-5-haiku-latest", "claude-3-5-haiku-latest", "claude-3"),
    ("claude-3-7-sonnet-20250219", "claude-3-7-sonnet-20250219", "claude-3"),
    ("claude-sonnet-4-20250514", "claude-sonnet-4-20250514", "claude-4"),
    ("claude-opus-4-1-20250805", "claude-opus-4-1-20250805", "claude-4"),
    ("claude-haiku-4-5", "claude-haiku-4-5", "claude-4"),
    ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5", "claude-4"),
    ("claude-opus-4-6", "claude-opus-4-6", "claude-4.6"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6", "claude-4.6"),
    ("claude-opus-4-7", "claude-opus-4-7", "modern"),
    ("claude-opus-4-8", "claude-opus-4-8", "modern"),
    ("claude-opus-5", "claude-opus-5", "modern"),
    ("claude-sonnet-5", "claude-sonnet-5", "modern"),
    ("openrouter/anthropic/claude-sonnet-5", "claude-sonnet-5", "modern"),
    ("claude-haiku-5-5", "claude-haiku-5-5", "modern"),
    ("claude-fable-5-2", "claude-fable-5-2", "modern"),
    ("claude-mythos-5-1", "claude-mythos-5-1", "modern"),
    ("claude-opus-6", "claude-opus-6", "modern"),
    ("claude-sonnet-5-7", "claude-sonnet-5-7", "modern"),
    ("claude-custom-proxy", "claude-custom-proxy", "modern"),
    # Aliases, the sentinel and non-Claude ids resolve to the default model,
    # which is unchanged behavior and still gets neither field.
    ("opus", DEFAULT_SONNET_MODEL, "modern"),
    ("sonnet", DEFAULT_SONNET_MODEL, "modern"),
    ("haiku", DEFAULT_SONNET_MODEL, "modern"),
    ("default", DEFAULT_SONNET_MODEL, "modern"),
    ("gpt-6.1-sol", DEFAULT_SONNET_MODEL, "modern"),
    ("groq/llama-4", DEFAULT_SONNET_MODEL, "modern"),
    ("gemini/gemini-3-pro", DEFAULT_SONNET_MODEL, "modern"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "wire_model", "family"), DIRECT_TABLE)
async def test_anthropic_adapter_request_follows_the_family(
    model: str, wire_model: str, family: str
) -> None:
    prefill = family in ("claude-3", "claude-4")
    adapter = AnthropicAdapter(api_key="test-only-placeholder")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text='"ok":true}' if prefill else '{"ok":true}')],
            model=wire_model,
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
    assert request["model"] == wire_model
    assert not {"temperature", "top_p", "top_k"} & request.keys()
    if family == "claude-3":
        assert request["extra_body"] == {"temperature": 0.2, "top_p": 0.8}
    elif family in ("claude-4", "claude-4.6"):
        assert request["extra_body"] == {"temperature": 0.2}
    else:
        assert "extra_body" not in request
    if prefill:
        assert request["messages"][-1] == {"role": "assistant", "content": "{"}
        assert "system" not in request
    else:
        assert request["messages"] == [{"role": "user", "content": "hello"}]
        assert "valid JSON object" in request["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "wire_model", "temperature"),
    [
        # An explicit provider keeps routing independent of LiteLLM's model map.
        ("anthropic/claude-sonnet-5", "claude-sonnet-5", None),
        ("claude-opus-4-8", "claude-opus-4-8", None),
        ("openrouter/anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", None),
        ("claude-sonnet-4-5", "claude-sonnet-4-5", 0.2),
    ],
)
async def test_litellm_http_body_follows_the_family(
    monkeypatch: pytest.MonkeyPatch, model: str, wire_model: str, temperature: float | None
) -> None:
    pytest.importorskip("litellm")
    import httpx

    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    openrouter = model.startswith("openrouter/")
    requests: list[dict] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: object
    ) -> httpx.Response:
        requests.append(json.loads(request.content))
        if openrouter:
            body: dict[str, object] = {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": wire_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
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
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    result = await LiteLLMAdapter(api_key="test-only-placeholder").complete(
        [Message(role=MessageRole.USER, content="hello")],
        CompletionConfig(model=model, temperature=0.2, top_p=0.8),
    )
    assert result.is_ok
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == wire_model
    assert request.get("temperature") == temperature
    assert not {"top_p", "top_k"} & request.keys()
