"""Unit tests for Codex integration helper commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.commands.codex import app
from ouroboros.codex import CodexArtifactInstallResult

runner = CliRunner()


class TestCodexRefresh:
    """Tests for `ouroboros codex refresh`."""

    def test_refresh_installs_rules_and_skills_without_config_files(self, tmp_path: Path) -> None:
        rules_path = tmp_path / ".codex" / "rules" / "ouroboros.md"
        skill_paths = (
            tmp_path / ".codex" / "skills" / "ouroboros-interview",
            tmp_path / ".codex" / "skills" / "ouroboros-run",
        )
        result = CodexArtifactInstallResult(rules_path=rules_path, skill_paths=skill_paths)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.codex.install_codex_artifacts", return_value=result
            ) as mock_install,
        ):
            cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 0
        mock_install.assert_called_once_with(codex_dir=tmp_path / ".codex", prune=False)
        assert "Installed Codex rules" in cli_result.output
        assert "Installed 2 Codex skills" in cli_result.output
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".ouroboros" / "config.yaml").exists()
