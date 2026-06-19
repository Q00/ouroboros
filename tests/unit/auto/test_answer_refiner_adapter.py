"""Tests for ``LLMAnswerRefiner`` (the LLM-backed answer refiner adapter).

These pin the two properties that make the refiner converge instead of
oscillate: it is DETERMINISTIC (temperature 0) and it is ANCHORED (the
contracts already committed this interview are injected into the prompt so the
model preserves them verbatim instead of re-deciding them every round).
"""

from __future__ import annotations

import pytest

from ouroboros.auto.adapters import LLMAnswerRefiner
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)


class _CapturingAdapter:
    """Fake ``LLMAdapter`` that records the last call and returns fixed text."""

    def __init__(self, content: str = "concrete answer") -> None:
        self.content = content
        self.messages: list[Message] | None = None
        self.config: CompletionConfig | None = None

    async def complete(self, messages, config):
        self.messages = messages
        self.config = config
        return Result.ok(
            CompletionResponse(
                content=self.content,
                model="fake",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _user_text(adapter: _CapturingAdapter) -> str:
    assert adapter.messages is not None
    return "\n".join(m.content for m in adapter.messages if m.role == MessageRole.USER)


@pytest.mark.asyncio
async def test_refiner_is_deterministic_temperature_zero() -> None:
    adapter = _CapturingAdapter()
    refiner = LLMAnswerRefiner(adapter)

    out = await refiner("goal", "question", "constraints", "generic")

    assert out == "concrete answer"
    assert adapter.config is not None
    # Determinism is the convergence guarantee: same inputs -> same answer.
    assert adapter.config.temperature == 0.0


@pytest.mark.asyncio
async def test_committed_contracts_are_injected_into_prompt() -> None:
    adapter = _CapturingAdapter()
    refiner = LLMAnswerRefiner(adapter)

    committed = [
        ("outputs", "outputs.add", 'todo add "X" prints "Added #1: X"'),
        ("constraints", "constraints.exit", "invalid id exits 1"),
    ]
    await refiner("build a todo CLI", "what list format?", "outputs", "generic", committed)

    text = _user_text(adapter)
    # The model must SEE every prior commitment so it can preserve them verbatim.
    assert 'todo add "X" prints "Added #1: X"' in text
    assert "invalid id exits 1" in text
    assert "committed" in text.lower()


@pytest.mark.asyncio
async def test_no_committed_block_when_none_committed() -> None:
    adapter = _CapturingAdapter()
    refiner = LLMAnswerRefiner(adapter)

    await refiner("goal", "question", "constraints", "generic")

    text = _user_text(adapter)
    # Round 1 (nothing decided yet) must not fabricate a commitments section.
    assert "already committed" not in text.lower()


@pytest.mark.asyncio
async def test_provider_error_returns_none() -> None:
    from ouroboros.core.errors import ProviderError

    class _FailingAdapter:
        async def complete(self, messages, config):  # noqa: ARG002
            return Result.err(ProviderError("provider down"))

    refiner = LLMAnswerRefiner(_FailingAdapter())
    assert await refiner("g", "q", "constraints", "generic") is None
