"""Manual smoke test for the Copilot CLI adapter against a real ``copilot`` binary.

Skipped automatically unless OUROBOROS_COPILOT_SMOKE=1 is set, so it never
runs in regular CI. Designed to be invoked on a developer workstation that
already has GitHub Copilot CLI authenticated via GH_TOKEN / GITHUB_TOKEN.
"""

from __future__ import annotations

import os

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.copilot_cli_adapter import CopilotCliLLMAdapter

SMOKE_ENABLED = os.environ.get("OUROBOROS_COPILOT_SMOKE", "").strip() == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(not SMOKE_ENABLED, reason="set OUROBOROS_COPILOT_SMOKE=1 to enable")
async def test_real_copilot_cli_returns_text() -> None:
    adapter = CopilotCliLLMAdapter(
        cwd=os.getcwd(),
        permission_mode="default",
        allowed_tools=[],
        timeout=120,
    )

    result = await adapter.complete(
        [Message(role=MessageRole.USER, content='Reply with exactly the word "ready" and nothing else.')],
        CompletionConfig(model="default", max_tokens=32),
    )

    assert result.is_ok, f"adapter returned error: {result.error}"
    assert result.value.content.strip()
    assert "ready" in result.value.content.lower()
