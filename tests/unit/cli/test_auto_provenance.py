"""Tests for the gateway-internal provenance contract on ``ooo auto``.

Provenance is delivered exclusively through the ``OUROBOROS_AUTO_PROVENANCE_JSON``
env var; there is no user-facing CLI flag. The tests below exercise both the
helper directly and the CLI surface (env-only) to prove that direct CLI runs
are unaffected and gateway-tagged runs flow allowlisted metadata to state.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.provenance import PROVENANCE_ENV_VAR, resolve_provenance
from ouroboros.cli.main import app

runner = CliRunner()


def test_resolve_provenance_returns_none_when_no_env_present() -> None:
    assert resolve_provenance(env={}) is None


def test_resolve_provenance_defaults_env_to_os_environ(monkeypatch) -> None:
    monkeypatch.setenv(PROVENANCE_ENV_VAR, json.dumps({"source": "discord"}))
    assert resolve_provenance() == {"source": "discord"}


def test_resolve_provenance_reads_env_var() -> None:
    payload = json.dumps({"source": "discord", "rewrite": True, "raw": "drop me"})
    cleaned = resolve_provenance(env={PROVENANCE_ENV_VAR: payload})
    assert cleaned == {"source": "discord", "rewrite": True}


def test_resolve_provenance_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        resolve_provenance(env={PROVENANCE_ENV_VAR: "{not json"})


def test_resolve_provenance_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        resolve_provenance(env={PROVENANCE_ENV_VAR: "[1, 2]"})


def test_resolve_provenance_empty_env_returns_none() -> None:
    assert resolve_provenance(env={PROVENANCE_ENV_VAR: ""}) is None
    assert resolve_provenance(env={PROVENANCE_ENV_VAR: "   "}) is None


def test_resolve_provenance_drops_unknown_keys_only() -> None:
    payload = json.dumps({"only": "garbage"})
    assert resolve_provenance(env={PROVENANCE_ENV_VAR: payload}) is None


def test_resolve_provenance_rejects_oversize_env() -> None:
    big = json.dumps({"source": "x" * 5000})
    with pytest.raises(ValueError, match="exceeds 4096-byte cap"):
        resolve_provenance(env={PROVENANCE_ENV_VAR: big})


def test_auto_cli_picks_up_env_provenance(monkeypatch) -> None:
    """The gateway env var must flow through to AutoPipelineState."""
    captured: dict[str, object] = {}

    async def fake_run(state):
        captured["provenance"] = dict(state.provenance) if state.provenance else None
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline") as pipeline_cls:
        pipeline_cls.return_value.run = fake_run
        monkeypatch.setenv(
            PROVENANCE_ENV_VAR,
            json.dumps({"source": "discord", "rewrite": True, "leaked": "x"}),
        )
        result = runner.invoke(app, ["auto", "do something safe", "--skip-run"])

    assert result.exit_code == 0, result.output
    assert captured["provenance"] == {"source": "discord", "rewrite": True}


def test_auto_cli_resume_rejects_attaching_provenance_to_direct_session(
    tmp_path, monkeypatch
) -> None:
    """Re-attribution would mislead audit; resume must refuse to add provenance."""
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="ship a feature", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.transition(AutoPhase.INTERVIEW, "starting")
    state.mark_blocked("need credentials", tool_name="auto")
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore", return_value=store):
        monkeypatch.setenv(PROVENANCE_ENV_VAR, json.dumps({"source": "discord"}))
        result = runner.invoke(app, ["auto", "--resume", state.auto_session_id])

    assert result.exit_code == 1
    assert "cannot attach provenance on resume" in result.output


def test_auto_cli_without_env_keeps_state_provenance_none(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(state):
        captured["provenance"] = state.provenance
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline") as pipeline_cls:
        pipeline_cls.return_value.run = fake_run
        monkeypatch.delenv(PROVENANCE_ENV_VAR, raising=False)
        result = runner.invoke(app, ["auto", "do something safe", "--skip-run"])

    assert result.exit_code == 0, result.output
    assert captured["provenance"] is None
