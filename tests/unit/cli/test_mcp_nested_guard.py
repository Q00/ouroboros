"""Tests for _OUROBOROS_NESTED sentinel guard.

Ensures that:
1. When _OUROBOROS_NESTED=1 is set, the serve command exits with code 0 immediately
2. When _OUROBOROS_NESTED is not set, serve() sets it to "1" in os.environ before
   starting the MCP server
"""

from __future__ import annotations

import asyncio
import importlib
from importlib import metadata as importlib_metadata
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.mcp import _require_mcp_dependency, _run_mcp_server, app
from ouroboros.package_profiles import (
    SDK_RUNTIME_IN_MCP_SERVER_MESSAGE,
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
)

runner = CliRunner()


def _set_installed_versions(monkeypatch, versions: dict[str, str]) -> None:
    """Make package-profile detection deterministic for a CLI scenario."""

    def fake_version(distribution: str) -> str:
        try:
            return versions[distribution]
        except KeyError as exc:
            raise importlib_metadata.PackageNotFoundError(distribution) from exc

    monkeypatch.setattr(
        "ouroboros.package_profiles.importlib_metadata.version",
        fake_version,
    )


def test_nested_guard_exits_cleanly(monkeypatch):
    """Nested ouroboros MCP server should exit with code 0."""
    monkeypatch.setenv("_OUROBOROS_NESTED", "1")
    result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])
    assert result.exit_code == 0


def test_nested_guard_skips_shell_hydration(monkeypatch) -> None:
    monkeypatch.setenv("_OUROBOROS_NESTED", "1")
    hydrate = Mock(side_effect=AssertionError("nested serve must not hydrate"))
    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", hydrate)

    result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])

    assert result.exit_code == 0
    hydrate.assert_not_called()


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
        result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])

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
                "MCP SDK v2 server API unavailable. Install with: pip install 'ouroboros-ai[mcp]'"
            )
        ),
    ):
        result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])

    assert result.exit_code == 1
    normalized_output = " ".join(result.output.split())
    assert "MCP dependencies not installed: MCP SDK v2 server API unavailable" in normalized_output
    assert "Install with:" in result.output
    assert "pip install" in result.output
    assert "ouroboros-ai[mcp]" in result.output
    assert (
        "uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' "
        "ouroboros mcp serve --runtime claude-cli"
    ) in normalized_output


def test_serve_defaults_to_port_8080_when_port_omitted(monkeypatch):
    """mcp serve should pass port 8080 when --port is omitted."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)

    mock_run_mcp_server = AsyncMock()

    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=mock_run_mcp_server,
    ):
        result = runner.invoke(
            app,
            ["serve", "--runtime", "claude-cli", "--transport", "streamable-http"],
        )

    assert result.exit_code == 0
    mock_run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "streamable-http",
        None,
        "claude_mcp",
        None,
        auth_token="",
        allowed_hosts=(),
        allowed_origins=(),
        workspace_roots=(),
    )


def test_public_claude_cli_runtime_selects_cli_worker(monkeypatch):
    """The explicit `claude-cli` name selects the worker inside MCP 2."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})

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
        auth_token="",
        allowed_hosts=(),
        allowed_origins=(),
        workspace_roots=(),
    )


def test_public_host_runtime_selects_host_dispatch(monkeypatch) -> None:
    """A setup-managed ``--runtime host`` launcher must parse and reach the server."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})

    mock_run_mcp_server = AsyncMock()
    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=mock_run_mcp_server,
    ):
        result = runner.invoke(app, ["serve", "--runtime", "host"])

    assert result.exit_code == 0, result.output
    mock_run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "stdio",
        None,
        "host",
        None,
        auth_token="",
        allowed_hosts=(),
        allowed_origins=(),
        workspace_roots=(),
    )


def test_public_codex_runtime_remains_executable(monkeypatch):
    """Separating the Claude SDK diagnostic must not gate other workers."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})

    mock_run_mcp_server = AsyncMock()
    with patch(
        "ouroboros.cli.commands.mcp._run_mcp_server",
        new=mock_run_mcp_server,
    ):
        result = runner.invoke(app, ["serve", "--runtime", "codex"])

    assert result.exit_code == 0
    mock_run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "stdio",
        None,
        "codex",
        None,
        auth_token="",
        allowed_hosts=(),
        allowed_origins=(),
        workspace_roots=(),
    )


