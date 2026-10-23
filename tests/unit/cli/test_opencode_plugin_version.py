from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import ouroboros.cli.commands.setup as setup_cmd
from ouroboros.cli.opencode_config import plugin_version_error


@pytest.mark.parametrize("version", ["2.0.23", "opencode 2.0.4", "3.0.0"])
def test_public_setup_rejects_incompatible_plugin_before_writes(
    tmp_path: Path, version: str
) -> None:
    config_dir = tmp_path / "config"
    with (
        patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir) as ensure,
        patch(
            "ouroboros.cli.commands.setup._detect_runtimes",
            return_value={"opencode": "/selected/opencode"},
        ),
        patch(
            "ouroboros.cli.opencode_config.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, version + "\n", ""),
        ) as probe,
        patch("ouroboros.cli.commands.setup._install_opencode_bridge_plugin") as install,
        patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry") as mcp,
        patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry") as entry,
    ):
        result = CliRunner().invoke(
            setup_cmd.app,
            ["--runtime", "opencode", "--opencode-mode", "plugin", "--non-interactive"],
        )

    assert result.exit_code == 1, result.output
    assert "plugin mode is not supported" in result.output
    assert "--opencode-mode subprocess" in result.output
    assert "Setup complete!" not in result.output
    ensure.assert_not_called()
    install.assert_not_called()
    mcp.assert_not_called()
    entry.assert_not_called()
    probe.assert_called_once_with(
        ["/selected/opencode", "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("version", ["1.18.29", "v1.18.29", "development", ""])
def test_v1_or_unknown_version_preserves_existing_setup(version: str) -> None:
    with patch(
        "ouroboros.cli.opencode_config.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, version, ""),
    ):
        assert plugin_version_error("/selected/opencode") is None


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("opencode", 5)])
def test_unavailable_version_probe_preserves_existing_setup(error: Exception) -> None:
    with patch("ouroboros.cli.opencode_config.subprocess.run", side_effect=error):
        assert plugin_version_error("/selected/opencode") is None


def test_subprocess_mode_does_not_probe_plugin_compatibility(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
    with (
        patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        patch("ouroboros.cli.commands.setup.plugin_version_error") as probe,
        patch("ouroboros.cli.commands.setup._cleanup_plugin_artifacts"),
        patch("ouroboros.cli.commands.setup._install_runtime_instruction_artifact"),
        patch("ouroboros.cli.commands.setup.opencode_config_dir", return_value=tmp_path),
    ):
        assert setup_cmd._setup_opencode("/selected/opencode", mode="subprocess")
    probe.assert_not_called()
