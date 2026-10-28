"""Dry-run command previews preserve ordinary literal path text."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from ouroboros.cli.commands import update


def test_dry_run_preserves_bracketed_paths_and_environment() -> None:
    command = ["/tmp/tools [red]/uv", "tool", "upgrade", "ouroboros-ai"]
    overrides = {"UV_TOOL_DIR": "/tmp/tools [blue]"}
    output = StringIO()
    console = Console(file=output, width=240, color_system=None)
    with (
        patch("ouroboros.cli.formatters.panels.console", console),
        patch.object(update.subprocess, "run") as execute,
    ):
        assert update._run_step(
            command, description="Upgrade package", dry_run=True, env_overrides=overrides
        )
    execute.assert_not_called()
    rendered = output.getvalue()
    assert command[0] in rendered
    assert "UV_TOOL_DIR=" + overrides["UV_TOOL_DIR"] in rendered


def test_public_dry_run_preserves_the_selected_manager_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from typer.testing import CliRunner

    from ouroboros.cli.main import app

    identity = update.InstallationIdentity(
        manager="uv",
        tool_name="ouroboros-ai",
        environment=Path("/tmp/tools [blue]/ouroboros-ai"),
        profile="ouroboros-ai[mcp]",
        console_path=Path("/tmp/tools [blue]/ooo"),
        manager_binary="/tmp/tools [red]/uv",
        manager_home=Path("/tmp/tools [blue]"),
    )
    monkeypatch.setattr(update, "__version__", "0.59.8")
    monkeypatch.setattr(update, "_latest_pypi_version", lambda **_kwargs: "0.59.9")
    monkeypatch.setattr(update, "_detect_installation_identity", lambda: identity)
    monkeypatch.setattr(update, "configure_omp_tool_call_timeout", lambda **_kwargs: True)
    with patch.object(update.subprocess, "run") as execute:
        result = CliRunner().invoke(app, ["update", "--dry-run", "--runtime", "none"])

    assert result.exit_code == 0, result.output
    execute.assert_not_called()
    assert identity.manager_binary in result.output
    assert f"Environment: {identity.environment}" in result.output
    assert "UV_TOOL_DIR=" + str(identity.manager_home) in result.output
