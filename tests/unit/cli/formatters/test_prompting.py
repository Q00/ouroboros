"""Tests for interactive prompt helpers."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from ouroboros.cli.formatters.prompting import multiline_prompt_async


@pytest.mark.asyncio
async def test_multiline_prompt_async_patches_stdout_and_stderr() -> None:
    """The shared prompt helper should proxy both stdout and stderr during input."""

    def fake_read() -> str:
        assert sys.stdout is not sys.__stdout__
        assert sys.stderr is not sys.__stderr__
        return "line 1\nline 2"

    with (
        patch(
            "ouroboros.cli.formatters.prompting._read_multiline_from_stdin", side_effect=fake_read
        ),
        patch("ouroboros.cli.formatters.prompting.console.print") as print_mock,
    ):
        result = await multiline_prompt_async("Prompt here")

    assert result == "line 1\nline 2"
    print_mock.assert_called_once()
    assert "Ctrl+D" in print_mock.call_args.args[0]
