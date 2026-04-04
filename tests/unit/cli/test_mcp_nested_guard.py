"""Tests for _OUROBOROS_NESTED sentinel guard.

Ensures that:
1. When _OUROBOROS_NESTED=1 is set, the serve command exits with code 0 immediately
2. When _OUROBOROS_NESTED is not set, serve() sets it to "1" in os.environ before
   starting the MCP server
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from ouroboros.cli.commands.mcp import app

runner = CliRunner()


def test_nested_guard_exits_cleanly(monkeypatch):
    """Nested ouroboros MCP server should exit with code 0."""
    monkeypatch.setenv("_OUROBOROS_NESTED", "1")
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0


def test_serve_sets_nested_env_var(monkeypatch):
    """serve() should set _OUROBOROS_NESTED=1 for child processes.

    We need to:
    1. Ensure _OUROBOROS_NESTED is not set initially
    2. Mock asyncio.run to prevent actually starting a server
    3. Verify that _OUROBOROS_NESTED was set to "1" before asyncio.run was called
    """
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    # Patch asyncio.run to capture os.environ state when it's called
    captured_env = {}

    async def mock_run_mcp_server(*args, **kwargs):
        # Capture the environment at the time the coroutine is actually awaited
        captured_env["_OUROBOROS_NESTED"] = os.environ.get("_OUROBOROS_NESTED")

    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=AsyncMock(side_effect=mock_run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    # Should exit cleanly (no exception)
    assert result.exit_code == 0

    # _OUROBOROS_NESTED should have been set to "1" before asyncio.run was called
    assert captured_env.get("_OUROBOROS_NESTED") == "1"
