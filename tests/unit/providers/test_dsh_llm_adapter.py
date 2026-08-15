"""Tests for the DeepSeek Harness (dsh) LLM adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.dsh_acp_client import AcpClientError, AcpTurnResult
from ouroboros.providers.dsh_llm_adapter import DshLLMAdapter


def _patch_turn(turn: AcpTurnResult | list[AcpTurnResult]) -> AsyncMock:
    side_effect = turn if isinstance(turn, list) else [turn]
    return AsyncMock(side_effect=side_effect)


@pytest.mark.asyncio
async def test_complete_returns_completion_response() -> None:
    adapter = DshLLMAdapter()
    turn = AcpTurnResult(text="hi there", stop_reason="end_turn", session_id="s1")
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=_patch_turn(turn),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_ok
    response = result.value
    assert response.content == "hi there"
    # The composition file owns the real model; the adapter must not claim a
    # specific model id it never selected.
    assert response.model == "dsh-composition"
    assert response.raw_response["backend"] == "dsh"
    assert response.raw_response["stop_reason"] == "end_turn"
    # ACP carries no token usage — honestly zero, not fabricated.
    assert response.usage.total_tokens == 0
    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    ("text", "stop_reason"),
    [
        ("", "end_turn"),
        ("", "refusal"),
        ("   \n\t", "end_turn"),
    ],
    ids=["end-turn", "refusal", "whitespace-only"],
)
@pytest.mark.asyncio
async def test_complete_rejects_a_turn_that_produced_no_text(text: str, stop_reason: str) -> None:
    """A turn with no text is a failed turn, whatever it stopped for."""
    adapter = DshLLMAdapter()
    turn = AcpTurnResult(text=text, stop_reason=stop_reason, session_id="s1")
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=_patch_turn(turn),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_err, f"empty turn stopping on {stop_reason!r} must not report success"
    assert result.error.provider == "dsh"
    assert result.error.details == {"stop_reason": stop_reason}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("max_turn_requests", "length"),
        ("cancelled", "cancelled"),
        ("refusal", "refusal"),
    ],
)
@pytest.mark.asyncio
async def test_finish_reason_normalization(stop_reason: str, expected: str) -> None:
    adapter = DshLLMAdapter()
    turn = AcpTurnResult(text="partial answer", stop_reason=stop_reason, session_id="s1")
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=_patch_turn(turn),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_ok
    assert result.value.finish_reason == expected
    assert result.value.raw_response["stop_reason"] == stop_reason


@pytest.mark.asyncio
async def test_acp_error_maps_to_provider_error() -> None:
    adapter = DshLLMAdapter()
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=AsyncMock(
            side_effect=AcpClientError(
                "dsh-acp-demo not found or not launchable: ...",
                error_type="cli_unavailable",
            )
        ),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.provider == "dsh"
    assert result.error.status_code == 503
    assert result.error.details["error_type"] == "cli_unavailable"


@pytest.mark.asyncio
async def test_timeout_maps_to_504() -> None:
    adapter = DshLLMAdapter()
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=AsyncMock(
            side_effect=AcpClientError(
                "dsh-acp-demo ACP session/prompt timed out after 600s",
                error_type="timeout",
            )
        ),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.status_code == 504


@pytest.mark.asyncio
async def test_structured_response_format_extracts_and_validates_json() -> None:
    adapter = DshLLMAdapter()
    payload = {"answer": 42}
    turn = AcpTurnResult(
        text=f"Sure — here you go:\n```json\n{json.dumps(payload)}\n```",
        stop_reason="end_turn",
        session_id="s1",
    )
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=_patch_turn(turn),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(
                model="default",
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_ok
    assert json.loads(result.value.content) == payload


@pytest.mark.asyncio
async def test_structured_response_format_fails_after_bounded_retries() -> None:
    adapter = DshLLMAdapter()
    bad_turn = AcpTurnResult(text="not json at all", stop_reason="end_turn", session_id="s1")
    mock = AsyncMock(return_value=bad_turn)
    with patch(
        "ouroboros.providers.dsh_llm_adapter.DshAcpClient.run_turn",
        new=mock,
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(
                model="default",
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_err
    assert result.error.provider == "dsh"
    assert mock.await_count == 3


@pytest.mark.asyncio
async def test_client_receives_config_path_and_timeouts() -> None:
    """The adapter must thread cli/config/timeout wiring into the client."""
    captured: dict[str, object] = {}

    class _SpyClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def run_turn(self, prompt_text: str, **_: object) -> AcpTurnResult:
            return AcpTurnResult(text="ok", stop_reason="end_turn", session_id="s1")

    adapter = DshLLMAdapter(
        cli_path="/opt/dsh/dsh-acp-demo",
        cwd="/work",
        config_path="/etc/dsh/cordis.yml",
        timeout=123.0,
        startup_timeout=9.0,
    )
    with patch("ouroboros.providers.dsh_llm_adapter.DshAcpClient", _SpyClient):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert result.is_ok
    assert captured == {
        "cli_path": "/opt/dsh/dsh-acp-demo",
        "cwd": "/work",
        "config_path": "/etc/dsh/cordis.yml",
        "startup_timeout": 9.0,
        "turn_timeout": 123.0,
    }


@pytest.mark.asyncio
async def test_none_timeout_is_coalesced_to_bounded_default() -> None:
    """The shared factory passes ``timeout=None``; that must never disable the
    per-turn guard."""
    captured: dict[str, object] = {}

    class _SpyClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def run_turn(self, prompt_text: str, **_: object) -> AcpTurnResult:
            return AcpTurnResult(text="ok", stop_reason="end_turn", session_id="s1")

    adapter = DshLLMAdapter(timeout=None)
    with patch("ouroboros.providers.dsh_llm_adapter.DshAcpClient", _SpyClient):
        await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default"),
        )

    assert captured["turn_timeout"] == 600.0
