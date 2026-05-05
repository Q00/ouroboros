"""Unit tests for init command backend forwarding behavior."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewState
from ouroboros.cli.commands.init import (
    SeedGenerationResult,
    _generate_seed_from_interview,
    _get_adapter,
    _resolve_init_llm_backend,
    _start_workflow,
)
from ouroboros.cli.main import app
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result

runner = CliRunner()


class TestInitWorkflowRuntimeHandoff:
    """Tests for workflow and LLM backend forwarding from init."""

    @pytest.mark.asyncio
    async def test_start_workflow_forwards_runtime_backend(self) -> None:
        """Workflow handoff forwards the selected runtime backend."""
        mock_run_orchestrator = AsyncMock()

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=mock_run_orchestrator,
        ):
            await _start_workflow(
                Path("/tmp/generated-seed.yaml"),
                use_orchestrator=True,
                runtime_backend="codex",
            )

        mock_run_orchestrator.assert_awaited_once()
        assert mock_run_orchestrator.await_args.kwargs["runtime_backend"] == "codex"

    def test_cli_forwards_llm_backend_to_interview_flow(self) -> None:
        """CLI wiring forwards the explicit LLM backend into the interview coroutine."""
        mock_run_interview = AsyncMock()

        with patch("ouroboros.cli.commands.init._run_interview", new=mock_run_interview):
            result = runner.invoke(
                app,
                [
                    "init",
                    "start",
                    "Build a REST API",
                    "--orchestrator",
                    "--runtime",
                    "codex",
                    "--llm-backend",
                    "codex",
                ],
            )

        assert result.exit_code == 0
        assert mock_run_interview.await_args.args[6] == "codex"
        assert mock_run_interview.await_args.args[5] == "codex"

    def test_get_adapter_respects_configured_llm_backend_without_flags(self) -> None:
        """init start without flags uses llm.backend config instead of forcing LiteLLM."""
        mock_adapter = MagicMock()

        with (
            patch("ouroboros.cli.commands.init.get_llm_backend", return_value="claude"),
            patch(
                "ouroboros.cli.commands.init.create_llm_adapter",
                return_value=mock_adapter,
            ) as mock_create_adapter,
        ):
            adapter = _get_adapter(use_orchestrator=False, for_interview=True)

        assert adapter is mock_adapter
        assert mock_create_adapter.call_args.kwargs["backend"] == "claude"
        assert mock_create_adapter.call_args.kwargs["use_case"] == "interview"

    def test_orchestrator_flag_still_defaults_to_claude_code(self) -> None:
        """--orchestrator keeps its compatibility default independent of config."""
        with patch("ouroboros.cli.commands.init.get_llm_backend", return_value="litellm"):
            assert _resolve_init_llm_backend(use_orchestrator=True) == "claude_code"

    def test_explicit_llm_backend_overrides_config_and_orchestrator(self) -> None:
        """--llm-backend remains the highest-priority backend selection."""
        with patch("ouroboros.cli.commands.init.get_llm_backend", return_value="claude"):
            assert _resolve_init_llm_backend(use_orchestrator=True, backend="codex") == "codex"

    def test_get_adapter_uses_interview_use_case_for_codex(self) -> None:
        """Interview adapter creation stays backend-neutral for Codex."""
        mock_adapter = MagicMock()

        with patch(
            "ouroboros.cli.commands.init.create_llm_adapter",
            return_value=mock_adapter,
        ) as mock_create_adapter:
            adapter = _get_adapter(
                use_orchestrator=True,
                backend="codex",
                for_interview=True,
                debug=True,
            )

        assert adapter is mock_adapter
        assert mock_create_adapter.call_args.kwargs["backend"] == "codex"
        assert mock_create_adapter.call_args.kwargs["use_case"] == "interview"
        assert mock_create_adapter.call_args.kwargs["max_turns"] == 5

    def test_cli_reports_configured_claude_backend_without_orchestrator_flag(self) -> None:
        """CLI UX no longer claims LiteLLM when config selects Claude."""
        mock_run_interview = AsyncMock()

        with (
            patch("ouroboros.cli.commands.init.get_llm_backend", return_value="claude"),
            patch("ouroboros.cli.commands.init._run_interview", new=mock_run_interview),
        ):
            result = runner.invoke(app, ["init", "start", "Build a REST API"])

        assert result.exit_code == 0
        assert "Using Claude Code" in result.output
        assert "Using LiteLLM" not in result.output
        assert mock_run_interview.await_args.args[6] is None

    def test_get_adapter_uses_interview_use_case_for_opencode(self) -> None:
        """Interview adapter creation stays backend-neutral for OpenCode."""
        mock_adapter = MagicMock()

        with patch(
            "ouroboros.cli.commands.init.create_llm_adapter",
            return_value=mock_adapter,
        ) as mock_create_adapter:
            adapter = _get_adapter(
                use_orchestrator=True,
                backend="opencode",
                for_interview=True,
                debug=False,
            )

        assert adapter is mock_adapter
        assert mock_create_adapter.call_args.kwargs["backend"] == "opencode"
        assert mock_create_adapter.call_args.kwargs["use_case"] == "interview"
        assert mock_create_adapter.call_args.kwargs["max_turns"] == 5

    @pytest.mark.asyncio
    async def test_seed_generation_prints_provider_diagnostics(self) -> None:
        """Seed failures should show ProviderError details, not just the terse message."""
        state = InterviewState(
            interview_id="interview_595",
            initial_context="Build a CLI",
        )
        llm_adapter = MagicMock()
        ambiguity_score = AmbiguityScore(
            overall_score=0.1,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal",
                    clarity_score=0.9,
                    weight=0.4,
                    justification="clear",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraints",
                    clarity_score=0.9,
                    weight=0.3,
                    justification="clear",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success",
                    clarity_score=0.9,
                    weight=0.3,
                    justification="clear",
                ),
            ),
        )
        provider_error = ProviderError(
            message="Claude Agent SDK request failed: Command failed with exit code 1",
            details={
                "error_type": "ProcessError",
                "configured_cli_path": "/Applications/cmux.app/Contents/Resources/bin/claude",
                "stderr": "",
            },
        )

        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(ambiguity_score))
        mock_generator = MagicMock()
        mock_generator.generate = AsyncMock(return_value=Result.err(provider_error))

        with (
            patch("ouroboros.cli.commands.init.AmbiguityScorer", return_value=mock_scorer),
            patch("ouroboros.cli.commands.init.SeedGenerator", return_value=mock_generator),
            patch("ouroboros.cli.commands.init.print_error") as mock_print_error,
        ):
            seed_path, result = await _generate_seed_from_interview(state, llm_adapter)

        assert seed_path is None
        assert result == SeedGenerationResult.CANCELLED
        error_text = mock_print_error.call_args.args[0]
        assert "configured_cli_path" in error_text
        assert "/Applications/cmux.app/Contents/Resources/bin/claude" in error_text
