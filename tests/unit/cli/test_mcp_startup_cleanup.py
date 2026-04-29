"""Unit tests for auto-cleanup on MCP server startup.

Tests the orphaned session detection and cleanup that runs during
MCP server startup in _run_mcp_server(), ensuring:
- Orphaned sessions (RUNNING/PAUSED >1h) are detected and cancelled
- No orphans results in no cancellation calls
- Cleanup failures don't prevent server startup (graceful degradation)
- Correct stderr output for visibility in stdio mode
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.session import (
    SessionRepository,
    SessionStatus,
    SessionTracker,
)


def _make_tracker(
    session_id: str = "orch_test123",
    execution_id: str = "exec_001",
    status: SessionStatus = SessionStatus.RUNNING,
    start_time: datetime | None = None,
) -> SessionTracker:
    """Create a SessionTracker for testing."""
    return SessionTracker(
        session_id=session_id,
        execution_id=execution_id,
        seed_id="seed_001",
        status=status,
        start_time=start_time or datetime.now(UTC),
    )


class TestMCPStartupAutoCleanup:
    """Tests for auto-cleanup during MCP server startup (_run_mcp_server)."""

    @pytest.fixture(autouse=True)
    def _stub_brownfield_store(self):
        """Stub ``BrownfieldStore`` for every test in this class.

        ``_run_mcp_server()`` always constructs and initializes a
        ``BrownfieldStore`` after PR #487. Without this stub, tests that do
        not patch it explicitly would open and migrate the real default DB
        at ``~/.ouroboros/ouroboros.db``, making the unit tests stateful and
        unsafe in environments where ``$HOME`` is not writable. Tests that
        need a custom stub (e.g. to simulate init failure) can override by
        patching ``ouroboros.persistence.brownfield.BrownfieldStore`` again
        inside the test — the inner patch wins for its scope.
        """
        mock_brownfield = AsyncMock()
        mock_brownfield.initialize = AsyncMock()
        with patch(
            "ouroboros.persistence.brownfield.BrownfieldStore",
            return_value=mock_brownfield,
        ):
            yield mock_brownfield

    def _create_patches(
        self,
        mock_event_store: AsyncMock | None = None,
        mock_repo: AsyncMock | None = None,
        cancelled_sessions: list | None = None,
        init_side_effect: Exception | None = None,
        cancel_side_effect: Exception | None = None,
    ):
        """Create all necessary patches for _run_mcp_server tests.

        Returns a context manager tuple and the mock objects.
        """
        if mock_event_store is None:
            mock_event_store = AsyncMock()
            mock_event_store.initialize = AsyncMock(side_effect=init_side_effect)

        if mock_repo is None:
            mock_repo = AsyncMock()
            if cancel_side_effect:
                mock_repo.cancel_orphaned_sessions = AsyncMock(side_effect=cancel_side_effect)
            else:
                mock_repo.cancel_orphaned_sessions = AsyncMock(
                    return_value=cancelled_sessions or []
                )

        mock_server = MagicMock()
        mock_server.info.tools = []
        mock_server.serve = AsyncMock()

        return mock_event_store, mock_repo, mock_server

    @pytest.mark.asyncio
    async def test_no_orphans_does_not_cancel(self) -> None:
        """Test that startup with no orphaned sessions skips cancellation."""
        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=[])

        async def serve_side_effect(*args, **kwargs) -> None:
            await asyncio.sleep(0)

        mock_server.serve.side_effect = serve_side_effect

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        mock_repo.cancel_orphaned_sessions.assert_called_once()
        mock_es.initialize.assert_awaited_once()
        mock_server.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_does_not_wait_for_background_cleanup(self) -> None:
        """Server startup should not wait for orphan cleanup to finish."""
        cleanup_started = asyncio.Event()
        allow_cleanup_finish = asyncio.Event()
        serve_started = asyncio.Event()

        mock_es = AsyncMock()
        mock_es.initialize = AsyncMock()
        mock_repo = AsyncMock()

        async def slow_cleanup() -> list:
            cleanup_started.set()
            await allow_cleanup_finish.wait()
            return []

        mock_repo.cancel_orphaned_sessions = AsyncMock(side_effect=slow_cleanup)

        mock_server = MagicMock()
        mock_server.info.tools = []

        async def serve_side_effect(*args, **kwargs) -> None:
            serve_started.set()
            allow_cleanup_finish.set()
            await asyncio.sleep(0)

        mock_server.serve = AsyncMock(side_effect=serve_side_effect)

        with (
            patch("ouroboros.persistence.event_store.EventStore", return_value=mock_es),
            patch("ouroboros.orchestrator.session.SessionRepository", return_value=mock_repo),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        assert cleanup_started.is_set()
        assert serve_started.is_set()
        mock_server.serve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orphans_detected_and_cancelled(self) -> None:
        """Test that orphaned sessions are cancelled on startup."""
        orphaned_trackers = [
            _make_tracker(
                session_id="orch_orphan_1",
                execution_id="exec_orphan_1",
                status=SessionStatus.RUNNING,
            ),
            _make_tracker(
                session_id="orch_orphan_2",
                execution_id="exec_orphan_2",
                status=SessionStatus.PAUSED,
            ),
        ]

        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=orphaned_trackers)

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
            patch("ouroboros.cli.commands.mcp._stderr_console") as mock_console,
        ):

            async def serve_side_effect(*args, **kwargs) -> None:
                await asyncio.sleep(0)

            mock_server.serve.side_effect = serve_side_effect

            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        mock_repo.cancel_orphaned_sessions.assert_called_once()
        mock_console.print.assert_any_call("[yellow]Auto-cancelled 2 orphaned session(s)[/yellow]")
        mock_server.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_streamable_http_advertises_endpoint_and_uses_stdout_startup(self, capfd) -> None:
        """Streamable HTTP startup output advertises the client endpoint."""
        orphaned_tracker = _make_tracker(
            session_id="orch_orphan_http",
            execution_id="exec_orphan_http",
            status=SessionStatus.RUNNING,
        )
        cleanup_called = asyncio.Event()

        mock_es = AsyncMock()
        mock_es.initialize = AsyncMock()
        mock_repo = AsyncMock()

        async def cancel_orphans() -> list[SessionTracker]:
            cleanup_called.set()
            return [orphaned_tracker]

        mock_repo.cancel_orphaned_sessions = AsyncMock(side_effect=cancel_orphans)

        mock_server = MagicMock()
        mock_server.info.tools = []

        async def serve_side_effect(*args, **kwargs) -> None:
            await cleanup_called.wait()
            await asyncio.sleep(0.01)

        mock_server.serve = AsyncMock(side_effect=serve_side_effect)

        with (
            patch("ouroboros.cli.commands.mcp._ensure_shell_env", lambda **_: None),
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
            patch("ouroboros.mcp.bridge.create_bridge_from_env", return_value=None),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            capfd.readouterr()
            await _run_mcp_server("127.0.0.1", 9100, "streamable-http")

        captured = capfd.readouterr()
        assert "http://127.0.0.1:9100/mcp" in captured.out
        assert "Auto-cancelled 1 orphaned session(s)" in captured.out
        assert "http://127.0.0.1:9100/mcp" not in captured.err
        assert "Auto-cancelled 1 orphaned session(s)" not in captured.err
        mock_server.serve.assert_awaited_once_with(
            transport="streamable-http",
            host="127.0.0.1",
            port=9100,
        )

    @pytest.mark.asyncio
    async def test_pending_background_cleanup_is_cancelled_on_shutdown(self) -> None:
        """Server shutdown should cancel an unfinished startup cleanup task."""
        cleanup_started = asyncio.Event()
        cleanup_cancelled = asyncio.Event()

        mock_es = AsyncMock()
        mock_es.initialize = AsyncMock()
        mock_repo = AsyncMock()

        async def slow_cleanup() -> list:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

        mock_repo.cancel_orphaned_sessions = AsyncMock(side_effect=slow_cleanup)

        mock_server = MagicMock()
        mock_server.info.tools = []

        async def serve_side_effect(*args, **kwargs) -> None:
            await cleanup_started.wait()

        mock_server.serve = AsyncMock(side_effect=serve_side_effect)

        with (
            patch("ouroboros.persistence.event_store.EventStore", return_value=mock_es),
            patch("ouroboros.orchestrator.session.SessionRepository", return_value=mock_repo),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        assert cleanup_started.is_set()
        assert cleanup_cancelled.is_set()
        mock_server.serve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_store_init_failure_aborts_startup(self) -> None:
        """Persistent-store init failures must surface — the server cannot
        safely run with a half-initialized store, so the failure propagates
        instead of being swallowed and the server never starts serving."""
        mock_es, _, mock_server = self._create_patches()
        mock_es.initialize = AsyncMock(side_effect=Exception("DB connection failed"))

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            with pytest.raises(Exception, match="DB connection failed"):
                await _run_mcp_server("localhost", 8080, "stdio")

        mock_server.serve.assert_not_called()

    @pytest.mark.asyncio
    async def test_brownfield_store_init_failure_aborts_startup(self) -> None:
        """Regression for PR #487: a failed shared brownfield-store init must
        abort startup rather than wedge brownfield MCP access until restart."""
        mock_es, _, mock_server = self._create_patches()

        mock_brownfield = AsyncMock()
        mock_brownfield.initialize = AsyncMock(side_effect=Exception("brownfield migration failed"))

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.persistence.brownfield.BrownfieldStore",
                return_value=mock_brownfield,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            with pytest.raises(Exception, match="brownfield migration failed"):
                await _run_mcp_server("localhost", 8080, "stdio")

        mock_es.initialize.assert_awaited_once()
        mock_brownfield.initialize.assert_awaited_once()
        mock_server.serve.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_orphaned_sessions_failure_does_not_block(self) -> None:
        """Test that cancel_orphaned_sessions raising doesn't block startup."""
        mock_es, mock_repo, mock_server = self._create_patches(
            cancel_side_effect=Exception("Unexpected error during cleanup")
        )

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
            patch("ouroboros.cli.commands.mcp._stderr_console") as mock_console,
        ):

            async def serve_side_effect(*args, **kwargs) -> None:
                await asyncio.sleep(0)

            mock_server.serve.side_effect = serve_side_effect

            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        mock_server.serve.assert_called_once()
        warning_calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("auto-cleanup failed" in call for call in warning_calls)

    @pytest.mark.asyncio
    async def test_event_store_initialized_before_cleanup(self) -> None:
        """Test that EventStore.initialize() is called before cleanup runs."""
        call_order: list[str] = []

        mock_es = AsyncMock()

        async def track_initialize() -> None:
            call_order.append("initialize")

        mock_es.initialize = AsyncMock(side_effect=track_initialize)

        mock_repo = AsyncMock()

        async def track_cancel(*args, **kwargs) -> list:
            call_order.append("cancel_orphaned")
            return []

        mock_repo.cancel_orphaned_sessions = AsyncMock(side_effect=track_cancel)

        mock_server = MagicMock()
        mock_server.info.tools = []

        async def serve_side_effect(*args, **kwargs) -> None:
            await asyncio.sleep(0)

        mock_server.serve = AsyncMock(side_effect=serve_side_effect)

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        assert call_order[:2] == ["initialize", "cancel_orphaned"]

    @pytest.mark.asyncio
    async def test_runtime_backend_is_forwarded_to_server_factory(self) -> None:
        """Runtime override is passed through to the MCP composition root."""
        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=[])

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ) as mock_create_server,
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio", runtime_backend="codex")

        mock_create_server.assert_called_once()
        assert mock_create_server.call_args.kwargs["runtime_backend"] == "codex"

    @pytest.mark.asyncio
    async def test_llm_backend_is_forwarded_to_server_factory(self) -> None:
        """LLM backend override is passed through to the MCP composition root."""
        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=[])

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ) as mock_create_server,
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio", llm_backend="codex")

        mock_create_server.assert_called_once()
        assert mock_create_server.call_args.kwargs["llm_backend"] == "codex"

    @pytest.mark.asyncio
    async def test_custom_db_path_used_for_cleanup(self) -> None:
        """Test that custom db_path is passed to EventStore for cleanup."""
        mock_es = AsyncMock()
        mock_es.initialize = AsyncMock()

        mock_repo = AsyncMock()
        mock_repo.cancel_orphaned_sessions = AsyncMock(return_value=[])

        mock_server = MagicMock()
        mock_server.info.tools = []
        mock_server.serve = AsyncMock()

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ) as MockEventStore,
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio", db_path="/tmp/test.db")

        MockEventStore.assert_called_once_with("sqlite+aiosqlite:////tmp/test.db")

    @pytest.mark.asyncio
    async def test_single_orphan_reports_correct_count(self) -> None:
        """Test correct stderr output when exactly 1 orphan is found."""
        single_orphan = [
            _make_tracker(session_id="orch_lonely", status=SessionStatus.RUNNING),
        ]

        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=single_orphan)

        async def serve_side_effect(*args, **kwargs) -> None:
            await asyncio.sleep(0)

        mock_server.serve.side_effect = serve_side_effect

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
            patch("ouroboros.cli.commands.mcp._stderr_console") as mock_console,
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        mock_console.print.assert_any_call("[yellow]Auto-cancelled 1 orphaned session(s)[/yellow]")

    @pytest.mark.asyncio
    async def test_pid_file_write_failure_does_not_block_startup(self) -> None:
        """Test that PID file permission errors do not block server startup."""
        mock_es, mock_repo, mock_server = self._create_patches(cancelled_sessions=[])

        with (
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_es,
            ),
            patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_repo,
            ),
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=mock_server,
            ),
            patch("pathlib.Path.write_text", side_effect=PermissionError("denied")),
        ):
            from ouroboros.cli.commands.mcp import _run_mcp_server

            await _run_mcp_server("localhost", 8080, "stdio")

        mock_server.serve.assert_called_once()


