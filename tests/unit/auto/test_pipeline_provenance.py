"""Provenance reporting on AutoPipelineResult and CLI rendering."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.provenance import PROVENANCE_ENV_VAR
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore, redact_provenance
from ouroboros.cli.main import app

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _state_with_provenance(provenance: dict | None) -> AutoPipelineState:
    state = AutoPipelineState(goal="ship a feature", cwd="/tmp/proj")
    if provenance is not None:
        state.provenance = redact_provenance(provenance)
    return state


@pytest.mark.parametrize(
    "provenance, expected",
    [
        (None, "direct"),
        ({"source": "cli"}, "direct"),
        ({"source": "discord", "rewrite": True}, "gateway"),
        ({"source": "hermes"}, "gateway"),
        ({"rewrite": True}, "unknown"),  # source missing
    ],
)
def test_invoked_by_classification(provenance, expected) -> None:
    state = _state_with_provenance(provenance)
    assert state.invoked_by() == expected


def test_pipeline_result_carries_provenance_and_redacts_unknown() -> None:
    pipeline = AutoPipeline(
        interview_driver=None,  # type: ignore[arg-type]
        seed_generator=lambda _goal: None,  # type: ignore[arg-type]
    )
    state = _state_with_provenance({"source": "discord", "rewrite": True, "user_token": "leak-me"})
    ledger = SeedDraftLedger.from_goal(state.goal)

    result = pipeline._result(state, ledger)

    assert isinstance(result, AutoPipelineResult)
    assert result.invoked_by == "gateway"
    assert result.provenance == {"source": "discord", "rewrite": True}
    assert "user_token" not in (result.provenance or {})


def test_pipeline_result_direct_when_no_provenance() -> None:
    pipeline = AutoPipeline(
        interview_driver=None,  # type: ignore[arg-type]
        seed_generator=lambda _goal: None,  # type: ignore[arg-type]
    )
    state = _state_with_provenance(None)
    ledger = SeedDraftLedger.from_goal(state.goal)

    result = pipeline._result(state, ledger)

    assert result.invoked_by == "direct"
    assert result.provenance is None


def test_cli_renders_invoked_by_for_gateway_runs(monkeypatch) -> None:
    captured: dict = {}
    payload = {"source": "discord", "rewrite": True, "channel_id_hash": "abcdef"}

    async def fake_run(state):
        captured["provenance"] = dict(state.provenance) if state.provenance else None
        captured["invoked_by"] = state.invoked_by()
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            invoked_by=state.invoked_by(),
            provenance=dict(state.provenance) if state.provenance else None,
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline") as pipeline_cls:
        pipeline_cls.return_value.run = fake_run
        monkeypatch.setenv(PROVENANCE_ENV_VAR, json.dumps(payload))
        result = runner.invoke(app, ["auto", "ship a feature", "--skip-run"])

    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "Invoked by:" in out
    assert "gateway" in out
    assert "source=discord" in out
    # Sensitive fields must never reach CLI output.
    assert "channel_id_hash" not in out
    assert "abcdef" not in out


def test_cli_escapes_rich_markup_in_source(monkeypatch) -> None:
    """A crafted source that looks like Rich markup must render as literal text."""

    # Printable, no whitespace -- passes allowlist validators but would otherwise
    # be parsed by rich.console.Console.print and visually hijack the output.
    async def fake_run(state):
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            invoked_by=state.invoked_by(),
            provenance=dict(state.provenance) if state.provenance else None,
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline") as pipeline_cls:
        pipeline_cls.return_value.run = fake_run
        monkeypatch.setenv(PROVENANCE_ENV_VAR, json.dumps({"source": "[bold]X[/]"}))
        result = runner.invoke(app, ["auto", "ship a feature", "--skip-run"])

    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    # Literal characters survive escape; the markup must not be interpreted away.
    assert "[bold]X[/]" in out


def test_cli_omits_invoked_by_for_direct_runs(monkeypatch) -> None:
    async def fake_run(state):
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline") as pipeline_cls:
        pipeline_cls.return_value.run = fake_run
        monkeypatch.delenv(PROVENANCE_ENV_VAR, raising=False)
        result = runner.invoke(app, ["auto", "ship a feature", "--skip-run"])

    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "Invoked by:" not in out


def test_cli_status_renders_invoked_by(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _state_with_provenance({"source": "hermes", "rewrite": True})
    state.transition(AutoPhase.INTERVIEW, "starting")
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore", return_value=store):
        result = runner.invoke(app, ["auto", "--status", "--resume", state.auto_session_id])

    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "Invoked by:" in out
    assert "gateway" in out
    assert "source=hermes" in out
