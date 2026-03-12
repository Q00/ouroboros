"""Unit tests for init command backend forwarding behavior."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.init import _get_adapter, _start_workflow
from ouroboros.cli.main import app

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

        with (
            patch("ouroboros.cli.commands.init._run_interview", new=mock_run_interview),
            patch("ouroboros.cli.commands.init.asyncio.run") as mock_asyncio_run,
        ):
            mock_asyncio_run.return_value = None

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
        assert mock_run_interview.call_args.args[6] == "codex"
        assert mock_run_interview.call_args.args[5] == "codex"

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
