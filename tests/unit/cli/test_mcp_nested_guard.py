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
from ouroboros.package_profiles import UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE

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


def test_serve_defaults_to_port_8080_when_port_omitted(monkeypatch):
    """mcp serve should pass port 8080 when --port is omitted."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    mock_run_mcp_server = AsyncMock()

    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=mock_run_mcp_server,
    ):
        result = runner.invoke(app, ["serve", "--transport", "streamable-http"])

    assert result.exit_code == 0
    mock_run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "streamable-http",
        None,
        None,
        None,
    )


def test_public_claude_cli_runtime_selects_cli_worker(monkeypatch):
    """The explicit `claude-cli` name selects the worker inside MCP 2."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    mock_run_mcp_server = AsyncMock()
    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=mock_run_mcp_server,
    ):
        result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])

    assert result.exit_code == 0
    mock_run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "stdio",
        None,
        "claude_mcp",
        None,
    )


def test_public_claude_sdk_runtime_fails_before_process_state(monkeypatch):
    """The MCP 2 server cannot select the in-process SDK runtime."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    result = runner.invoke(app, ["serve", "--runtime", "claude"])

    assert result.exit_code == 1
    for profile in ("ouroboros-ai[mcp]", "ouroboros-ai[claude]", "[claude-sdk]", "[claude-cli]"):
        assert profile in result.output
    assert " ".join(result.output.split()) == " ".join(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE.split())
    assert "_OUROBOROS_NESTED" not in os.environ


def test_public_claude_sdk_alias_reaches_canonical_mcp2_guard(monkeypatch):
    """The shipped SDK alias parses before the MCP 2 boundary rejects it."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    result = runner.invoke(app, ["serve", "--runtime", "claude-sdk"])

    assert result.exit_code == 1
    assert "Invalid value" not in result.output
    assert " ".join(result.output.split()) == " ".join(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE.split())
    assert "_OUROBOROS_NESTED" not in os.environ


def test_forced_sdk_mcp_mix_fails_before_process_state(monkeypatch):
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    with patch(
        "ouroboros.cli.commands.mcp.has_unsupported_claude_sdk_mcp_mix",
        return_value=True,
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    for profile in ("ouroboros-ai[mcp]", "ouroboros-ai[claude]", "[claude-sdk]", "[claude-cli]"):
        assert profile in result.output
    assert "_OUROBOROS_NESTED" not in os.environ
