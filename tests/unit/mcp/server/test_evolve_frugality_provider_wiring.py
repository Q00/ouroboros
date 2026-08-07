"""Production wiring for proof-eligible evolve completion providers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evolution.provider_usage import (
    capture_generation_provider_usage,
    tracked_complete,
)
from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.base import CompletionConfig, CompletionResponse, UsageInfo
import ouroboros.providers.credential_authority as credential_authority_module
from ouroboros.providers.factory import create_llm_adapter as real_create_llm_adapter
from ouroboros.providers.frugality_attestation import PreparedFrugalityCompletion

litellm_adapter_module = pytest.importorskip("ouroboros.providers.litellm_adapter")
LiteLLMAdapter = litellm_adapter_module.LiteLLMAdapter


@pytest.mark.asyncio
async def test_public_evolve_factory_emits_complete_litellm_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public handler must receive the real factory's proof-safe producer."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "low-entropy-production-key")
    monkeypatch.setattr(
        credential_authority_module,
        "installation_authority_key",
        lambda: b"i" * 32,
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")

    with (
        patch(
            "ouroboros.orchestrator.create_agent_runtime",
            return_value=MagicMock(),
        ),
        patch(
            "ouroboros.providers.create_llm_adapter",
            wraps=real_create_llm_adapter,
        ) as factory,
    ):
        server = create_ouroboros_server(
            event_store=store,
            state_dir=tmp_path / "state",
            runtime_backend="codex",
            llm_backend="litellm",
        )

    try:
        handler = server._tool_handlers["ouroboros_evolve_step"]
        adapter = handler.evolutionary_loop.reflect_engine.llm_adapter
        assert isinstance(adapter, LiteLLMAdapter)
        assert adapter._max_retries == 1
        assert handler.evolutionary_loop.wonder_engine.llm_adapter is adapter
        proof_factory_calls = [
            call for call in factory.call_args_list if call.kwargs.get("frugality_proof") is True
        ]
        assert len(proof_factory_calls) == 2
        assert all(call.kwargs["backend"] == "litellm" for call in proof_factory_calls)

        async def complete_prepared(
            messages: object,
            prepared: PreparedFrugalityCompletion,
        ) -> Result[CompletionResponse, object]:
            del messages
            return Result.ok(
                CompletionResponse(
                    content="{}",
                    model=prepared.config.model,
                    usage=UsageInfo(
                        prompt_tokens=8,
                        completion_tokens=2,
                        total_tokens=10,
                    ),
                )
            )

        monkeypatch.setattr(adapter, "frugality_complete_prepared", complete_prepared)
        with capture_generation_provider_usage() as capture:
            result = await tracked_complete(
                adapter,
                [],
                CompletionConfig(model="anthropic/claude-test", role="reflect"),
            )

        assert result.is_ok
        summary = capture.summary()
        assert summary.complete is True
        assert summary.call_count == 1
        assert summary.token_spend == 10
    finally:
        await server.shutdown()