class TestFindOrphanedSessionsEdgeCases:
    """Additional edge-case tests for orphan detection logic."""

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.get_all_sessions = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def repository(self, mock_event_store: AsyncMock) -> SessionRepository:
        return SessionRepository(mock_event_store)

    def _make_start_event(
        self,
        session_id: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        event = MagicMock()
        event.type = "orchestrator.session.started"
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {
            "execution_id": f"exec_{session_id}",
            "seed_id": f"seed_{session_id}",
            "start_time": (timestamp or datetime.now(UTC)).isoformat(),
        }
        return event

    def _make_event(
        self,
        session_id: str,
        event_type: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        event = MagicMock()
        event.type = event_type
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {}
        return event

    @pytest.mark.asyncio
    async def test_empty_event_store_returns_no_orphans(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that an empty event store yields no orphans."""
        mock_event_store.get_all_sessions.return_value = []

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_all_sessions_completed_returns_no_orphans(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test with all sessions in terminal states."""
        old_time = datetime.now(UTC) - timedelta(hours=5)

        start_1 = self._make_start_event("s1", timestamp=old_time)
        completed_1 = self._make_event(
            "s1", "orchestrator.session.completed", timestamp=old_time + timedelta(hours=1)
        )
        start_2 = self._make_start_event("s2", timestamp=old_time)
        failed_2 = self._make_event(
            "s2", "orchestrator.session.failed", timestamp=old_time + timedelta(hours=1)
        )
        start_3 = self._make_start_event("s3", timestamp=old_time)
        cancelled_3 = self._make_event(
            "s3", "orchestrator.session.cancelled", timestamp=old_time + timedelta(hours=1)
        )

        mock_event_store.get_all_sessions.return_value = [start_1, start_2, start_3]

        async def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            return {
                "s1": [start_1, completed_1],
                "s2": [start_2, failed_2],
                "s3": [start_3, cancelled_3],
            }.get(aggregate_id, [])

        mock_event_store.replay.side_effect = mock_replay

        result = await repository.find_orphaned_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_session_with_no_timestamp_uses_start_time_fallback(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that sessions with None timestamp fall back to start_time."""
        old_time = datetime.now(UTC) - timedelta(hours=3)
        start_event = self._make_start_event("s1", timestamp=old_time)
        # Event with None timestamp
        last_event = self._make_event("s1", "orchestrator.progress.updated")
        last_event.timestamp = None

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, last_event]

        result = await repository.find_orphaned_sessions()

        # Should still detect it using start_time fallback
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_session_with_no_timestamp_and_no_start_time_skipped(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that sessions with no timestamp and no start_time are skipped."""
        start_event = self._make_start_event("s1")
        start_event.data = {}  # No start_time in data
        last_event = self._make_event("s1", "orchestrator.progress.updated")
        last_event.timestamp = None

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, last_event]

        result = await repository.find_orphaned_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_session_with_naive_timestamp_handled(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that naive (non-tz-aware) timestamps are handled correctly."""
        # Naive datetime (no tzinfo) - should be treated as UTC
        old_naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
        start_event = self._make_start_event("s1", timestamp=old_naive)
        start_event.timestamp = old_naive  # Ensure it's naive

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        result = await repository.find_orphaned_sessions()

        # Should still detect the orphan despite naive timestamp
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_session_just_within_threshold_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a session exactly at the boundary is NOT orphaned."""
        # 59 minutes ago (under 1 hour threshold)
        recent_time = datetime.now(UTC) - timedelta(minutes=59)
        start_event = self._make_start_event("s1", timestamp=recent_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        result = await repository.find_orphaned_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_session_just_beyond_threshold_is_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a session just beyond threshold IS orphaned."""
        # 61 minutes ago (over 1 hour threshold)
        stale_time = datetime.now(UTC) - timedelta(minutes=61)
        start_event = self._make_start_event("s1", timestamp=stale_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        result = await repository.find_orphaned_sessions()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_replay_returns_empty_events_skipped(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that sessions with empty replay events are skipped."""
        start_event = self._make_start_event("s1")

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = []  # No events replayed

        result = await repository.find_orphaned_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_reconstruct_failure_excludes_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a session whose reconstruction fails is excluded."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_event = self._make_start_event("s1", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        # Patch reconstruct_session to fail
        with patch.object(
            repository,
            "reconstruct_session",
            return_value=Result.err(PersistenceError("corrupt data")),
        ):
            result = await repository.find_orphaned_sessions()

        # Orphan detection succeeds, but reconstruct fails — session excluded
        assert result == []


class TestCancelOrphanedSessionsEdgeCases:
    """Additional edge-case tests for cancel_orphaned_sessions."""

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.get_all_sessions = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def repository(self, mock_event_store: AsyncMock) -> SessionRepository:
        return SessionRepository(mock_event_store)

    @pytest.mark.asyncio
    async def test_partial_failure_cancels_what_it_can(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that if one cancellation fails, others still proceed."""
        old_time = datetime.now(UTC) - timedelta(hours=2)

        tracker_1 = _make_tracker(
            session_id="s1", status=SessionStatus.RUNNING, start_time=old_time
        )
        tracker_2 = _make_tracker(
            session_id="s2", status=SessionStatus.RUNNING, start_time=old_time
        )

        with patch.object(
            repository,
            "find_orphaned_sessions",
            return_value=[tracker_1, tracker_2],
        ):
            call_count = 0

            async def mock_mark_cancelled(session_id: str, reason: str, cancelled_by: str):
                nonlocal call_count
                call_count += 1
                from ouroboros.core.errors import PersistenceError
                from ouroboros.core.types import Result

                if session_id == "s1":
                    return Result.err(PersistenceError("DB write failed"))
                return Result.ok(None)

            with patch.object(repository, "mark_cancelled", side_effect=mock_mark_cancelled):
                result = await repository.cancel_orphaned_sessions()

        # Only s2 should be in the result (s1 failed)
        assert len(result) == 1
        assert result[0].session_id == "s2"
        # Both were attempted
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cancel_orphaned_uses_auto_cleanup_reason(
        self,
        repository: SessionRepository,
    ) -> None:
        """Test that cancelled_by is set to 'auto_cleanup' for all orphans."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        tracker = _make_tracker(session_id="s1", status=SessionStatus.RUNNING, start_time=old_time)

        cancel_calls: list[dict] = []

        async def capture_cancel(session_id: str, reason: str, cancelled_by: str):
            from ouroboros.core.types import Result

            cancel_calls.append(
                {
                    "session_id": session_id,
                    "reason": reason,
                    "cancelled_by": cancelled_by,
                }
            )
            return Result.ok(None)

        with (
            patch.object(
                repository,
                "find_orphaned_sessions",
                return_value=[tracker],
            ),
            patch.object(
                repository,
                "mark_cancelled",
                side_effect=capture_cancel,
            ),
        ):
            await repository.cancel_orphaned_sessions()

        assert len(cancel_calls) == 1
        assert cancel_calls[0]["cancelled_by"] == "auto_cleanup"
        assert "Auto-cancelled on startup" in cancel_calls[0]["reason"]

    @pytest.mark.asyncio
    async def test_cancel_orphaned_includes_status_in_reason(
        self,
        repository: SessionRepository,
    ) -> None:
        """Test that the reason message includes the session's previous status."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        tracker = _make_tracker(
            session_id="s_paused",
            status=SessionStatus.PAUSED,
            start_time=old_time,
        )

        cancel_calls: list[dict] = []

        async def capture_cancel(session_id: str, reason: str, cancelled_by: str):
            from ouroboros.core.types import Result

            cancel_calls.append({"reason": reason})
            return Result.ok(None)

        with (
            patch.object(
                repository,
                "find_orphaned_sessions",
                return_value=[tracker],
            ),
            patch.object(
                repository,
                "mark_cancelled",
                side_effect=capture_cancel,
            ),
        ):
            await repository.cancel_orphaned_sessions()

        assert "paused" in cancel_calls[0]["reason"]

    @pytest.mark.asyncio
    async def test_cancel_orphaned_empty_returns_empty_immediately(
        self,
        repository: SessionRepository,
    ) -> None:
        """Test that no orphans means mark_cancelled is never called."""
        with patch.object(
            repository,
            "find_orphaned_sessions",
            return_value=[],
        ) as mock_find:
            with patch.object(
                repository,
                "mark_cancelled",
            ) as mock_cancel:
                result = await repository.cancel_orphaned_sessions()

        assert result == []
        mock_find.assert_called_once()
        mock_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_staleness_threshold_passed_through(
        self,
        repository: SessionRepository,
    ) -> None:
        """Test that custom staleness threshold is passed to find_orphaned_sessions."""
        custom_threshold = timedelta(minutes=30)

        with patch.object(
            repository,
            "find_orphaned_sessions",
            return_value=[],
        ) as mock_find:
            await repository.cancel_orphaned_sessions(staleness_threshold=custom_threshold)

        mock_find.assert_called_once_with(custom_threshold)
