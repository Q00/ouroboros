"""Opt-in smoke tests for the real Zcode CLI integration.

Skipped by default so regular CI does not require ZCode.app, Z.ai credentials,
or network access. To run locally, install/authenticate ZCode and set
``OUROBOROS_ZCODE_SMOKE=1``. When ZCode is installed as the macOS app bundle,
also set ``OUROBOROS_ZCODE_CLI_PATH`` to the bundled ``zcode.cjs`` script.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from ouroboros.config import get_zcode_cli_path
from ouroboros.orchestrator.runtime_factory import create_agent_runtime
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.zcode_cli_adapter import ZcodeCliLLMAdapter

SMOKE_ENABLED = os.environ.get("OUROBOROS_ZCODE_SMOKE", "").strip() == "1"


def _configured_zcode_cli_path() -> str | None:
    configured_path = get_zcode_cli_path()
    if not configured_path:
        return None
    return configured_path


def _has_zcode_cli() -> bool:
    configured_path = _configured_zcode_cli_path()
    if configured_path:
        return Path(configured_path).expanduser().exists()
    return shutil.which("zcode") is not None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not SMOKE_ENABLED,
    reason="set OUROBOROS_ZCODE_SMOKE=1 to enable the real Zcode CLI smoke test",
)
async def test_real_zcode_runtime_returns_terminal_response(tmp_path: Path) -> None:
    """Prove terminal Ouroboros can execute a task through the Zcode runtime."""
    if not _has_zcode_cli():
        pytest.skip(
            "Zcode CLI is not on PATH and no existing Zcode CLI path is configured "
            "via OUROBOROS_ZCODE_CLI_PATH or config"
        )

    runtime = create_agent_runtime(
        backend="zcode",
        cwd=tmp_path,
        permission_mode="acceptEdits",
    )

    async with asyncio.timeout(180):
        result = await runtime.execute_task_to_result(
            'Reply with exactly the word "ready" and nothing else.',
            tools=[],
            system_prompt="You are running a smoke test. Return only the requested word.",
        )

    assert runtime.runtime_backend == "zcode_cli"
    assert result.is_ok, f"Zcode runtime returned error: {result.error}"
    task_result = result.value
    assert task_result.success is True
    assert task_result.final_message.strip()
    assert task_result.final_message.strip().lower() == "ready"
    assert task_result.messages


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not SMOKE_ENABLED,
    reason="set OUROBOROS_ZCODE_SMOKE=1 to enable the real Zcode CLI smoke test",
)
async def test_real_zcode_llm_adapter_honors_json_object_response_format(
    tmp_path: Path,
) -> None:
    """Prove terminal Ouroboros can use Zcode for structured LLM roles."""
    if not _has_zcode_cli():
        pytest.skip(
            "Zcode CLI is not on PATH and no existing Zcode CLI path is configured "
            "via OUROBOROS_ZCODE_CLI_PATH or config"
        )

    adapter = ZcodeCliLLMAdapter(
        cli_path=_configured_zcode_cli_path(),
        cwd=tmp_path,
        permission_mode="acceptEdits",
        allowed_tools=[],
        timeout=180,
    )

    async with asyncio.timeout(180):
        result = await adapter.complete(
            [
                Message(
                    role=MessageRole.USER,
                    content='Return exactly this JSON object: {"status": "ready"}',
                )
            ],
            CompletionConfig(
                model="default",
                max_tokens=64,
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_ok, f"Zcode LLM adapter returned error: {result.error}"
    assert json.loads(result.value.content) == {"status": "ready"}
