"""Tests for the PM CLI command."""

import os
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


@pytest.mark.parametrize(
    ("option", "expected"),
    [("openai/gpt-5.2", "openai/gpt-5.2"), ("frontier", "opus"), ("frugal", "haiku")],
)
def test_pm_model_option_resolves_through_the_model_resolver(
    option: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--model is the per-invocation choice: an id runs as given, a tier resolves."""
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)
    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value="claude_code"),
        patch("ouroboros.cli.commands.pm.resolve_llm_backend", return_value="claude_code"),
        patch("ouroboros.cli.commands.pm._run_pm_interview") as mock_run_pm_interview,
    ):
        result = runner.invoke(app, ["pm", "--model", option], input="n\n")

    assert result.exit_code == 0
    mock_run_pm_interview.assert_called_once()
    assert mock_run_pm_interview.call_args.kwargs["model"] == expected
    assert "OUROBOROS_MODEL" not in os.environ
