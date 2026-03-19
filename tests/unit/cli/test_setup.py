"""Tests for CLI setup command — config persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def tmp_config_env(tmp_path: Path):
    """Provide isolated config dir + home for setup tests."""
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    claude_dir = home_dir / ".claude"
    claude_dir.mkdir()
    return config_dir, home_dir


def _run_setup_claude(config_dir: Path, home_dir: Path, claude_path: str):
    """Run _setup_claude with mocked paths."""
    from ouroboros.cli.commands.setup import _setup_claude

    with (
        patch(
            "ouroboros.config.loader.ensure_config_dir",
            return_value=config_dir,
        ),
        patch("ouroboros.config.loader.create_default_config"),
        patch("pathlib.Path.home", return_value=home_dir),
    ):
        _setup_claude(claude_path)


class TestSetupClaude:
    """Tests for _setup_claude() config persistence (review findings #2, #3)."""

    def test_setup_claude_persists_runtime_backend(self, tmp_config_env: tuple):
        config_dir, home_dir = tmp_config_env
        config_path = config_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))

        _run_setup_claude(config_dir, home_dir, "/usr/local/bin/claude")

        saved = yaml.safe_load(config_path.read_text())
        assert saved["orchestrator"]["runtime_backend"] == "claude"
        assert saved["llm"]["backend"] == "claude"

    def test_setup_claude_persists_claude_path(self, tmp_config_env: tuple):
        config_dir, home_dir = tmp_config_env
        config_path = config_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))

        _run_setup_claude(config_dir, home_dir, "/opt/custom/bin/claude")

        saved = yaml.safe_load(config_path.read_text())
        assert saved["orchestrator"]["cli_path"] == "/opt/custom/bin/claude"

    def test_switch_codex_to_claude_overwrites_backend(self, tmp_config_env: tuple):
        """Switching from codex to claude must rewrite runtime_backend and llm.backend."""
        config_dir, home_dir = tmp_config_env
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "orchestrator": {
                        "runtime_backend": "codex",
                        "codex_cli_path": "/usr/bin/codex",
                    },
                    "llm": {"backend": "codex"},
                }
            )
        )

        _run_setup_claude(config_dir, home_dir, "/usr/local/bin/claude")

        saved = yaml.safe_load(config_path.read_text())
        assert saved["orchestrator"]["runtime_backend"] == "claude"
        assert saved["llm"]["backend"] == "claude"
        assert saved["orchestrator"]["cli_path"] == "/usr/local/bin/claude"
