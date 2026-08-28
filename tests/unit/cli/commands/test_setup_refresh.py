"""Unit tests for `ouroboros setup refresh`.

Refresh must rewrite only artifacts a previous setup already installed, and
must never touch MCP registrations, runtime selection, or config.yaml — so an
install.sh upgrade cannot resurrect a deliberately removed integration (e.g.
OpenCode subprocess mode) or silently rewire a runtime the user never set up.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.setup import app
from ouroboros.codex import CodexArtifactInstallResult
from ouroboros.hermes.artifacts import HERMES_SKILL_CATEGORY, HERMES_SKILL_NAME
from ouroboros.runtime_instruction_artifacts import (
    _SECTION_END,
    _SECTION_START,
    GUIDE_FILENAME,
)

runner = CliRunner()


def _managed_section_text() -> str:
    return f"user text\n\n{_SECTION_START}\nold guide\n{_SECTION_END}\n"


def _invoke_refresh(tmp_path: Path):
    """Run `setup refresh` with home/config dirs redirected into tmp_path."""
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("shutil.which", return_value=None),
        patch(
            "ouroboros.cli.commands.setup.opencode_config_dir",
            return_value=tmp_path / ".config" / "opencode",
        ),
    ):
        return runner.invoke(app, ["refresh"])


class TestSetupRefreshPresenceGating:
    def test_no_artifacts_installed_refreshes_nothing(self, tmp_path: Path) -> None:
        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert "No installed runtime artifacts found to refresh." in result.output
        assert not (tmp_path / ".gemini").exists()
        assert not (tmp_path / ".kiro").exists()
        assert not (tmp_path / ".copilot").exists()
        assert not (tmp_path / ".pi").exists()

    def test_absent_bridge_is_not_resurrected(self, tmp_path: Path) -> None:
        """Subprocess-mode opencode (bridge removed) must stay bridge-free."""
        opencode_dir = tmp_path / ".config" / "opencode"
        opencode_dir.mkdir(parents=True)
        (opencode_dir / "opencode.json").write_text("{}", encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert not (opencode_dir / "plugins").exists()

    def test_instruction_file_without_managed_section_is_untouched(self, tmp_path: Path) -> None:
        gemini_md = tmp_path / ".gemini" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True)
        gemini_md.write_text("my own notes\n", encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert gemini_md.read_text(encoding="utf-8") == "my own notes\n"


class TestSetupRefreshUpdatesInstalledArtifacts:
    def test_refreshes_managed_gemini_section(self, tmp_path: Path) -> None:
        gemini_md = tmp_path / ".gemini" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True)
        gemini_md.write_text(_managed_section_text(), encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        refreshed = gemini_md.read_text(encoding="utf-8")
        assert refreshed.startswith("user text")
        assert _SECTION_START in refreshed
        assert "old guide" not in refreshed
        assert "gemini" in result.output

    def test_refreshes_existing_kiro_guide(self, tmp_path: Path) -> None:
        guide = tmp_path / ".kiro" / "steering" / GUIDE_FILENAME
        guide.parent.mkdir(parents=True)
        guide.write_text("stale\n", encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert guide.read_text(encoding="utf-8") != "stale\n"

    def test_refreshes_existing_hermes_skills(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME
        skill_dir.mkdir(parents=True)

        with patch("ouroboros.cli.commands.setup._install_hermes_artifacts") as mock_hermes:
            result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        mock_hermes.assert_called_once_with()
        assert "hermes" in result.output

    def test_dangling_hermes_target_is_reported_as_refresh_failure(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME
        skill_dir.parent.mkdir(parents=True)
        skill_dir.symlink_to(tmp_path / "missing-hermes-target", target_is_directory=True)
        with patch(
            "ouroboros.cli.commands.setup._install_hermes_artifacts", return_value=False
        ) as mock_hermes:
            result = _invoke_refresh(tmp_path)

        mock_hermes.assert_called_once_with()
        assert result.exit_code == 1
        assert "Runtime artifact refresh incomplete: hermes" in result.output

    def test_failure_only_refresh_does_not_claim_no_artifacts_found(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME
        skill_dir.mkdir(parents=True)

        with patch("ouroboros.cli.commands.setup._install_hermes_artifacts", return_value=False):
            result = _invoke_refresh(tmp_path)

        assert result.exit_code == 1
        assert "Runtime artifact refresh incomplete: hermes" in result.output
        assert "No installed runtime artifacts found" not in result.output

    def test_malformed_hermes_record_is_isolated_and_other_refreshes_continue(
        self, tmp_path: Path
    ) -> None:
        skill_dir = tmp_path / ".hermes" / "skills" / HERMES_SKILL_CATEGORY / HERMES_SKILL_NAME
        skill_dir.mkdir(parents=True)
        intent = skill_dir.parent / f".ouroboros.old.{'e' * 32}.intent"
        intent.write_bytes(b"\xff\xfe")
        pi_bridge = tmp_path / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"
        pi_bridge.parent.mkdir(parents=True)
        pi_bridge.write_text("// stale bridge\n", encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 1
        assert "Runtime artifact refresh incomplete: hermes" in result.output
        assert pi_bridge.read_text(encoding="utf-8") != "// stale bridge\n"

    def test_refreshes_existing_pi_bridge(self, tmp_path: Path) -> None:
        bridge = tmp_path / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"
        bridge.parent.mkdir(parents=True)
        bridge.write_text("// stale bridge\n", encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert bridge.read_text(encoding="utf-8") != "// stale bridge\n"

    def test_codex_refreshes_when_codex_dir_exists(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        install_result = CodexArtifactInstallResult(
            rules_path=codex_dir / "rules" / "ouroboros.md",
            skill_paths=(codex_dir / "skills" / "ouroboros-run",),
        )

        with patch(
            "ouroboros.codex.install_codex_artifacts", return_value=install_result
        ) as mock_install:
            result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        mock_install.assert_called_once_with(codex_dir=codex_dir, prune=False)
        assert "codex" in result.output

    def test_codex_refresh_preserves_raw_codex_home_for_symlink_refusal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        codex_home_link = tmp_path / ".codex"
        try:
            codex_home_link.symlink_to(real_home, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")
        monkeypatch.setenv("CODEX_HOME", str(codex_home_link))

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 1
        assert "Could not refresh Codex artifacts" in result.output
        assert "Runtime artifact refresh incomplete: codex" in result.output
        assert not (real_home / "rules").exists()

    def test_existing_opencode_bridge_false_return_is_partial_failure(self, tmp_path: Path) -> None:
        bridge = (
            tmp_path
            / ".config"
            / "opencode"
            / "plugins"
            / "ouroboros-bridge"
            / "ouroboros-bridge.ts"
        )
        bridge.parent.mkdir(parents=True)
        bridge.write_text("// stale\n", encoding="utf-8")

        with patch(
            "ouroboros.cli.commands.setup._install_opencode_bridge_plugin",
            return_value=False,
        ):
            result = _invoke_refresh(tmp_path)

        assert result.exit_code == 1
        assert "Runtime artifact refresh incomplete: opencode" in result.output
        assert "Refreshed runtime artifacts: opencode" not in result.output

    def test_instruction_write_error_is_partial_failure(self, tmp_path: Path) -> None:
        gemini_md = tmp_path / ".gemini" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True)
        gemini_md.write_text(_managed_section_text(), encoding="utf-8")

        with patch(
            "ouroboros.runtime_instruction_artifacts.install_gemini_instruction_artifact",
            side_effect=OSError("read-only filesystem"),
        ):
            result = _invoke_refresh(tmp_path)

        assert result.exit_code == 1
        assert "Could not install gemini instruction artifact" in result.output
        assert "Runtime artifact refresh incomplete: gemini" in result.output
        assert "Refreshed runtime artifacts: gemini" not in result.output


class TestSetupRefreshDoesNotTouchConfig:
    def test_never_writes_config_or_mcp_files(self, tmp_path: Path) -> None:
        gemini_md = tmp_path / ".gemini" / "GEMINI.md"
        gemini_md.parent.mkdir(parents=True)
        gemini_md.write_text(_managed_section_text(), encoding="utf-8")

        result = _invoke_refresh(tmp_path)

        assert result.exit_code == 0
        assert not (tmp_path / ".ouroboros" / "config.yaml").exists()
        assert not (tmp_path / ".claude" / "mcp.json").exists()
        assert not (tmp_path / ".codex" / "config.toml").exists()
