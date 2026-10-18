"""Current API pins reach dispatch while explicit model choices remain literal."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.config.model_selection import resolve_role_model
from ouroboros.config.models import OuroborosConfig
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("OUROBOROS_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr("ouroboros.providers.profiles.load_config", get_default_config)
    monkeypatch.setattr("ouroboros.config.loader.load_config", get_default_config)


def response(model: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        model=model,
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", "auto", "default"])
async def test_direct_default_reaches_current_standard_pin(model: str) -> None:
    adapter = AnthropicAdapter(api_key="test-key")
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response("claude-sonnet-5-5"))
    adapter._client = client

    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="hello")], CompletionConfig(model=model)
    )

    assert result.is_ok
    assert client.messages.create.await_args.kwargs["model"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-custom-proxy"])
@pytest.mark.parametrize("profile", [False, True])
async def test_explicit_direct_request_and_profile_keep_literal_model(
    model: str, profile: bool
) -> None:
    adapter = AnthropicAdapter(api_key="test-key")
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response(model))
    adapter._client = client
    config = OuroborosConfig(llm_profiles={"chosen": {"model": model}})
    request = (
        CompletionConfig(model="auto", profile="chosen")
        if profile
        else CompletionConfig(model=model)
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        result = await adapter.complete([Message(role=MessageRole.USER, content="hello")], request)

    assert result.is_ok
    assert client.messages.create.await_args.kwargs["model"] == model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "invocation", "expected"),
    [
        ("brownfield_scan", None, "claude-haiku-5-5"),
        ("validation", None, "claude-sonnet-5-5"),
        ("interview", None, "claude-opus-5-5"),
        ("consensus_advocate", None, "openrouter/anthropic/claude-opus-5.5"),
        ("qa", None, "claude-opus-5-5"),
        ("execute", None, "claude-opus-5-5"),
        ("execute", "frugal", "claude-haiku-5-5"),
        ("execute", "standard", "claude-sonnet-5-5"),
        ("execute", "frontier", "claude-opus-5-5"),
    ],
)
async def test_litellm_resolved_roles_and_tiers_reach_current_provider_model(
    role: str, invocation: str | None, expected: str
) -> None:
    pytest.importorskip("litellm")
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter
    from tests.unit.providers.test_litellm_adapter import create_mock_response

    resolved = resolve_role_model(role, backend="litellm", invocation_model=invocation)
    with patch("litellm.acompletion", new_callable=AsyncMock) as dispatch:
        dispatch.return_value = create_mock_response(model=expected)
        result = await LiteLLMAdapter(api_key="test-key").complete(
            [Message(role=MessageRole.USER, content="hello")],
            CompletionConfig(model=resolved.model),
        )

    assert result.is_ok
    assert dispatch.await_args.kwargs["model"] == expected
