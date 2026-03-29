"""Tests for the PM CLI command."""

from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.main import app

runner = CliRunner()


@patch("ouroboros.cli.commands.pm._run_pm_interview")
@patch("ouroboros.cli.commands.pm.get_clarification_model", return_value="default")
def test_pm_uses_configured_clarification_model_when_option_omitted(
    mock_get_clarification_model, mock_run_pm_interview
) -> None:
    """The bare `pm` command should resolve its model from config."""
    result = runner.invoke(app, ["pm"], input="n\n")

    assert result.exit_code == 0
    mock_get_clarification_model.assert_called_once_with()
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
