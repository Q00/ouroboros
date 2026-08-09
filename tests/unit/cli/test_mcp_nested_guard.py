"""Tests for _OUROBOROS_NESTED sentinel guard.

Ensures that:
1. When _OUROBOROS_NESTED=1 is set, the serve command exits with code 0 immediately
2. When _OUROBOROS_NESTED is not set, serve() sets it to "1" in os.environ before
   starting the MCP server
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.mcp import _require_mcp_dependency, _run_mcp_server, app

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


def test_run_mcp_server_checks_dependency_before_startup(monkeypatch):
    """A missing MCP SDK must not initialize the server or stores."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    preflight = Mock(side_effect=ImportError("mcp package not installed"))
    shell_env = Mock()

    with (
        patch("ouroboros.cli.commands.mcp._require_mcp_dependency", preflight),
        patch("ouroboros.cli.commands.mcp._ensure_shell_env", shell_env),
    ):
        with pytest.raises(ImportError, match="mcp package not installed"):
            asyncio.run(_run_mcp_server("localhost", 8080, "stdio"))

    preflight.assert_called_once_with()
    shell_env.assert_not_called()


def test_dependency_preflight_rejects_importable_incompatible_sdk(tmp_path, monkeypatch):
    """An MCP 1.x-like package root must not satisfy the MCP v2 boundary."""
    package_dir = tmp_path / "mcp"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "server.py").write_text("class Server: pass\n", encoding="utf-8")

    for module_name in tuple(sys.modules):
        if module_name == "mcp" or module_name.startswith("mcp."):
            monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    try:
        assert importlib.import_module("mcp") is not None
        with pytest.raises(ImportError, match="MCP SDK v2 server API unavailable"):
            _require_mcp_dependency()
    finally:
        # ``monkeypatch.delitem`` restores modules that existed before this
        # test, but cannot remove synthetic modules imported afterward.
        for module_name in tuple(sys.modules):
            if module_name == "mcp" or module_name.startswith("mcp."):
                sys.modules.pop(module_name, None)
        importlib.invalidate_caches()


def test_serve_reports_missing_mcp_dependency(monkeypatch):
    """The CLI returns one actionable failure for a missing MCP SDK."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=AsyncMock(
            side_effect=ImportError(
                "MCP SDK v2 server API unavailable. "
                "Install with: pip install 'ouroboros-ai[mcp]'"
            )
        ),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    assert "MCP dependencies not installed: MCP SDK v2 server API unavailable" in result.output
    assert "Install with:" in result.output
    assert "pip install" in result.output
    assert "ouroboros-ai[mcp]" in result.output
    assert "uvx --from 'ouroboros-ai[mcp]' ouroboros mcp serve" in result.output


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
