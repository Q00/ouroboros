"""Tests for the bounded MCP serve shutdown drain and the client watchdog.

The MCP SDK's stdio session reads stdin via a shielded anyio worker thread
(``abandon_on_cancel=False``), so an unbounded ``await serve_task`` in the
shutdown path hangs forever whenever a stop was requested by a signal or the
watchdog while the client is alive but quiescent — the "server survives kill"
symptom. The drain is bounded; after the grace, fd 0 is closed (stdio only)
to EOF the blocked readline so the normal cleanup path completes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.cli.commands import mcp as mcp_module


@pytest.fixture(autouse=True)
def _stub_brownfield_store():
    """Keep ``_run_mcp_server`` off the real ~/.ouroboros database."""
    mock_brownfield = AsyncMock()
    mock_brownfield.initialize = AsyncMock()
    with patch(
        "ouroboros.persistence.brownfield.BrownfieldStore",
        return_value=mock_brownfield,
    ):
        yield mock_brownfield


@pytest.fixture(autouse=True)
def _isolate_pid_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_PID_REGISTRY_DIR", tmp_path / "mcp-servers")
    monkeypatch.setattr(mcp_module, "_LEGACY_PID_FILE", tmp_path / "mcp-server.pid")
    monkeypatch.setattr(mcp_module, "_own_pid_file", None)
    monkeypatch.setattr(mcp_module, "_own_pid_payload", None)


def _make_mocks():
    mock_es = AsyncMock()
    mock_es.initialize = AsyncMock()
    mock_repo = AsyncMock()
    mock_repo.cancel_orphaned_sessions = AsyncMock(return_value=[])
    mock_server = MagicMock()
    mock_server.info.tools = []
    mock_server.serve = AsyncMock()
    mock_server.shutdown = AsyncMock()
    return mock_es, mock_repo, mock_server


def _patches(mock_es, mock_repo, mock_server):
    return (
        patch("ouroboros.persistence.event_store.EventStore", return_value=mock_es),
        patch("ouroboros.orchestrator.session.SessionRepository", return_value=mock_repo),
        patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=mock_server,
        ),
    )


@pytest.mark.asyncio
async def test_watchdog_dead_client_stops_server(monkeypatch) -> None:
    """A resolved client identity that dies must stop the serve loop.

    Under the shipped ``client -> uvx -> python`` topology the direct parent
    (uvx) survives the client's death, so the getppid() check alone can never
    fire — the absolute client-identity poll is what catches this case.
    """
    mock_es, mock_repo, mock_server = _make_mocks()

    async def cooperative_serve(*args, **kwargs):
        await asyncio.sleep(3600)

    mock_server.serve.side_effect = cooperative_serve

    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: (4242, 1.0))
    monkeypatch.setattr(
        mcp_module, "is_process_identity_alive", lambda _pid, _start_time=None: False
    )

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "stdio"),
            timeout=10.0,
        )

    mock_server.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_stuck_serve_loop_is_bounded_and_unblocked_by_fd0_close(monkeypatch) -> None:
    """A serve loop that swallows cancellation must not hang shutdown."""
    mock_es, mock_repo, mock_server = _make_mocks()
    monkeypatch.setattr(mcp_module, "_SHUTDOWN_DRAIN_GRACE_SECONDS", 0.2)

    closed_fds: list[int] = []
    release = asyncio.Event()
    real_close = mcp_module.os.close

    def fake_close(fd: int) -> None:
        # Intercept only fd 0 (the drain's EOF escalation); everything else
        # must really close or subprocess plumbing (ps lookups) deadlocks.
        if fd == 0:
            closed_fds.append(fd)
            release.set()
            return
        real_close(fd)

    monkeypatch.setattr(mcp_module.os, "close", fake_close)

    async def stuck_serve(*args, **kwargs):
        # Emulates the shielded stdin readline: swallows cancellation and only
        # returns once fd 0 is closed (EOF reaches the worker thread).
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue

    mock_server.serve.side_effect = stuck_serve

    # Dead client fires the watchdog -> stop -> shutdown path.
    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: (4242, 1.0))
    monkeypatch.setattr(
        mcp_module, "is_process_identity_alive", lambda _pid, _start_time=None: False
    )

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "stdio"),
            timeout=10.0,
        )

    assert 0 in closed_fds, "the drain must EOF fd 0 after the grace expired"
    mock_server.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_stdio_transport_never_closes_fd0(monkeypatch) -> None:
    mock_es, mock_repo, mock_server = _make_mocks()
    monkeypatch.setattr(mcp_module, "_SHUTDOWN_DRAIN_GRACE_SECONDS", 0.1)

    closed_fds: list[int] = []
    real_close = mcp_module.os.close

    def fake_close(fd: int) -> None:
        if fd == 0:
            closed_fds.append(fd)
            return
        real_close(fd)

    monkeypatch.setattr(mcp_module.os, "close", fake_close)

    stop_probe = asyncio.Event()

    async def slow_then_cooperative_serve(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # One slow unwind beyond the first grace window, then exit.
            stop_probe.set()
            await asyncio.sleep(0.3)
            raise

    mock_server.serve.side_effect = slow_then_cooperative_serve

    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: (4242, 1.0))
    monkeypatch.setattr(
        mcp_module, "is_process_identity_alive", lambda _pid, _start_time=None: False
    )

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "streamable-http"),
            timeout=10.0,
        )

    assert stop_probe.is_set()
    assert closed_fds == [], "fd 0 belongs to the console on network transports"


@pytest.mark.asyncio
async def test_job_manager_drained_before_server_shutdown(monkeypatch) -> None:
    """Live jobs must be terminalized while the EventStore is still open."""
    from ouroboros.mcp.job_manager import JobManager

    mock_es, mock_repo, mock_server = _make_mocks()

    call_order: list[str] = []

    job_manager = MagicMock(spec=JobManager)

    async def record_drain(grace_seconds: float) -> int:
        call_order.append("drain")
        return 0

    job_manager.drain = record_drain
    mock_server.job_manager = job_manager

    async def record_shutdown() -> None:
        call_order.append("shutdown")

    mock_server.shutdown = AsyncMock(side_effect=record_shutdown)

    async def quick_serve(*args, **kwargs):
        await asyncio.sleep(0)

    mock_server.serve.side_effect = quick_serve
    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: None)

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "stdio"),
            timeout=10.0,
        )

    assert call_order == ["drain", "shutdown"]


@pytest.mark.asyncio
async def test_early_composition_failure_closes_stores(monkeypatch) -> None:
    """A failure before the adapter exists must still release the stores."""
    mock_es, mock_repo, _ = _make_mocks()

    es_patch, repo_patch, _ = _patches(mock_es, mock_repo, MagicMock())
    with (
        es_patch,
        repo_patch,
        patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            side_effect=ValueError("bad backend"),
        ),
        pytest.raises(ValueError, match="bad backend"),
    ):
        await mcp_module._run_mcp_server("localhost", 8080, "stdio")

    mock_es.close.assert_awaited_once()
