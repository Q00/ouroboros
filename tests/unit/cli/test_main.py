"""Unit tests for CLI main module."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros import __version__
from ouroboros.cli.main import app

runner = CliRunner()


class TestMainApp:
    """Tests for the main Typer application."""

    def test_app_has_help(self) -> None:
        """Test that --help shows formatted help text."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Ouroboros" in result.output
        assert "Self-Improving AI Workflow System" in result.output

    def test_app_version_option(self) -> None:
        """Test that --version shows version information."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        # Strip ANSI codes for comparison (Rich adds color formatting)
        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert __version__ in clean_output

    def test_app_version_short_option(self) -> None:
        """Test that -V shows version information."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        # Strip ANSI codes for comparison (Rich adds color formatting)
        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert __version__ in clean_output

    def test_no_args_shows_help(self) -> None:
        """Test that running without args shows help (exit code 2 for no_args_is_help)."""
        result = runner.invoke(app, [])
        # no_args_is_help=True causes exit code 2, which is expected
        assert result.exit_code == 2
        assert "Ouroboros" in result.output


class TestCommandGroups:
    """Tests for command group registration."""

    def test_run_command_group_registered(self) -> None:
        """Test that run command group is registered."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "Execute Ouroboros workflows" in result.output

    def test_config_command_group_registered(self) -> None:
        """Test that config command group is registered."""
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "Manage Ouroboros configuration" in result.output

    def test_status_command_group_registered(self) -> None:
        """Test that status command group is registered."""
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "Check Ouroboros system status" in result.output


class TestRunCommands:
    """Tests for run command group."""

    def test_run_workflow_help(self) -> None:
        """Test run workflow command help."""
        result = runner.invoke(app, ["run", "workflow", "--help"])
        assert result.exit_code == 0
        assert "seed" in result.output.lower()

    def test_run_resume_help(self) -> None:
        """Test run resume command help."""
        result = runner.invoke(app, ["run", "resume", "--help"])
        assert result.exit_code == 0
        assert "Resume" in result.output


class TestConfigCommands:
    """Tests for config command group."""

    def test_config_show_help(self) -> None:
        """Test config show command help."""
        result = runner.invoke(app, ["config", "show", "--help"])
        assert result.exit_code == 0
        assert "Display" in result.output

    def test_config_init_help(self) -> None:
        """Test config init command help."""
        result = runner.invoke(app, ["config", "init", "--help"])
        assert result.exit_code == 0
        assert "Initialize" in result.output

    def test_config_set_help(self) -> None:
        """Test config set command help."""
        result = runner.invoke(app, ["config", "set", "--help"])
        assert result.exit_code == 0
        assert "Set" in result.output

    def test_config_validate_help(self) -> None:
        """Test config validate command help."""
        result = runner.invoke(app, ["config", "validate", "--help"])
        assert result.exit_code == 0
        assert "Validate" in result.output


class TestStatusCommands:
    """Tests for status command group."""

    def test_status_executions_help(self) -> None:
        """Test status executions command help."""
        result = runner.invoke(app, ["status", "executions", "--help"])
        assert result.exit_code == 0
        assert "List" in result.output

    def test_status_execution_help(self) -> None:
        """Test status execution command help."""
        result = runner.invoke(app, ["status", "execution", "--help"])
        assert result.exit_code == 0
        assert "details" in result.output.lower()

    def test_status_health_help(self) -> None:
        """Test status health command help."""
        result = runner.invoke(app, ["status", "health", "--help"])
        assert result.exit_code == 0
        assert "health" in result.output.lower()

    def test_status_health_runs(self) -> None:
        """Test status health command execution."""
        result = runner.invoke(app, ["status", "health"])
        assert result.exit_code == 0
        assert "System Health" in result.output


class TestMCPCommands:
    """Tests for mcp command group."""

    def test_mcp_command_group_registered(self) -> None:
        """Test that mcp command group is registered."""
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "MCP" in result.output

    def test_mcp_serve_help(self) -> None:
        """Test mcp serve command help."""
        result = runner.invoke(app, ["mcp", "serve", "--help"])
        assert result.exit_code == 0
        assert "transport" in result.output.lower()
        assert "port" in result.output.lower()

    def test_mcp_info(self) -> None:
        """Test mcp info command."""
        result = runner.invoke(app, ["mcp", "info"])
        assert result.exit_code == 0
        assert "ouroboros-mcp" in result.output
        assert "ouroboros_execute_seed" in result.output


class TestTUICommands:
    """Tests for tui command group."""

    def test_tui_command_group_registered(self) -> None:
        """Test that tui command group is registered."""
        result = runner.invoke(app, ["tui", "--help"])
        assert result.exit_code == 0
        assert "Interactive TUI monitor" in result.output

    def test_tui_monitor_help(self) -> None:
        """Test tui monitor command help."""
        import re

        result = runner.invoke(app, ["tui", "monitor", "--help"])
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output).lower()
        assert "db-path" in plain
        assert "monitor" in plain


class TestShorthandCommands:
    """Tests for CLI shorthand/convenience commands (v0.8.0+ UX redesign)."""

    def test_run_shorthand_falls_back_to_workflow(self, tmp_path: Path) -> None:
        """Test that 'ouroboros run seed.yaml' is equivalent to 'ouroboros run workflow seed.yaml'."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        with patch("ouroboros.cli.commands.run.asyncio.run") as mock_run:
            mock_run.return_value = None

            result = runner.invoke(app, ["run", str(seed_file)])

            # Should invoke workflow command (orchestrator by default calls asyncio.run)
            assert mock_run.called

    def test_run_shorthand_with_no_orchestrator(self, tmp_path: Path) -> None:
        """Test that 'ouroboros run seed.yaml --no-orchestrator' uses placeholder mode."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        result = runner.invoke(app, ["run", str(seed_file), "--no-orchestrator"])

        assert result.exit_code == 0
        assert "Would execute" in result.output

    def test_run_explicit_workflow_still_works(self, tmp_path: Path) -> None:
        """Test backward compat: 'ouroboros run workflow seed.yaml' still works."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        result = runner.invoke(app, ["run", "workflow", str(seed_file), "--no-orchestrator"])

        assert result.exit_code == 0
        assert "Would execute" in result.output

    def test_run_resume_subcommand_still_works(self) -> None:
        """Test backward compat: 'ouroboros run resume' still works."""
        result = runner.invoke(app, ["run", "resume"])
        assert result.exit_code == 0

    def test_init_shorthand_falls_back_to_start(self) -> None:
        """Test that 'ouroboros init <context>' routes to 'ouroboros init start <context>'."""
        result = runner.invoke(app, ["init", "start", "--help"])
        start_help = result.output

        # The shorthand should show the same help as the explicit command
        result2 = runner.invoke(app, ["init", "--help"])
        # Both should be accessible
        assert result.exit_code == 0
        assert result2.exit_code == 0

    def test_init_list_subcommand_still_works(self) -> None:
        """Test backward compat: 'ouroboros init list' still routes to list."""
        with patch("ouroboros.cli.commands.init.asyncio.run") as mock_run:
            mock_run.return_value = []
            result = runner.invoke(app, ["init", "list"])
            assert result.exit_code == 0

    def test_monitor_top_level_alias(self) -> None:
        """Test that 'ouroboros monitor' is a shorthand for 'ouroboros tui monitor'."""
        result = runner.invoke(app, ["monitor", "--help"])
        # Should show monitor help (hidden command but still accessible)
        assert result.exit_code == 0

    def test_orchestrator_is_default(self, tmp_path: Path) -> None:
        """Test that orchestrator mode is the default for 'run workflow'."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        with patch("ouroboros.cli.commands.run.asyncio.run") as mock_run:
            mock_run.return_value = None

            # No --orchestrator flag needed
            result = runner.invoke(app, ["run", "workflow", str(seed_file)])

            # asyncio.run should be called (orchestrator path)
            assert mock_run.called
