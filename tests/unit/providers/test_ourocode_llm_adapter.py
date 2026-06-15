"""Tests for the ourocode LLM adapter (ACP-backed completion path)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.ourocode_acp_client import AcpClientError, AcpTurnResult
from ouroboros.providers.ourocode_llm_adapter import OurocodeLLMAdapter


@pytest.mark.asyncio
async def test_complete_returns_completion_response() -> None:
    adapter = OurocodeLLMAdapter()
    turn = AcpTurnResult(text="hi there", stop_reason="end_turn", session_id="s1")
    with (
        patch.object(OurocodeLLMAdapter, "_compose_prompt", return_value="composed"),
        patch(
            "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
            new=AsyncMock(return_value=turn),
        ),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="claude-sonnet-4-6"),
        )

    assert result.is_ok
    response = result.value
    assert response.content == "hi there"
    assert response.model == "claude"
    assert response.raw_response["ourocode_model"] == "claude"
    # ACP carries no token usage — honestly zero, not fabricated.
    assert response.usage.total_tokens == 0
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_complete_maps_not_signed_in_to_provider_error() -> None:
    adapter = OurocodeLLMAdapter()
    err = AcpClientError("not signed in", error_type="not_signed_in")
    with patch(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
        new=AsyncMock(side_effect=err),
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="claude-sonnet-4-6"),
        )

    assert result.is_err
    assert result.error.provider == "ourocode"
    assert result.error.status_code == 401
    assert result.error.details["error_type"] == "not_signed_in"


@pytest.mark.asyncio
async def test_complete_passes_profile_model_selector_to_acp_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ourocode provider profile must select the actual OUROCODE_MODEL."""
    captured: list[dict[str, object]] = []

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

        async def run_turn(self, _prompt_text: str) -> AcpTurnResult:
            return AcpTurnResult(text="profiled", stop_reason="end_turn", session_id="s1")

    config = OuroborosConfig(
        llm_profiles={
            "oauth": {
                "providers": {
                    "ourocode": {
                        "model": "claude_api",
                    },
                },
            },
        }
    )
    monkeypatch.setattr(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient",
        CapturingClient,
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        result = await OurocodeLLMAdapter().complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="default", profile="oauth"),
        )

    assert result.is_ok
    assert captured[0]["model"] == "claude_api"
    assert result.value.model == "claude_api"
    assert result.value.raw_response["ourocode_model"] == "claude_api"


@pytest.mark.asyncio
async def test_complete_rejects_unsupported_raw_model_selector() -> None:
    """Arbitrary model ids must not be recorded while ourocode runs a fallback."""
    run_turn = AsyncMock(
        return_value=AcpTurnResult(text="should not run", stop_reason="end_turn", session_id="s1")
    )
    with patch(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
        new=run_turn,
    ):
        result = await OurocodeLLMAdapter().complete(
            messages=[Message(role=MessageRole.USER, content="hi")],
            config=CompletionConfig(model="gpt-5"),
        )

    assert result.is_err
    assert result.error.provider == "ourocode"
    assert result.error.details["model"] == "gpt-5"
    assert run_turn.await_count == 0


@pytest.mark.asyncio
async def test_complete_extracts_json_object_response_format() -> None:
    adapter = OurocodeLLMAdapter()
    turn = AcpTurnResult(text='here is JSON: {"ok": true}', stop_reason="end_turn", session_id="s1")
    run_turn = AsyncMock(return_value=turn)
    with patch(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
        new=run_turn,
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="return status")],
            config=CompletionConfig(
                model="claude-sonnet-4-6",
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_ok
    assert result.value.content == '{"ok": true}'
    prompt = run_turn.await_args.args[0]
    assert "Respond with ONLY a valid JSON object" in prompt
    assert "return status" in prompt


@pytest.mark.asyncio
async def test_complete_rejects_non_json_response_format_output() -> None:
    adapter = OurocodeLLMAdapter()
    turn = AcpTurnResult(text="plain prose, not json", stop_reason="end_turn", session_id="s1")
    run_turn = AsyncMock(return_value=turn)
    with patch(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
        new=run_turn,
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="return status")],
            config=CompletionConfig(
                model="claude-sonnet-4-6",
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_err
    assert result.error.provider == "ourocode"
    assert result.error.details["last_response_preview"] == "plain prose, not json"
    assert run_turn.await_count == 3


@pytest.mark.asyncio
async def test_complete_validates_json_schema_response_format() -> None:
    adapter = OurocodeLLMAdapter()
    turn = AcpTurnResult(text='{"ok": true}', stop_reason="end_turn", session_id="s1")
    run_turn = AsyncMock(return_value=turn)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    with patch(
        "ouroboros.providers.ourocode_llm_adapter.OurocodeAcpClient.run_turn",
        new=run_turn,
    ):
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="return status")],
            config=CompletionConfig(
                model="claude-sonnet-4-6",
                response_format={"type": "json_schema", "json_schema": schema},
            ),
        )

    assert result.is_ok
    assert result.value.content == '{"ok": true}'
    prompt = run_turn.await_args.args[0]
    assert "JSON schema" in prompt
    assert '"additionalProperties": false' in prompt


def test_compose_prompt_single_user_message() -> None:
    composed = OurocodeLLMAdapter._compose_prompt(
        [Message(role=MessageRole.USER, content="just do it")]
    )
    assert composed == "just do it"


def test_compose_prompt_includes_system_and_history() -> None:
    composed = OurocodeLLMAdapter._compose_prompt(
        [
            Message(role=MessageRole.SYSTEM, content="be terse"),
            Message(role=MessageRole.USER, content="q1"),
            Message(role=MessageRole.ASSISTANT, content="a1"),
            Message(role=MessageRole.USER, content="q2"),
        ]
    )
    assert "## System Instructions\nbe terse" in composed
    assert "User: q1" in composed
    assert "Assistant: a1" in composed
    assert "User: q2" in composed


@pytest.mark.asyncio
async def test_complete_via_fake_acp_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the real ACP client against the fake server."""
    from pathlib import Path

    fake = Path(__file__).parents[2] / "fixtures" / "fake_ourocode_acp.py"
    monkeypatch.setenv("FAKE_ACP_MODE", "ok")
    adapter = OurocodeLLMAdapter(cli_path=fake, cwd=tmp_path, timeout=20.0)
    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="hi")],
        config=CompletionConfig(model="claude-sonnet-4-6"),
    )
    assert result.is_ok
    assert result.value.content == "Hello, world!"


def test_adapter_coalesces_none_timeout_to_bounded_default() -> None:
    """The factory passes timeout=None; the adapter must never store None
    (which would disable the per-turn wait_for guard)."""
    assert OurocodeLLMAdapter(timeout=None)._timeout == 600.0
    assert OurocodeLLMAdapter(timeout=12.0)._timeout == 12.0


def test_ourocode_llm_adapter_accessible_from_providers_package() -> None:
    import ouroboros.providers as providers

    assert providers.OurocodeLLMAdapter is OurocodeLLMAdapter
