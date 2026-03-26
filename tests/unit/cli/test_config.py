"""Unit tests for the config command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.config import app

runner = CliRunner()


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a minimal config dir with valid config.yaml."""
    config = {
        "orchestrator": {
            "runtime_backend": "claude",
            "cli_path": "/usr/bin/claude",
        },
        "llm": {"backend": "claude"},
        "logging": {"level": "info"},
        "persistence": {"database_path": "data/ouroboros.db"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))
    return tmp_path


@pytest.fixture()
def codex_config_dir(tmp_path: Path) -> Path:
    """Create a config dir with codex backend."""
    config = {
        "orchestrator": {
            "runtime_backend": "codex",
            "codex_cli_path": "/usr/bin/codex",
        },
        "llm": {"backend": "codex"},
        "logging": {"level": "info"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))
    return tmp_path


def _patch_config_dir(config_dir: Path):
    """Patch get_config_dir to return our temp dir."""
    return patch("ouroboros.cli.commands.config._load_config", side_effect=None)


# ── config show ──────────────────────────────────────────────────


class TestConfigShow:
    """Tests for config show command."""

    def test_show_displays_summary(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "claude" in result.output

    def test_show_section(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "orchestrator"])
        assert result.exit_code == 0
        assert "runtime_backend" in result.output

    def test_show_invalid_section(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "nonexistent"])
        assert result.exit_code == 1

    def test_show_codex_cli_path(self, codex_config_dir: Path) -> None:
        """config show should display codex_cli_path for codex backend."""
        with patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "/usr/bin/codex" in result.output

    def test_show_database_path_from_persistence(self, config_dir: Path) -> None:
        """config show should use persistence.database_path when set."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "data/ouroboros.db" in result.output


# ── config backend ───────────────────────────────────────────────


class TestConfigBackend:
    """Tests for config backend command."""

    def test_show_current_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend"])
        assert result.exit_code == 0
        assert "claude" in result.output

    def test_switch_to_same_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "claude"])
        assert result.exit_code == 0
        assert "Already using" in result.output

    def test_switch_unsupported_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "opencode"])
        assert result.exit_code == 1
        assert "Unsupported" in result.output

    def test_switch_cli_not_found(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_switch_delegates_to_setup(self, config_dir: Path) -> None:
        """config backend should delegate to _setup_codex for full side effects."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("ouroboros.cli.commands.setup._setup_codex") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/codex")

    def test_switch_to_claude_delegates_to_setup(self, codex_config_dir: Path) -> None:
        """config backend claude should delegate to _setup_claude."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("ouroboros.cli.commands.setup._setup_claude") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "claude"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/claude")


# ── config validate ──────────────────────────────────────────────


class TestConfigValidate:
    """Tests for config validate command."""

    def test_valid_config(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("pathlib.Path.exists", return_value=True),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_invalid_backend_exits_nonzero(self, tmp_path: Path) -> None:
        """validate should exit 1 when backend is unsupported."""
        config = {"orchestrator": {"runtime_backend": "opencode"}, "llm": {"backend": "opencode"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "not supported" in result.output

    def test_missing_cli_path_exits_nonzero(self, tmp_path: Path) -> None:
        """validate should exit 1 when CLI path doesn't exist."""
        config = {
            "orchestrator": {
                "runtime_backend": "claude",
                "cli_path": "/nonexistent/claude",
            },
            "llm": {"backend": "claude"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "does not exist" in result.output


# ── config set ───────────────────────────────────────────────────


class TestConfigSet:
    """Tests for config set command."""

    def test_set_existing_string_value(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["set", "logging.level", "debug"])
        assert result.exit_code == 0

        # Verify the file was actually written
        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert data["logging"]["level"] == "debug"

    def test_set_new_key(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["set", "logging.new_key", "new_value"])
        assert result.exit_code == 0

        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert data["logging"]["new_key"] == "new_value"


# ── config init ──────────────────────────────────────────────────


class TestConfigInit:
    """Tests for config init command."""

    def test_init_existing_warns(self, config_dir: Path) -> None:
        with patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output
