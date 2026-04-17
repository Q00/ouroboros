"""Unit tests for the config command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.config import app

runner = CliRunner(env={"COLUMNS": "200"})


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
        """config show should resolve persistence.database_path under config dir."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "data/ouroboros.db" in result.output

    def test_show_database_path_default(self, tmp_path: Path) -> None:
        """Without persistence.database_path, default is <config_dir>/ouroboros.db."""
        config = {
            "orchestrator": {"runtime_backend": "claude", "cli_path": "/usr/bin/claude"},
            "llm": {"backend": "claude"},
            "logging": {"level": "info"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "ouroboros.db" in result.output


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
            result = runner.invoke(app, ["backend", "nonexistent"])
        assert result.exit_code == 1
        assert "Unsupported" in result.output

    def test_switch_opencode_not_switchable(self, config_dir: Path) -> None:
        """opencode is a valid backend but not yet switchable via config backend."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "opencode"])
        assert result.exit_code == 1
        assert "opencode" in result.output

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

    def test_switch_to_hermes_delegates_to_setup(self, config_dir: Path) -> None:
        """config backend hermes should delegate to _setup_hermes."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/hermes"),
            patch("ouroboros.cli.commands.setup._setup_hermes") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "hermes"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/hermes")

    def test_switch_warns_on_setup_print_error(self, config_dir: Path) -> None:
        """config backend should warn when setup emits print_error (non-exception failure)."""
        from ouroboros.cli.commands import setup as setup_mod

        def _failing_setup(cli_path: str) -> None:
            setup_mod.print_error("Could not locate packaged Codex rules.")

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("ouroboros.cli.commands.setup._setup_codex", side_effect=_failing_setup),
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 0
        assert "issues" in result.output or "Warning" in result.output

    def test_malformed_config_yaml(self, tmp_path: Path) -> None:
        """config commands should handle malformed YAML gracefully."""
        (tmp_path / "config.yaml").write_text(": invalid: yaml: [")

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 1
        assert "Cannot parse" in result.output

    def test_structurally_invalid_section_type(self, tmp_path: Path) -> None:
        """config commands should handle sections with wrong types (e.g. orchestrator: [])."""
        config = {"orchestrator": [], "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 1
        assert "Invalid config section" in result.output

    def test_structurally_invalid_logging_section(self, tmp_path: Path) -> None:
        """config validate should catch logging: [] instead of crashing."""
        config = {"orchestrator": {"runtime_backend": "claude"}, "logging": "bad"}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "Invalid config section" in result.output


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
        config = {"orchestrator": {"runtime_backend": "nonexistent"}, "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1

    def test_opencode_backend_is_valid(self, tmp_path: Path) -> None:
        """validate should accept opencode as a valid runtime backend."""
        config = {"orchestrator": {"runtime_backend": "opencode"}, "llm": {"backend": "opencode"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    def test_hermes_runtime_backend_is_valid(self, tmp_path: Path) -> None:
        """validate should accept hermes as a valid runtime backend."""
        config = {"orchestrator": {"runtime_backend": "hermes"}, "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

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

    def test_set_unknown_top_level_key_rejected(self, config_dir: Path) -> None:
        """config set should reject unknown top-level keys."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["set", "typo.section", "value"])
        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_set_unknown_nested_key_rejected(self, config_dir: Path) -> None:
        """config set should reject unknown nested keys like logging.levle."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["set", "logging.levle", "debug"])
        assert result.exit_code == 1
        assert "Unknown config key" in result.output


# ── config init ──────────────────────────────────────────────────


class TestConfigInit:
    """Tests for config init command."""

    def test_init_existing_shows_info(self, config_dir: Path) -> None:
        # Create both files so init considers it fully initialized
        (config_dir / "credentials.yaml").write_text("test: true")
        with patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "already initialized" in result.output

    def test_init_partial_preserves_existing_config(self, config_dir: Path) -> None:
        """config init should NOT overwrite existing config.yaml when only credentials.yaml is missing."""
        # config_dir already has config.yaml with custom settings
        original_data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert original_data["orchestrator"]["runtime_backend"] == "claude"

        # No credentials.yaml — partial init state
        with patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

        # config.yaml should be untouched
        after_data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert after_data == original_data
        # credentials.yaml should now exist
        assert (config_dir / "credentials.yaml").exists()

    def test_init_partial_creates_missing_config(self, tmp_path: Path) -> None:
        """config init should create config.yaml when only credentials.yaml exists."""
        (tmp_path / "credentials.yaml").write_text(yaml.dump({"providers": {}}))

        with patch("ouroboros.config.loader.ensure_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

        # config.yaml should now exist with defaults
        assert (tmp_path / "config.yaml").exists()
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert "orchestrator" in data
        # credentials.yaml should be untouched (still has our original content)
        cred_data = yaml.safe_load((tmp_path / "credentials.yaml").read_text())
        assert cred_data == {"providers": {}}
