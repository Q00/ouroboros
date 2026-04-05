"""Tests for interactive prompt helpers."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.cli.formatters.prompting import multiline_prompt_async


@pytest.mark.asyncio
async def test_multiline_prompt_async_patches_stdout_and_stderr() -> None:
    """The shared prompt helper should proxy both stdout and stderr during input."""
    session = MagicMock()
    session.prompt_async = AsyncMock()

    async def fake_prompt_async() -> str:
        assert sys.stdout is not sys.__stdout__
        assert sys.stderr is not sys.__stderr__
        return "line 1\nline 2"

    session.prompt_async.side_effect = fake_prompt_async

    with (
        patch(
            "ouroboros.cli.formatters.prompting.PromptSession", return_value=session
        ) as mock_session,
        patch("ouroboros.cli.formatters.prompting.console.print"),
    ):
        result = await multiline_prompt_async("Prompt here")

    assert result == "line 1\nline 2"
    session.prompt_async.assert_awaited_once()

    kwargs = mock_session.call_args.kwargs
    assert kwargs["message"] == "> "
    assert kwargs["multiline"] is True
    assert kwargs["prompt_continuation"] == "  "

    key_bindings = kwargs["key_bindings"]
    bound_keys = {tuple(binding.keys) for binding in key_bindings.bindings}
    assert ("c-j",) in bound_keys
    assert ("c-m",) in bound_keys
