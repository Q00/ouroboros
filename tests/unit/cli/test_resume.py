"""Unit tests for the resume command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.resume import _get_in_flight_sessions, app

runner = CliRunner()

# Patch target for SessionRepository — imported lazily inside the function
_SESSION_REPO_PATH = "ouroboros.orchestrator.session.SessionRepository"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(
    session_id: str = "sess-abc123",
    execution_id: str = "exec-xyz789",
    seed_id: str = "seed-001",
    status_value: str = "running",
) -> MagicMock:
    """Return a minimal SessionTracker-like mock."""
    from ouroboros.orchestrator.session import SessionStatus

    tracker = MagicMock()
    tracker.session_id = session_id
    tracker.execution_id = execution_id
    tracker.seed_id = seed_id
    tracker.status = SessionStatus(status_value)
    tracker.start_time = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    return tracker


def _make_event(aggregate_id: str) -> MagicMock:
    event = MagicMock()
    event.aggregate_id = aggregate_id
    return event


# ---------------------------------------------------------------------------
# _get_in_flight_sessions
# ---------------------------------------------------------------------------


class TestGetInFlightSessions:
    """Tests for the _get_in_flight_sessions helper."""

    @pytest.mark.asyncio
    async def test_returns_running_sessions(self) -> None:
        """Running sessions are returned."""
        tracker = _make_tracker(status_value="running")
        event = _make_event("sess-abc123")

        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = [event]

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=ok_result)
            result = await _get_in_flight_sessions(event_store)

        assert result == [tracker]

    @pytest.mark.asyncio
    async def test_returns_paused_sessions(self) -> None:
        """Paused sessions are also returned."""
        tracker = _make_tracker(status_value="paused")
        event = _make_event("sess-paused")

        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = [event]

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=ok_result)
            result = await _get_in_flight_sessions(event_store)

        assert result == [tracker]

    @pytest.mark.asyncio
    async def test_excludes_terminal_sessions(self) -> None:
        """Completed / failed / cancelled sessions are not returned."""
        for status in ("completed", "failed", "cancelled"):
            tracker = _make_tracker(status_value=status)
            event = _make_event(f"sess-{status}")

            event_store = AsyncMock()
            event_store.get_all_sessions.return_value = [event]

            ok_result = MagicMock()
            ok_result.is_err = False
            ok_result.value = tracker

            with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
                MockRepo.return_value.reconstruct_session = AsyncMock(return_value=ok_result)
                result = await _get_in_flight_sessions(event_store)

            assert result == [], f"Expected empty list for status={status!r}"

    @pytest.mark.asyncio
    async def test_empty_event_store_returns_empty_list(self) -> None:
        """No sessions in the DB → empty list."""
        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = []

        with patch(_SESSION_REPO_PATH, autospec=True):
            result = await _get_in_flight_sessions(event_store)

        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_session_events(self) -> None:
        """If the same session_id appears more than once, reconstruct is called once."""
        tracker = _make_tracker(status_value="running")
        events = [_make_event("sess-dup"), _make_event("sess-dup")]

        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = events

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=ok_result)
            result = await _get_in_flight_sessions(event_store)

        mock_repo.reconstruct_session.assert_called_once_with("sess-dup")
        assert result == [tracker]

    @pytest.mark.asyncio
    async def test_skips_sessions_that_fail_to_reconstruct(self) -> None:
        """If a session cannot be reconstructed, it is silently skipped."""
        event = _make_event("sess-broken")
        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = [event]

        err_result = MagicMock()
        err_result.is_err = True

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=err_result)
            result = await _get_in_flight_sessions(event_store)

        assert result == []


# ---------------------------------------------------------------------------
# CLI integration — empty state
# ---------------------------------------------------------------------------


class TestResumeCLIEmpty:
    """Tests for the `ouroboros resume` command with no sessions."""

    def _invoke_with_empty_store(self, tmp_path: Path) -> object:
        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = []
        event_store.close = AsyncMock()

        async def _fake_get_event_store():
            return event_store

        with (
            patch(
                "ouroboros.cli.commands.resume._get_event_store",
                side_effect=_fake_get_event_store,
            ),
            patch(_SESSION_REPO_PATH, autospec=True),
        ):
            return runner.invoke(app, [], catch_exceptions=False)

    def test_exit_code_zero_when_no_sessions(self, tmp_path: Path) -> None:
        result = self._invoke_with_empty_store(tmp_path)
        assert result.exit_code == 0

    def test_no_crash_when_no_sessions(self, tmp_path: Path) -> None:
        """Command must not raise or crash when the DB is empty."""
        result = self._invoke_with_empty_store(tmp_path)
        assert "No in-flight sessions" in result.output or result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI integration — corrupted / missing DB
# ---------------------------------------------------------------------------


class TestResumeCLICorrupted:
    """Tests for graceful handling of a bad or missing EventStore."""

    def test_handles_corrupted_db_gracefully(self) -> None:
        """If the EventStore raises during initialization, the command handles it."""

        async def _raise():
            raise Exception("database disk image is malformed")

        with patch(
            "ouroboros.cli.commands.resume._get_event_store",
            side_effect=_raise,
        ):
            result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code in (0, 1)

    def test_handles_get_all_sessions_exception(self) -> None:
        """If get_all_sessions raises mid-flight, error is surfaced without crash."""
        event_store = AsyncMock()
        event_store.get_all_sessions.side_effect = Exception("DB locked")
        event_store.close = AsyncMock()

        async def _fake_get_event_store():
            return event_store

        with patch(
            "ouroboros.cli.commands.resume._get_event_store",
            side_effect=_fake_get_event_store,
        ):
            result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# CLI integration — sessions present, user selects one
# ---------------------------------------------------------------------------


class TestResumeCLIWithSessions:
    """Tests for interactive session selection."""

    def _build_mocks(self) -> tuple:
        tracker = _make_tracker()
        event = _make_event("sess-abc123")

        event_store = AsyncMock()
        event_store.get_all_sessions.return_value = [event]
        event_store.close = AsyncMock()

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        return tracker, event_store, ok_result

    def _invoke_with_sessions(self, input_text: str) -> object:
        tracker, event_store, ok_result = self._build_mocks()

        async def _fake_get_event_store():
            return event_store

        with (
            patch(
                "ouroboros.cli.commands.resume._get_event_store",
                side_effect=_fake_get_event_store,
            ),
            patch(_SESSION_REPO_PATH, autospec=True) as MockRepo,
        ):
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=ok_result)
            return runner.invoke(app, [], input=input_text, catch_exceptions=False)

    def test_lists_sessions_and_shows_exec_id(self) -> None:
        """When a session is selected, the exec_id is printed."""
        result = self._invoke_with_sessions("1\n")
        assert result.exit_code == 0
        assert "exec-xyz789" in result.output

    def test_quit_exits_cleanly(self) -> None:
        """Entering 'q' exits with code 0 and no crash."""
        result = self._invoke_with_sessions("q\n")
        assert result.exit_code == 0

    def test_invalid_selection_exits_with_error(self) -> None:
        """An out-of-range number exits with code 1."""
        result = self._invoke_with_sessions("99\n")
        assert result.exit_code == 1

    def test_status_hint_included_in_output(self) -> None:
        """The output suggests `ooo status <exec_id>` for re-attachment."""
        result = self._invoke_with_sessions("1\n")
        assert "ooo status" in result.output
