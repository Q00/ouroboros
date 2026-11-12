"""Exercise installed SDK serialization and parsing without a provider connection."""

import json

import pytest

from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-sonnet-5-5"])
@pytest.mark.parametrize("structured", [False, True])
async def test_installed_sdk_preserves_completion_wire_contract(
    model: str, structured: bool
) -> None:
    requests = []
    text = '"ok": true}' if structured and model == "claude-sonnet-4-6" else '{"ok": true}'

    def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 5},
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http_client:
        async with anthropic.AsyncAnthropic(
            api_key="synthetic-not-a-credential", http_client=http_client, max_retries=0
        ) as client:
            adapter = AnthropicAdapter(api_key="synthetic-not-a-credential")
            adapter._client = client
            result = await adapter.complete(
                [
                    Message(role=MessageRole.SYSTEM, content="synthetic instruction"),
                    Message(role=MessageRole.USER, content="synthetic input"),
                ],
                CompletionConfig(
                    model=model,
                    max_tokens=32,
                    reasoning_effort="low",
                    response_format={"type": "json_object"} if structured else None,
                ),
            )

    assert result.is_ok, result.error if result.is_err else None
    assert json.loads(result.value.content) == {"ok": True}
    assert result.value.model == model
    assert result.value.usage.prompt_tokens == 3
    assert result.value.usage.completion_tokens == 5
    assert result.value.usage.total_tokens == 8
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/messages"
    body = json.loads(request.content)
    assert body["model"] == model
    assert body["max_tokens"] == 32
    assert body["messages"][0] == {"role": "user", "content": "synthetic input"}
    assert body["system"].startswith("synthetic instruction")
    assert body["output_config"] == {"effort": "low"}
    if model == "claude-sonnet-4-6":
        assert body["thinking"] == {"type": "adaptive"}
        assert "temperature" in body and "top_p" in body
        if structured:
            assert body["messages"][-1] == {"role": "assistant", "content": "{"}
    else:
        assert "temperature" not in body and "top_p" not in body
        if structured:
            assert len(body["messages"]) == 1
            assert "JSON" in body["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_installed_sdk_errors_reach_public_provider_error(status: int) -> None:
    def respond(request):
        return httpx2.Response(
            status,
            json={"type": "error", "error": {"type": "api_error", "message": "synthetic error"}},
            request=request,
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http_client:
        async with anthropic.AsyncAnthropic(
            api_key="synthetic-not-a-credential", http_client=http_client, max_retries=0
        ) as client:
            adapter = AnthropicAdapter(api_key="synthetic-not-a-credential")
            adapter._client = client
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="synthetic input")],
                CompletionConfig(model="claude-sonnet-5-5", max_tokens=32),
            )

    assert result.is_err
    assert result.error.provider == "anthropic"
    assert result.error.status_code == status