def test_public_claude_sdk_runtime_fails_before_process_state(monkeypatch):
    """The MCP 2 server cannot select the in-process SDK runtime."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})

    result = runner.invoke(app, ["serve", "--runtime", "claude"])

    assert result.exit_code == 1
    # This is a runtime-selection failure, not a packaging one (#2038). The
    # message must name the flag that fixes it and must not send a user with a
    # correct install back to change extras.
    assert " ".join(result.output.split()) == " ".join(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.split())
    assert "--runtime" in result.output
    assert "claude-cli" in result.output
    assert "ouroboros-ai[mcp]" not in result.output
    assert "_OUROBOROS_NESTED" not in os.environ


def test_public_claude_sdk_alias_reaches_canonical_mcp2_guard(monkeypatch):
    """The shipped SDK alias parses before the MCP 2 boundary rejects it."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})

    result = runner.invoke(app, ["serve", "--runtime", "claude-sdk"])

    assert result.exit_code == 1
    assert "Invalid value" not in result.output
    assert " ".join(result.output.split()) == " ".join(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.split())
    assert "_OUROBOROS_NESTED" not in os.environ


@pytest.mark.parametrize(
    "versions",
    [
        pytest.param({}, id="no-mcp"),
        pytest.param(
            {"mcp": "1.29.0", "claude-agent-sdk": "0.2.123"},
            id="mcp1-claude-sdk-profile",
        ),
    ],
)
def test_sdk_runtime_diagnostic_does_not_claim_install_health(
    monkeypatch, tmp_path, versions: dict[str, str]
) -> None:
    """The early runtime diagnosis must stay true before MCP v2 preflight."""
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(monkeypatch, versions)

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = runner.invoke(app, ["serve", "--runtime", "claude"])

    assert result.exit_code == 1
    assert " ".join(result.output.split()) == " ".join(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.split())
    assert "install" not in result.output.lower()
    assert "--runtime claude-cli" in " ".join(result.output.split())
    assert "_OUROBOROS_NESTED" not in os.environ
    assert not (tmp_path / ".ouroboros").exists()


def test_mixed_sdk_mcp2_profile_uses_canonical_diagnostic(monkeypatch):
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    _set_installed_versions(
        monkeypatch,
        {"mcp": "2.0.0", "claude-agent-sdk": "0.2.123"},
    )

    result = runner.invoke(app, ["serve", "--runtime", "claude-cli"])

    assert result.exit_code == 1
    for profile in ("ouroboros-ai[mcp]", "ouroboros-ai[claude]", "[claude-sdk]", "[claude-cli]"):
        assert profile in result.output
    assert " ".join(result.output.split()) == " ".join(UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE.split())
    assert "_OUROBOROS_NESTED" not in os.environ


def _clear_runtime_selection(monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)
    monkeypatch.delenv("_OUROBOROS_NESTED", raising=False)
    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", lambda: None)
    # An inherited SDK default now looks for an installed CLI to serve with, so
    # what is on the developer's PATH would otherwise decide these outcomes.
    _installed_clis(monkeypatch)


def _installed_clis(monkeypatch, *available: str) -> None:
    """Pin which runtime CLIs the stand-in search can find."""
    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp.shutil.which",
        lambda command: f"/usr/local/bin/{command}" if command in available else None,
    )


def _assert_rejected_before_start(result, run_mcp_server: AsyncMock, home) -> None:
    """A bare ``serve`` inherits the ``claude`` default and must say so.

    Before #2038 this asserted the package-profile message, which told a user
    with a clean install to change extras that were never wrong.
    """
    assert result.exit_code == 1
    assert " ".join(result.output.split()) == " ".join(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.split())
    run_mcp_server.assert_not_awaited()
    assert "_OUROBOROS_NESTED" not in os.environ
    assert not (home / ".ouroboros" / "ouroboros.db").exists()


def test_bare_serve_rejects_missing_config_sdk_default_before_mutation(
    monkeypatch, tmp_path
) -> None:
    """A valid MCP 2 install still rejects the inherited SDK default safely."""
    _clear_runtime_selection(monkeypatch)
    _set_installed_versions(monkeypatch, {"mcp": "2.0.0"})
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)
    assert not (tmp_path / ".ouroboros").exists()


def test_bare_serve_rejects_configured_sdk_before_mutation(monkeypatch, tmp_path) -> None:
    """Persisted legacy SDK selection cannot cross the MCP 2 boundary."""
    _clear_runtime_selection(monkeypatch)
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
        encoding="utf-8",
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)
    assert sorted(path.name for path in config_dir.iterdir()) == ["config.yaml"]


def test_bare_serve_allows_configured_cli_worker(monkeypatch, tmp_path) -> None:
    """The persisted dependency-free worker is a valid effective MCP 2 runtime."""
    _clear_runtime_selection(monkeypatch)
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: claude_mcp\nllm:\n  backend: claude\n",
        encoding="utf-8",
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    run_mcp_server.assert_awaited_once_with(
        "localhost",
        8080,
        "stdio",
        None,
        "claude_mcp",
        None,
        auth_token="",
        allowed_hosts=(),
        allowed_origins=(),
        workspace_roots=(),
    )


