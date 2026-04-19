"""Regression tests for :class:`LateralThinkHandler`.

Verifies the multi-persona fan-out path honours the shared
``should_dispatch_via_plugin`` contract:

* Plugin-gated (OpenCode runtime + ``opencode_mode="plugin"`` explicitly) →
  emits a ``_subagents`` envelope for the bridge plugin to consume.
* Non-plugin (``opencode_mode="subprocess"``, unset/None, or non-OpenCode
  runtime) → falls back to inline concatenation of persona prompts so the
  caller gets a useful text response instead of a dead envelope.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler


@pytest.mark.asyncio
async def test_multi_persona_plugin_mode_emits_subagents_envelope() -> None:
    """Plugin mode → the ``_subagents`` envelope is produced for the bridge."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # Envelope is present on meta and as JSON text.
    assert "_subagents" in payload.meta
    assert len(payload.meta["_subagents"]) == 2
    text = payload.content[0].text
    decoded = json.loads(text)
    assert "_subagents" in decoded
    assert len(decoded["_subagents"]) == 2


@pytest.mark.asyncio
async def test_multi_persona_subprocess_mode_falls_back_inline() -> None:
    """Subprocess mode → no envelope, inline concatenated prompt text."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # No envelope in the subprocess fallback path.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "inline_fallback"
    assert payload.meta.get("persona_count") == 2
    text = payload.content[0].text
    # Each persona section is separated by the canonical delimiter.
    assert text.count("\n\n---\n\n") == 1
    assert "Lateral Thinking" in text


@pytest.mark.asyncio
async def test_multi_persona_non_opencode_runtime_falls_back_inline() -> None:
    """Non-OpenCode runtime → inline fallback regardless of ``opencode_mode``."""
    handler = LateralThinkHandler(
        agent_runtime_backend="claude_code",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "all",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "inline_fallback"
    # persona='all' expands to every ThinkingPersona (5).
    assert payload.meta.get("persona_count") == 5


@pytest.mark.asyncio
async def test_single_persona_path_unchanged() -> None:
    """Single-persona (default) path does not touch the dispatch gate."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "contrarian",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    # Single-persona path returns inline text unconditionally.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("persona") == "contrarian"
