"""Unit tests for Codex integration helper commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.commands.codex import _check_auto_dispatch_surface, app
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


class TestCodexDoctor:
    """Tests for `ouroboros codex doctor`."""

    @staticmethod
    def _write_healthy_codex_surface(codex_dir: Path) -> None:
        rules_path = codex_dir / "rules" / "ouroboros.md"
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            "| `ooo auto ...` | `ouroboros_auto` |\n"
            "Do not emulate it with manual work. If unavailable, stop.\n",
            encoding="utf-8",
        )

        skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(
            "---\n"
            "name: auto\n"
            "mcp_tool: ouroboros_auto\n"
            "---\n"
            "Manual fallback is not allowed when the tool is unavailable.\n",
            encoding="utf-8",
        )

        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

    def test_check_auto_dispatch_surface_passes_for_healthy_install(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_reports_missing_auto_contract(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        (codex_dir / "rules").mkdir(parents=True)
        (codex_dir / "rules" / "ouroboros.md").write_text(
            "| `ooo run <seed.yaml>` | `ouroboros_execute_seed` |\n",
            encoding="utf-8",
        )
        (codex_dir / "skills" / "ouroboros-auto").mkdir(parents=True)
        (codex_dir / "skills" / "ouroboros-auto" / "SKILL.md").write_text(
            "---\nname: auto\n---\n# Auto\n",
            encoding="utf-8",
        )
        (codex_dir / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")

        failures = _check_auto_dispatch_surface(codex_dir)

        assert "Codex rules do not map `ooo auto` to `ouroboros_auto`" in failures
        assert "auto skill does not declare `mcp_tool: ouroboros_auto`" in failures
        assert "Codex config does not contain [mcp_servers.ouroboros]" in failures

    def test_doctor_command_exits_nonzero_when_dispatch_surface_is_broken(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "Codex ooo auto dispatch: BROKEN" in cli_result.output
        assert "missing Codex rules file" in cli_result.output

    def test_doctor_command_reports_ok_for_healthy_install(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 0
        assert "Codex ooo auto dispatch: OK" in cli_result.output
