"""``--model`` / ``--pin-models`` reach the model resolver for one invocation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.main import app
from ouroboros.cli.model_options import model_options
from ouroboros.config.model_selection import pin_models_enabled, resolve_role_model

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.dump({"orchestrator": {"runtime_backend": "claude"}, "llm": {"backend": "claude"}})
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("ouroboros.config.loader.get_config_dir", lambda: config_dir)
    monkeypatch.delenv("OUROBOROS_MODEL", raising=False)
    monkeypatch.delenv("OUROBOROS_PIN_MODELS", raising=False)
    return config_dir


def _observe() -> dict[str, Any]:
    execute = resolve_role_model("execute", backend="claude")
    evaluation = resolve_role_model("semantic_evaluation", backend="claude")
    return {
        "execute": execute.model,
        "evaluation": evaluation.model,
        "pinned": pin_models_enabled(),
    }


def test_model_options_scope_restores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_PIN_MODELS", "1")
    with model_options("frugal", False):
        assert os.environ["OUROBOROS_MODEL"] == "frugal"
        assert os.environ["OUROBOROS_PIN_MODELS"] == "0"
    assert "OUROBOROS_MODEL" not in os.environ
    assert os.environ["OUROBOROS_PIN_MODELS"] == "1"


def test_model_options_without_flags_change_nothing() -> None:
    with model_options(None, None):
        assert "OUROBOROS_MODEL" not in os.environ
        assert "OUROBOROS_PIN_MODELS" not in os.environ


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], {"execute": "sonnet", "evaluation": "opus", "pinned": False}),
        (["--model", "frontier"], {"execute": "opus", "evaluation": "opus", "pinned": False}),
        (
            ["--model", "claude-opus-5"],
            {"execute": "claude-opus-5", "evaluation": "claude-opus-5", "pinned": False},
        ),
        (["--pin-models"], {"execute": "sonnet", "evaluation": "opus", "pinned": True}),
    ],
)
def test_run_flags_reach_the_resolver(
    tmp_path: Path, args: list[str], expected: dict[str, Any]
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("goal: g\n")
    observed: dict[str, Any] = {}

    async def fake_run_orchestrator(*_args: Any, **_kwargs: Any) -> None:
        observed.update(_observe())

    with patch("ouroboros.cli.commands.run._run_orchestrator", fake_run_orchestrator):
        result = runner.invoke(app, ["run", "workflow", str(seed), *args])

    assert result.exit_code == 0, result.output
    assert observed == expected
    assert "OUROBOROS_MODEL" not in os.environ
    assert "OUROBOROS_PIN_MODELS" not in os.environ


def test_interview_flags_reach_the_resolver() -> None:
    observed: dict[str, Any] = {}

    async def fake_run_interview(*_args: Any, **_kwargs: Any) -> None:
        observed.update(_observe())

    with (
        patch("ouroboros.cli.commands.init._run_interview", fake_run_interview),
        patch("ouroboros.cli.commands.init._find_pm_seeds", return_value=[]),
    ):
        result = runner.invoke(
            app, ["interview", "start", "--model", "frugal", "--pin-models", "Build a CLI"]
        )

    assert result.exit_code == 0, result.output
    assert observed == {"execute": "haiku", "evaluation": "haiku", "pinned": True}
    assert "OUROBOROS_MODEL" not in os.environ


def test_pm_without_model_keeps_a_configured_litellm_model(isolated_config: Path) -> None:
    """No flag means no invocation choice: explicit backends keep saved ids."""
    (isolated_config / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {"runtime_backend": "claude"},
                "llm": {"backend": "litellm"},
                "clarification": {"default_model": "openrouter/openai/gpt-4.1"},
            }
        )
    )
    with (
        patch("ouroboros.cli.commands.pm.resolve_llm_backend", return_value="litellm"),
        patch("ouroboros.cli.commands.pm._run_pm_interview") as run_pm_interview,
    ):
        result = runner.invoke(app, ["pm"], input="n\n")

    assert result.exit_code == 0, result.output
    assert run_pm_interview.call_args.kwargs["model"] == "openrouter/openai/gpt-4.1"
