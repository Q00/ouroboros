"""Tests for the bounded MCP serve shutdown drain and the client watchdog.

The MCP SDK's stdio session reads stdin via a shielded anyio worker thread
(``abandon_on_cancel=False``), so an unbounded ``await serve_task`` in the
shutdown path hangs forever whenever a stop was requested by a signal or the
watchdog while the client is alive but quiescent — the "server survives kill"
symptom. The drain is bounded; a serve task that outlives the graces forces a
hard exit, but only after every cleanup in the finally block has run.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.cli.commands import mcp as mcp_module


@pytest.fixture(autouse=True)
def _stub_mcp_dependency_preflight(monkeypatch):
    """Keep lifecycle tests independent of the optional MCP SDK install."""
    monkeypatch.setattr(mcp_module, "_require_mcp_dependency", lambda: None)


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
    monkeypatch.setattr(mcp_module, "_client_is_alive", lambda _pid, _start_marker=None: False)

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "stdio"),
            timeout=10.0,
        )

    mock_server.shutdown.assert_awaited_once()


class _HardExitCalled(BaseException):
    """Sentinel the test raises in place of ``os._exit``."""


@pytest.mark.asyncio
async def test_stuck_serve_loop_hard_exits_after_cleanup(monkeypatch) -> None:
    """A serve loop that swallows cancellation must force a hard exit.

    Under mcp>=2.0 no descriptor close can EOF the SDK's stdin reader (the
    wire is a private duplicate of fd 0) and the reader thread is non-daemon,
    so returning normally would hang interpreter teardown forever. The drain
    must therefore invoke the hard-exit backstop — but only AFTER the finally
    block's cleanup (server shutdown among it) has run.
    """
    mock_es, mock_repo, mock_server = _make_mocks()
    monkeypatch.setattr(mcp_module, "_SHUTDOWN_DRAIN_GRACE_SECONDS", 0.1)

    release = asyncio.Event()
    hard_exit_codes: list[int] = []

    def fake_hard_exit(code: int) -> None:
        hard_exit_codes.append(code)
        release.set()  # let the stuck task finish so the loop tears down clean
        raise _HardExitCalled

    monkeypatch.setattr(mcp_module, "_flush_and_hard_exit", fake_hard_exit)

    async def stuck_serve(*args, **kwargs):
        # Emulates the shielded stdin readline: swallows cancellation forever.
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue

    mock_server.serve.side_effect = stuck_serve

    # Dead client fires the watchdog -> stop -> shutdown path.
    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: (4242, 1.0))
    monkeypatch.setattr(mcp_module, "_client_is_alive", lambda _pid, _start_marker=None: False)

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch, pytest.raises(_HardExitCalled):
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "stdio"),
            timeout=10.0,
        )

    assert hard_exit_codes == [0]
    # Cleanup must have completed BEFORE the hard exit fired.
    mock_server.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_cooperative_unwind_does_not_hard_exit(monkeypatch) -> None:
    """An unwind that finishes within the graces must exit gracefully."""
    mock_es, mock_repo, mock_server = _make_mocks()
    monkeypatch.setattr(mcp_module, "_SHUTDOWN_DRAIN_GRACE_SECONDS", 0.3)

    hard_exit_codes: list[int] = []
    monkeypatch.setattr(
        mcp_module, "_flush_and_hard_exit", lambda code: hard_exit_codes.append(code)
    )

    stop_probe = asyncio.Event()

    async def slow_then_cooperative_serve(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # One slow unwind beyond the first grace window, then exit.
            stop_probe.set()
            await asyncio.sleep(0.4)
            raise

    mock_server.serve.side_effect = slow_then_cooperative_serve

    monkeypatch.setattr(mcp_module, "_resolve_client_identity", lambda _ppid: (4242, 1.0))
    monkeypatch.setattr(mcp_module, "_client_is_alive", lambda _pid, _start_marker=None: False)

    es_patch, repo_patch, server_patch = _patches(mock_es, mock_repo, mock_server)
    with es_patch, repo_patch, server_patch:
        await asyncio.wait_for(
            mcp_module._run_mcp_server("localhost", 8080, "streamable-http"),
            timeout=10.0,
        )

    assert stop_probe.is_set()
    assert hard_exit_codes == [], "a cooperative unwind must never hard-exit"
    mock_server.shutdown.assert_awaited_once()


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
