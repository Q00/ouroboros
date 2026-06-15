"""Tests for the ourocode ACP stdio client against a CI-safe fake server."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.providers.ourocode_acp_client import (
    AcpClientError,
    OurocodeAcpClient,
)

_FAKE_ACP = Path(__file__).parents[2] / "fixtures" / "fake_ourocode_acp.py"


def _client(tmp_path: Path) -> OurocodeAcpClient:
    return OurocodeAcpClient(
        cli_path=_FAKE_ACP,
        cwd=tmp_path,
        startup_timeout=20.0,
        turn_timeout=20.0,
    )


def test_build_env_strips_nested_ouroboros_backend_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "codex")

    env = OurocodeAcpClient(cli_path=_FAKE_ACP, cwd=tmp_path)._build_env()

    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env
    assert "OUROBOROS_RUNTIME" not in env
    assert env["OUROCODE_MODEL"] == "claude"


@pytest.mark.asyncio
async def test_run_turn_streams_chunks_and_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "ok")
    seen: list[str] = []
    result = await _client(tmp_path).run_turn("hi", on_chunk=seen.append)

    assert result.text == "Hello, world!"
    assert result.stop_reason == "end_turn"
    assert result.session_id == "sess_fake01"
    assert seen == ["Hello, ", "world!"]


@pytest.mark.asyncio
async def test_run_turn_passes_absolute_cwd(tmp_path: Path) -> None:
    # session/new requires an absolute cwd; the fake errors otherwise. A relative
    # cwd must be resolved to absolute by the client.
    client = OurocodeAcpClient(cli_path=_FAKE_ACP, cwd=".", turn_timeout=20.0)
    result = await client.run_turn("hi")
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_not_signed_in_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "not_signed_in")
    with pytest.raises(AcpClientError) as exc:
        await _client(tmp_path).run_turn("hi")
    assert exc.value.error_type == "not_signed_in"


@pytest.mark.asyncio
async def test_malformed_frame_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "malformed")
    with pytest.raises(AcpClientError) as exc:
        await _client(tmp_path).run_turn("hi")
    assert exc.value.error_type == "malformed_frame"


@pytest.mark.asyncio
async def test_missing_session_id_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "no_session_id")
    with pytest.raises(AcpClientError) as exc:
        await _client(tmp_path).run_turn("hi")
    assert exc.value.error_type == "malformed_response"


@pytest.mark.asyncio
async def test_cli_unavailable_is_classified(tmp_path: Path) -> None:
    client = OurocodeAcpClient(cli_path=tmp_path / "does-not-exist", cwd=tmp_path, turn_timeout=5.0)
    with pytest.raises(AcpClientError) as exc:
        await client.run_turn("hi")
    assert exc.value.error_type == "cli_unavailable"


@pytest.mark.asyncio
async def test_turn_timeout_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "hang")
    client = OurocodeAcpClient(
        cli_path=_FAKE_ACP, cwd=tmp_path, startup_timeout=20.0, turn_timeout=0.5
    )
    with pytest.raises(AcpClientError) as exc:
        await client.run_turn("hi")
    assert exc.value.error_type == "timeout"


@pytest.mark.asyncio
async def test_process_death_mid_turn_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ACP_MODE", "die_mid_turn")
    with pytest.raises(AcpClientError) as exc:
        await _client(tmp_path).run_turn("hi")
    assert exc.value.error_type == "process_died"


@pytest.mark.asyncio
async def test_none_turn_timeout_is_coalesced_not_unbounded(tmp_path: Path) -> None:
    """A ``None`` turn_timeout must not disable the guard (it coalesces to a
    bounded default), so a normal turn still completes."""
    client = OurocodeAcpClient(cli_path=_FAKE_ACP, cwd=tmp_path, turn_timeout=None)
    result = await client.run_turn("hi")
    assert result.stop_reason == "end_turn"