def test_inherited_sdk_default_serves_with_an_installed_cli(monkeypatch, tmp_path) -> None:
    """A dead server is worse than a runtime nobody typed.

    The SDK-backed ``claude`` runtime cannot run in this process, so inheriting
    it from config used to mean an MCP host booted with zero Ouroboros tools and
    three lines of stderr. With a CLI installed, serve that instead and say so.
    """
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "claude", "codex")
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
        encoding="utf-8",
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert run_mcp_server.await_args.args[4] == "claude_mcp"
    # The substitution has to be visible, or the tool list silently belongs to a
    # runtime the user never picked.
    assert "claude-cli" in result.output
    assert "--runtime" in result.output


def test_stand_in_prefers_the_same_engine_out_of_process(monkeypatch, tmp_path) -> None:
    """Without the Claude CLI, the next installed runtime still beats failing."""
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "codex", "opencode")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert run_mcp_server.await_args.args[4] == "codex"


def test_explicit_sdk_runtime_still_fails(monkeypatch, tmp_path) -> None:
    """Substituting for a runtime the caller just named would be worse."""
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "claude", "codex")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve", "--runtime", "claude"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)


def test_env_selected_sdk_runtime_still_fails(monkeypatch, tmp_path) -> None:
    """The environment is an explicit choice too, so it is not overridden."""
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "claude", "codex")
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "claude")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)


def test_legacy_env_selected_sdk_runtime_still_fails(monkeypatch, tmp_path) -> None:
    """OUROBOROS_RUNTIME is an explicit selector too and must not be replaced."""
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "claude", "codex")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "claude")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)


def test_inherited_sdk_uses_configured_claude_cli_path(monkeypatch, tmp_path) -> None:
    _clear_runtime_selection(monkeypatch)
    configured_cli = tmp_path / "tools" / "claude-custom"
    configured_cli.parent.mkdir()
    configured_cli.write_text("", encoding="utf-8")
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n"
        "  runtime_backend: claude\n"
        f"  cli_path: {configured_cli}\n"
        "llm:\n  backend: claude\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp.shutil.which",
        lambda command: str(configured_cli) if command == str(configured_cli) else None,
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert run_mcp_server.await_args.args[4] == "claude_mcp"


def test_inherited_sdk_uses_login_shell_recovered_path(monkeypatch, tmp_path) -> None:
    _clear_runtime_selection(monkeypatch)
    hydrated = False

    def hydrate() -> None:
        nonlocal hydrated
        hydrated = True

    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", hydrate)
    monkeypatch.setattr(
        "ouroboros.cli.commands.mcp.shutil.which",
        lambda command: f"/login/bin/{command}" if hydrated and command == "claude" else None,
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert hydrated is True
    assert run_mcp_server.await_args.args[4] == "claude_mcp"


def test_shell_hydrated_non_sdk_selector_is_authoritative(monkeypatch, tmp_path) -> None:
    _clear_runtime_selection(monkeypatch)

    def hydrate() -> None:
        monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")

    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", hydrate)
    _installed_clis(monkeypatch, "claude", "codex")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert run_mcp_server.await_args.args[4] == "codex"
    assert "serving with 'claude-cli'" not in result.output


def test_shell_hydrated_explicit_sdk_selector_still_fails(monkeypatch, tmp_path) -> None:
    _clear_runtime_selection(monkeypatch)

    def hydrate() -> None:
        monkeypatch.setenv("OUROBOROS_RUNTIME", "claude")

    monkeypatch.setattr("ouroboros.cli.commands.mcp._ensure_shell_env", hydrate)
    _installed_clis(monkeypatch, "claude", "codex")
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)


def test_inherited_sdk_refuses_when_stage_profile_controls_backend(monkeypatch, tmp_path) -> None:
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch, "claude", "codex")
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n"
        "  runtime_backend: claude\n"
        "  runtime_profile:\n"
        "    default: claude\n"
        "llm:\n  backend: claude\n",
        encoding="utf-8",
    )
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)


def test_no_installed_cli_keeps_the_original_refusal(monkeypatch, tmp_path) -> None:
    """With nothing to serve with, the actionable error is still the answer."""
    _clear_runtime_selection(monkeypatch)
    _installed_clis(monkeypatch)
    run_mcp_server = AsyncMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("ouroboros.cli.commands.mcp._run_mcp_server", new=run_mcp_server),
    ):
        result = runner.invoke(app, ["serve"])

    _assert_rejected_before_start(result, run_mcp_server, tmp_path)
