"""Tests for the PM CLI command."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.main import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("configured_backend", "resolved_backend"),
    [
        ("codex", "codex"),
        ("claude_code", "claude_code"),
        ("litellm", "litellm"),
    ],
)
def test_pm_uses_configured_clarification_model_when_option_omitted(
    configured_backend: str,
    resolved_backend: str,
) -> None:
    """The bare `pm` command should resolve its model from config."""
    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value=configured_backend),
        patch(
            "ouroboros.cli.commands.pm.resolve_llm_backend",
            return_value=resolved_backend,
        ),
        patch(
            "ouroboros.cli.commands.pm.get_clarification_model",
            return_value="default",
        ) as mock_get_clarification_model,
        patch("ouroboros.cli.commands.pm._run_pm_interview") as mock_run_pm_interview,
    ):
        result = runner.invoke(app, ["pm"], input="n\n")

    assert result.exit_code == 0
    mock_get_clarification_model.assert_called_once_with(resolved_backend)
    mock_run_pm_interview.assert_called_once()
    assert mock_run_pm_interview.call_args.kwargs["model"] == "default"
    assert "Model:" in result.output
    assert "default" in result.output


@patch("ouroboros.cli.commands.pm._run_pm_interview")
@patch("ouroboros.cli.commands.pm.get_clarification_model")
def test_pm_preserves_explicit_model_override(
    mock_get_clarification_model, mock_run_pm_interview
) -> None:
    """An explicit --model value should bypass config lookup."""
    result = runner.invoke(app, ["pm", "--model", "openai/gpt-5.2"], input="n\n")

    assert result.exit_code == 0
    mock_get_clarification_model.assert_not_called()
    mock_run_pm_interview.assert_called_once()
    assert mock_run_pm_interview.call_args.kwargs["model"] == "openai/gpt-5.2"
