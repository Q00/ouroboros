from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.commands import init as init_command
from ouroboros.cli.commands import run as run_command
from ouroboros.cli.commands import zcode as zcode_command
from ouroboros.cli.main import app

runner = CliRunner(env={"COLUMNS": "240"})


def test_zcode_command_is_registered() -> None:
    result = runner.invoke(app, ["zcode", "--help"])

    assert result.exit_code == 0, result.output
    assert "Convenience commands that use Zcode automatically" in result.output
    assert "qa" in result.output
    assert "run" in result.output


def test_zcode_without_arguments_shows_help() -> None:
    result = runner.invoke(app, ["zcode"])

    assert result.exit_code == 2, result.output
    assert "Usage: ouroboros zcode" in result.output
    assert "start" in result.output
    assert "Traceback" not in result.output


def test_zcode_shorthand_starts_interview_with_zcode() -> None:
    with patch("ouroboros.cli.commands.zcode.init_command.start") as mock_start:
        result = runner.invoke(app, ["zcode", "Build a small CLI"])

    assert result.exit_code == 0, result.output
    mock_start.assert_called_once()
    assert mock_start.call_args.kwargs["context"] == "Build a small CLI"
    assert mock_start.call_args.kwargs["orchestrator"] is True
    assert mock_start.call_args.kwargs["runtime"] is init_command.AgentRuntimeBackend.ZCODE
    assert mock_start.call_args.kwargs["llm_backend"] is init_command.LLMBackend.ZCODE


def test_zcode_shorthand_accepts_start_options() -> None:
    with patch("ouroboros.cli.commands.zcode.init_command.start") as mock_start:
        result = runner.invoke(app, ["zcode", "--debug", "Build a small CLI"])

    assert result.exit_code == 0, result.output
    assert mock_start.call_args.kwargs["context"] == "Build a small CLI"
    assert mock_start.call_args.kwargs["debug"] is True


def test_zcode_shorthand_accepts_start_options_without_context() -> None:
    with patch("ouroboros.cli.commands.zcode.init_command.start") as mock_start:
        result = runner.invoke(app, ["zcode", "--debug"])

    assert result.exit_code == 0, result.output
    assert mock_start.call_args.kwargs["context"] is None
    assert mock_start.call_args.kwargs["debug"] is True


def test_zcode_start_uses_default_app_bundle_path(tmp_path: Path, monkeypatch) -> None:
    zcode_path = tmp_path / "zcode.cjs"
    zcode_path.write_text("console.log('zcode')\n")
    monkeypatch.delenv("OUROBOROS_ZCODE_CLI_PATH", raising=False)
    monkeypatch.setattr(zcode_command, "_DEFAULT_ZCODE_CLI_PATH", zcode_path)

    with patch("ouroboros.cli.commands.zcode.init_command.start"):
        result = runner.invoke(app, ["zcode", "Build a small CLI"])

    assert result.exit_code == 0, result.output
    assert os.environ["OUROBOROS_ZCODE_CLI_PATH"] == str(zcode_path)
    monkeypatch.delenv("OUROBOROS_ZCODE_CLI_PATH", raising=False)


def test_zcode_qa_sets_llm_backend_and_delegates(monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", "/tmp/zcode.cjs")

    with patch("ouroboros.cli.commands.zcode.qa_command_module.qa_command") as mock_qa:
        result = runner.invoke(
            app,
            [
                "zcode",
                "qa",
                "draft.txt",
                "--artifact-type",
                "document",
                "--quality-bar",
                "PASS if clear.",
            ],
        )

    assert result.exit_code == 0, result.output
    assert os.environ["OUROBOROS_LLM_BACKEND"] == "zcode"
    assert mock_qa.call_args.kwargs["artifact"] == "draft.txt"
    assert mock_qa.call_args.kwargs["artifact_type"] == "document"
    assert mock_qa.call_args.kwargs["quality_bar"] == "PASS if clear."


def test_zcode_run_delegates_to_workflow_with_zcode_runtime(tmp_path: Path) -> None:
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text("goal: test\n")

    with patch("ouroboros.cli.commands.zcode.run_command.workflow") as mock_workflow:
        result = runner.invoke(app, ["zcode", "run", str(seed_path), "--no-qa"])

    assert result.exit_code == 0, result.output
    mock_workflow.assert_called_once()
    assert mock_workflow.call_args.kwargs["seed_file"] == seed_path
    assert mock_workflow.call_args.kwargs["orchestrator"] is True
    assert mock_workflow.call_args.kwargs["runtime"] is run_command.AgentRuntimeBackend.ZCODE
    assert mock_workflow.call_args.kwargs["no_qa"] is True
