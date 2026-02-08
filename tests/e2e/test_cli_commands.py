"""End-to-end tests for CLI commands.

This module tests the complete CLI command execution including:
- ouroboros init (interview flow)
- ouroboros run workflow (workflow execution)
- Command options and error handling
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from ouroboros.cli.main import app

if TYPE_CHECKING:
    from tests.e2e.conftest import MockClaudeAgentAdapter, MockLLMProvider


runner = CliRunner()


class TestCLIBasics:
    """Test basic CLI functionality."""

    def test_help_command(self) -> None:
        """Test that --help shows correct information."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Ouroboros" in result.output
        assert "Self-Improving AI Workflow System" in result.output

    def test_version_command(self) -> None:
        """Test that --version shows version information."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "Ouroboros" in result.output

    def test_init_help(self) -> None:
        """Test that init --help shows correct information."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "interview" in result.output.lower() or "start" in result.output.lower()

    def test_run_help(self) -> None:
        """Test that run --help shows correct information."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "Execute" in result.output or "workflow" in result.output.lower()

    def test_run_workflow_help(self) -> None:
        """Test that run workflow --help shows correct information."""
        result = runner.invoke(app, ["run", "workflow", "--help"])
        assert result.exit_code == 0
        assert "seed" in result.output.lower()


class TestInitCommand:
    """Test the ouroboros init command."""

    def test_init_start_without_context_prompts(self) -> None:
        """Test that init without context enters interactive mode."""
        # When running non-interactively, typer prompts will cause the test to fail
        # This verifies the command structure is correct
        result = runner.invoke(app, ["init", "start", "--help"])
        assert result.exit_code == 0
        assert "context" in result.output.lower() or "resume" in result.output.lower()

    def test_init_with_context_argument(
        self, temp_state_dir: Path, mock_interview_llm_provider: "MockLLMProvider"
    ) -> None:
        """Test init start with context argument."""
        # Mock the LLM adapter and asyncio.run
        with patch("ouroboros.cli.commands.init.LiteLLMAdapter") as mock_adapter_class:
            mock_adapter = MagicMock()
            mock_adapter.complete = mock_interview_llm_provider.complete
            mock_adapter_class.return_value = mock_adapter

            # Mock the Prompt and Confirm classes to avoid interactive prompts
            with patch("ouroboros.cli.commands.init.Prompt") as mock_prompt:
                with patch("ouroboros.cli.commands.init.Confirm") as mock_confirm:
                    # User provides responses and confirms
                    mock_prompt.ask.return_value = "A CLI tool for developers"
                    mock_confirm.ask.return_value = False  # Don't continue

                    # Mock asyncio.run to run the coroutine
                    with patch("ouroboros.cli.commands.init.asyncio.run") as mock_run:
                        # Just verify the command structure works
                        mock_run.return_value = None

                        _result = runner.invoke(
                            app,
                            [
                                "init",
                                "start",
                                "I want to build a task manager",
                                "--state-dir",
                                str(temp_state_dir),
                            ],
                        )

                        # asyncio.run should have been called (result unused intentionally)
                        assert mock_run.called

    def test_init_list_no_interviews(self, temp_state_dir: Path) -> None:
        """Test init list when no interviews exist."""
        with patch("ouroboros.cli.commands.init.LiteLLMAdapter"):
            with patch("ouroboros.cli.commands.init.asyncio.run") as mock_run:
                mock_run.return_value = []

                result = runner.invoke(
                    app,
                    ["init", "list", "--state-dir", str(temp_state_dir)],
                )

                # Should complete successfully (shows "No interviews found" or similar)
                assert result.exit_code == 0

    def test_init_resume_missing_interview(self, temp_state_dir: Path) -> None:
        """Test init resume with non-existent interview ID."""
        with patch("ouroboros.cli.commands.init.LiteLLMAdapter") as mock_adapter_class:
            mock_adapter = MagicMock()
            mock_adapter_class.return_value = mock_adapter

            with patch("ouroboros.cli.commands.init.asyncio.run") as mock_run:
                from ouroboros.core.errors import ValidationError
                from ouroboros.core.types import Result

                # The function should raise typer.Exit on error
                import typer

                mock_run.side_effect = typer.Exit(code=1)

                result = runner.invoke(
                    app,
                    [
                        "init",
                        "start",
                        "--resume",
                        "nonexistent_interview",
                        "--state-dir",
                        str(temp_state_dir),
                    ],
                )

                assert result.exit_code == 1


class TestRunWorkflowCommand:
    """Test the ouroboros run workflow command."""

    def test_run_workflow_with_seed_file(self, temp_seed_file: Path) -> None:
        """Test run workflow with a seed file (non-orchestrator mode)."""
        result = runner.invoke(
            app,
            ["run", "workflow", str(temp_seed_file), "--no-orchestrator"],
        )

        assert result.exit_code == 0
        assert "execute workflow" in result.output.lower() or "Would execute" in result.output

    def test_run_workflow_missing_seed_file(self, temp_dir: Path) -> None:
        """Test run workflow with missing seed file."""
        nonexistent_file = temp_dir / "nonexistent.yaml"

        result = runner.invoke(
            app,
            ["run", "workflow", str(nonexistent_file)],
        )

        # Should fail due to file not existing
        assert result.exit_code != 0

    def test_run_workflow_dry_run(self, temp_seed_file: Path) -> None:
        """Test run workflow with --dry-run flag."""
        result = runner.invoke(
            app,
            ["run", "workflow", str(temp_seed_file), "--dry-run", "--no-orchestrator"],
        )

        assert result.exit_code == 0
        assert "dry run" in result.output.lower() or "Would execute" in result.output

    def test_run_workflow_verbose(self, temp_seed_file: Path) -> None:
        """Test run workflow with --verbose flag."""
        result = runner.invoke(
            app,
            ["run", "workflow", str(temp_seed_file), "--verbose"],
        )

        assert result.exit_code == 0

    def test_run_workflow_orchestrator_mode(self, temp_seed_file: Path) -> None:
        """Test run workflow with --orchestrator flag."""
        with patch("ouroboros.cli.commands.run.asyncio.run") as mock_run:
            # Mock successful execution
            mock_run.return_value = None

            _result = runner.invoke(
                app,
                ["run", "workflow", str(temp_seed_file), "--orchestrator"],
            )

            # asyncio.run should have been called for orchestrator mode
            assert mock_run.called

    def test_run_workflow_orchestrator_with_resume(self, temp_seed_file: Path) -> None:
        """Test run workflow with --orchestrator and --resume flags."""
        with patch("ouroboros.cli.commands.run.asyncio.run") as mock_run:
            mock_run.return_value = None

            _result = runner.invoke(
                app,
                [
                    "run",
                    "workflow",
                    str(temp_seed_file),
                    "--orchestrator",
                    "--resume",
                    "orch_test_123",
                ],
            )

            assert mock_run.called

    def test_run_workflow_resume_without_orchestrator_warns(
        self, temp_seed_file: Path
    ) -> None:
        """Test that --resume without --orchestrator shows warning."""
        with patch("ouroboros.cli.commands.run.asyncio.run") as mock_run:
            mock_run.return_value = None

            result = runner.invoke(
                app,
                ["run", "workflow", str(temp_seed_file), "--resume", "sess_123"],
            )

            # Should show warning about requiring orchestrator
            assert "Warning" in result.output or mock_run.called


class TestRunResumeCommand:
    """Test the ouroboros run resume command."""

    def test_run_resume_help(self) -> None:
        """Test run resume --help."""
        result = runner.invoke(app, ["run", "resume", "--help"])
        assert result.exit_code == 0
        assert "Resume" in result.output or "execution" in result.output.lower()

    def test_run_resume_without_id(self) -> None:
        """Test run resume without execution ID (uses latest)."""
        result = runner.invoke(app, ["run", "resume"])

        assert result.exit_code == 0
        assert "resume" in result.output.lower()

    def test_run_resume_with_id(self) -> None:
        """Test run resume with specific execution ID."""
        result = runner.invoke(app, ["run", "resume", "exec_12345"])

        assert result.exit_code == 0
        assert "exec_12345" in result.output or "resume" in result.output.lower()


class TestConfigCommands:
    """Test the ouroboros config commands."""

    def test_config_show_help(self) -> None:
        """Test config show --help."""
        result = runner.invoke(app, ["config", "show", "--help"])
        assert result.exit_code == 0
        assert "Display" in result.output or "show" in result.output.lower()

    def test_config_init_help(self) -> None:
        """Test config init --help."""
        result = runner.invoke(app, ["config", "init", "--help"])
        assert result.exit_code == 0
        assert "Initialize" in result.output or "init" in result.output.lower()

    def test_config_validate_help(self) -> None:
        """Test config validate --help."""
        result = runner.invoke(app, ["config", "validate", "--help"])
        assert result.exit_code == 0
        assert "Validate" in result.output or "validate" in result.output.lower()


class TestStatusCommands:
    """Test the ouroboros status commands."""

    def test_status_health(self) -> None:
        """Test status health command."""
        result = runner.invoke(app, ["status", "health"])
        assert result.exit_code == 0
        assert "System Health" in result.output or "health" in result.output.lower()

    def test_status_executions_help(self) -> None:
        """Test status executions --help."""
        result = runner.invoke(app, ["status", "executions", "--help"])
        assert result.exit_code == 0
        assert "List" in result.output or "execution" in result.output.lower()

    def test_status_execution_help(self) -> None:
        """Test status execution --help."""
        result = runner.invoke(app, ["status", "execution", "--help"])
        assert result.exit_code == 0
        assert "details" in result.output.lower() or "execution" in result.output.lower()


class TestCLIErrorHandling:
    """Test CLI error handling scenarios."""

    def test_invalid_command(self) -> None:
        """Test that invalid command shows helpful error."""
        result = runner.invoke(app, ["invalid_command"])
        assert result.exit_code != 0

    def test_missing_required_argument(self) -> None:
        """Test that missing required argument shows error."""
        result = runner.invoke(app, ["run", "workflow"])
        assert result.exit_code != 0

    def test_invalid_option(self) -> None:
        """Test that invalid option shows error."""
        result = runner.invoke(app, ["--invalid-option"])
        assert result.exit_code != 0


class TestCLIIntegrationWithWorkflow:
    """Integration tests for CLI with actual workflow components."""

    async def test_init_creates_interview_state(
        self,
        temp_state_dir: Path,
        mock_interview_llm_provider: "MockLLMProvider",
    ) -> None:
        """Test that init creates interview state file."""
        from ouroboros.bigbang.interview import InterviewEngine

        engine = InterviewEngine(
            llm_adapter=MagicMock(complete=mock_interview_llm_provider.complete),
            state_dir=temp_state_dir,
        )

        # Start interview
        result = await engine.start_interview("Build a task manager CLI")
        assert result.is_ok

        state = result.value

        # Save state
        save_result = await engine.save_state(state)
        assert save_result.is_ok

        # Verify file was created
        state_file = temp_state_dir / f"interview_{state.interview_id}.json"
        assert state_file.exists()

    async def test_workflow_creates_session_events(
        self,
        temp_db_path: str,
        sample_seed: "Any",
        mock_successful_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test that workflow execution creates session events."""
        from ouroboros.orchestrator.runner import OrchestratorRunner
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner_obj = OrchestratorRunner(
                adapter=mock_successful_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner_obj.execute_seed(sample_seed)

            assert result.is_ok
            assert result.value.success
            assert result.value.session_id.startswith("orch_")

            # Verify events were created
            events = await event_store.replay("session", result.value.session_id)
            assert len(events) > 0

            # Should have at least a session.started event
            event_types = [e.type for e in events]
            assert "orchestrator.session.started" in event_types
        finally:
            await event_store.close()
