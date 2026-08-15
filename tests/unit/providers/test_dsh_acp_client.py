"""Tests for the DeepSeek Harness ACP client (``dsh-acp-demo`` launcher).

The wire protocol is identical to ourocode's ACP surface, so the CI-safe fake
(``tests/fixtures/fake_ourocode_acp.py``) doubles as a fake dsh server; these
tests focus on what :class:`DshAcpClient` changes — launch argv, environment,
``session/new`` params, and error classification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.providers.dsh_acp_client import AcpClientError, DshAcpClient

_FAKE_ACP = Path(__file__).parents[2] / "fixtures" / "fake_ourocode_acp.py"


def _client(tmp_path: Path, **kwargs: object) -> DshAcpClient:
    kwargs.setdefault("config_path", tmp_path / "trusted-cordis.yml")
    return DshAcpClient(
        cli_path=_FAKE_ACP,
        cwd=tmp_path,
        startup_timeout=20.0,
        turn_timeout=20.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_spawn_argv_rejects_unset_config_instead_of_loading_from_cwd(tmp_path: Path) -> None:
    with pytest.raises(AcpClientError) as excinfo:
        _client(tmp_path, config_path=None)._spawn_argv()

    assert excinfo.value.error_type == "invalid_config"
    assert "OUROBOROS_DSH_CONFIG_PATH" in excinfo.value.message


def test_spawn_argv_forwards_config_path(tmp_path: Path) -> None:
    config = tmp_path / "cordis.yml"
    argv = _client(tmp_path, config_path=config)._spawn_argv()
    assert argv == [str(_FAKE_ACP), "--config", str(config)]


def test_spawn_argv_rejects_relative_config_path(tmp_path: Path) -> None:
    with pytest.raises(AcpClientError) as excinfo:
        _client(tmp_path, config_path="cordis.yml")._spawn_argv()

    assert excinfo.value.error_type == "invalid_config"
    assert "absolute trusted path" in excinfo.value.message


def test_session_new_params_always_include_empty_mcp_servers(tmp_path: Path) -> None:
    """dsh's ACP server indexes ``params.mcpServers.length`` unconditionally.

    Omitting the field crashes ``session/new`` inside the server, and a
    non-empty list is rejected (`mcpServers is not supported`), so the empty
    list must always be sent.
    """
    params = _client(tmp_path)._session_new_params()
    assert params["mcpServers"] == []
    assert Path(params["cwd"]).is_absolute()


def test_build_env_strips_nested_ouroboros_selectors_without_ourocode_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "claude")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "dsh")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "claude")
    monkeypatch.setenv("OUROCODE_MODEL", "codex")
    env = _client(tmp_path)._build_env()
    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env
    assert "OUROBOROS_RUNTIME" not in env
    # ourocode's env selector must not leak into a dsh process — dsh reads its
    # model from the composition file, and a stray OUROCODE_MODEL would imply
    # a selection surface that does not exist.
    assert "OUROCODE_MODEL" not in env


@pytest.mark.asyncio
async def test_run_turn_speaks_the_shared_acp_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "ok")
    result = await _client(tmp_path).run_turn("hello")
    assert result.text
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_cli_unavailable_hint_names_dsh(tmp_path: Path) -> None:
    client = DshAcpClient(
        cli_path=tmp_path / "definitely-missing-dsh-acp-demo",
        cwd=tmp_path,
        config_path=tmp_path / "trusted-cordis.yml",
    )
    with pytest.raises(AcpClientError) as excinfo:
        await client.run_turn("hi")
    assert excinfo.value.error_type == "cli_unavailable"
    assert "dsh-acp-demo" in excinfo.value.message
    assert "OUROBOROS_DSH_CLI_PATH" in excinfo.value.message
    assert "47f9438" in excinfo.value.message
    assert "published npm install path is currently broken" in excinfo.value.message
    assert "ourocode" not in excinfo.value.message


@pytest.mark.asyncio
async def test_rpc_errors_stay_generic_without_ourocode_sign_in_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dsh has no sign-in marker; even a message matching ourocode's marker
    must classify as an ordinary RPC error, not ``not_signed_in``."""
    monkeypatch.setenv("FAKE_ACP_MODE", "not_signed_in")
    with pytest.raises(AcpClientError) as excinfo:
        await _client(tmp_path).run_turn("hi")
    assert excinfo.value.error_type == "rpc_error"
    assert "dsh-acp-demo" in excinfo.value.message
