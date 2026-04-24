"""Unit tests for the resume command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from ouroboros.cli.commands.resume import (
    EXIT_CORRUPTED_DB,
    _format_reattach_guidance,
    _get_event_store,
    _get_in_flight_sessions,
    _is_active_snapshot,
    app,
)

runner = CliRunner()

# Patch target for SessionRepository — imported lazily inside the function
_SESSION_REPO_PATH = "ouroboros.orchestrator.session.SessionRepository"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(
    session_id: str = "sess-abc123",
    execution_id: str | None = "exec-xyz789",
    seed_id: str | None = "seed-001",
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


def _make_snapshot(
    session_id: str = "sess-abc123",
    status_event_type: str | None = None,
    last_activity: str = "2026-04-15T12:00:00+00:00",
    start_time: str = "2026-04-15T12:00:00+00:00",
) -> MagicMock:
    """Return a minimal SessionActivitySnapshot-like mock."""
    snapshot = MagicMock()
    snapshot.session_id = session_id
    snapshot.execution_id = "exec-xyz789"
    snapshot.seed_id = "seed-001"
    snapshot.status_event_type = status_event_type
    snapshot.last_activity = last_activity
    snapshot.start_time = start_time
    snapshot.runtime_status = None
    return snapshot


# ---------------------------------------------------------------------------
# Snapshot filtering
# ---------------------------------------------------------------------------


class TestIsActiveSnapshot:
    """The terminal short-circuit must match the orchestrator.session.* contract."""

    @pytest.mark.parametrize(
        "terminal",
        [
            "orchestrator.session.completed",
            "orchestrator.session.failed",
            "orchestrator.session.cancelled",
        ],
    )
    def test_terminal_status_is_inactive(self, terminal: str) -> None:
        assert _is_active_snapshot(_make_snapshot(status_event_type=terminal)) is False

    @pytest.mark.parametrize(
        "active",
        [
            None,
            "orchestrator.session.paused",
            "orchestrator.session.started",
            "orchestrator.progress.updated",
        ],
    )
    def test_non_terminal_status_is_active(self, active: str | None) -> None:
        assert _is_active_snapshot(_make_snapshot(status_event_type=active)) is True


# ---------------------------------------------------------------------------
# _get_in_flight_sessions
# ---------------------------------------------------------------------------


class TestGetInFlightSessions:
    """Tests for the _get_in_flight_sessions helper (snapshot-first path)."""

    @pytest.mark.asyncio
    async def test_returns_running_sessions(self) -> None:
        """Running sessions are returned."""
        tracker = _make_tracker(status_value="running")
        snapshot = _make_snapshot()

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot]

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
        snapshot = _make_snapshot(
            session_id="sess-paused",
            status_event_type="orchestrator.session.paused",
        )

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot]

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=ok_result)
            result = await _get_in_flight_sessions(event_store)

        assert result == [tracker]

    @pytest.mark.asyncio
    async def test_short_circuits_terminal_snapshots(self) -> None:
        """Snapshots with terminal status_event_type skip reconstruct entirely."""
        snapshot = _make_snapshot(status_event_type="orchestrator.session.completed")

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot]

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock()
            result = await _get_in_flight_sessions(event_store)

        mock_repo.reconstruct_session.assert_not_called()
        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_terminal_sessions(self) -> None:
        """Even if snapshot lets it through, reconstructed terminal status is dropped."""
        for status in ("completed", "failed", "cancelled"):
            tracker = _make_tracker(status_value=status)
            snapshot = _make_snapshot(session_id=f"sess-{status}")

            event_store = AsyncMock()
            event_store.get_session_activity_snapshots.return_value = [snapshot]

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
        event_store.get_session_activity_snapshots.return_value = []

        with patch(_SESSION_REPO_PATH, autospec=True):
            result = await _get_in_flight_sessions(event_store)

        assert result == []

    @pytest.mark.asyncio
    async def test_sorts_most_recent_first(self) -> None:
        """Multiple active sessions surface newest-first by last_activity."""
        tracker_old = _make_tracker(session_id="sess-old")
        tracker_new = _make_tracker(session_id="sess-new")

        snapshot_old = _make_snapshot(
            session_id="sess-old",
            last_activity="2026-04-10T00:00:00+00:00",
        )
        snapshot_new = _make_snapshot(
            session_id="sess-new",
            last_activity="2026-04-16T00:00:00+00:00",
        )

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot_old, snapshot_new]

        by_id = {"sess-old": tracker_old, "sess-new": tracker_new}

        async def _reconstruct(session_id):
            ok = MagicMock()
            ok.is_err = False
            ok.value = by_id[session_id]
            return ok

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(side_effect=_reconstruct)
            result = await _get_in_flight_sessions(event_store)

        assert [t.session_id for t in result] == ["sess-new", "sess-old"]

    @pytest.mark.asyncio
    async def test_respects_display_limit(self) -> None:
        """When more than `limit` sessions are active, only the top N are returned."""
        snapshots = [
            _make_snapshot(
                session_id=f"sess-{i:02d}",
                last_activity=f"2026-04-15T12:{i:02d}:00+00:00",
            )
            for i in range(25)
        ]
        trackers = {
            s.session_id: _make_tracker(session_id=s.session_id, status_value="running")
            for s in snapshots
        }

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = snapshots

        async def _reconstruct(session_id):
            ok = MagicMock()
            ok.is_err = False
            ok.value = trackers[session_id]
            return ok

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(side_effect=_reconstruct)
            result = await _get_in_flight_sessions(event_store, limit=5)

        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_skips_sessions_that_fail_to_reconstruct(self) -> None:
        """If a session cannot be reconstructed, it is silently skipped."""
        snapshot = _make_snapshot(session_id="sess-broken")
        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot]

        err_result = MagicMock()
        err_result.is_err = True

        with patch(_SESSION_REPO_PATH, autospec=True) as MockRepo:
            MockRepo.return_value.reconstruct_session = AsyncMock(return_value=err_result)
            result = await _get_in_flight_sessions(event_store)

        assert result == []


# ---------------------------------------------------------------------------
# Re-attach output contract
# ---------------------------------------------------------------------------


class TestFormatReattachGuidance:
    """The printed guidance must match the real CLI contracts."""

    def test_resume_command_points_at_run_workflow(self) -> None:
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "ouroboros run workflow --orchestrator --resume sess-abc123 seed-001" in output

    def test_inspect_command_points_at_tui_monitor(self) -> None:
        """Inspect guidance must point at a *functional* command.

        ``ouroboros status execution <id>`` is registered but its handler is
        still a placeholder (src/ouroboros/cli/commands/status.py) — it would
        print "Would show details for execution: ..." instead of doing
        anything useful. ``ouroboros tui monitor`` is the real working
        inspection path today, so the guidance points there until
        ``status execution`` is implemented.
        """
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "ouroboros tui monitor" in output
        assert "ouroboros status execution" not in output

    def test_surfaces_both_identifiers(self) -> None:
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "sess-abc123" in output
        assert "exec-xyz789" in output

    def test_missing_execution_id_falls_back_safely(self) -> None:
        tracker = _make_tracker(execution_id=None)
        output = _format_reattach_guidance(tracker)
        assert "<unknown>" in output
        assert "ouroboros run workflow --orchestrator --resume sess-abc123" in output

    def test_missing_seed_id_surfaces_placeholder(self) -> None:
        tracker = _make_tracker(seed_id=None)
        output = _format_reattach_guidance(tracker)
        assert "<seed.yaml>" in output
        assert "Seed ID was not recorded" in output


# ---------------------------------------------------------------------------
# Read-only guarantee — missing DB must NOT create anything
# ---------------------------------------------------------------------------


class TestResumeReadOnly:
    """Verify the command never mutates the filesystem when the DB is absent."""

    def test_missing_db_does_not_create_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With HOME pointed at a fresh tmp_path, no ~/.ouroboros/ must appear."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        # Sanity: directory does not exist beforehand.
        ouroboros_dir = fake_home / ".ouroboros"
        assert not ouroboros_dir.exists()

        result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "No in-flight sessions" in result.output
        assert not ouroboros_dir.exists(), (
            f"resume must not create {ouroboros_dir}; found: "
            f"{list(ouroboros_dir.iterdir()) if ouroboros_dir.exists() else 'n/a'}"
        )

    def test_missing_db_does_not_create_schema(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if the dir exists, the DB file must not be created."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".ouroboros").mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        db_file = fake_home / ".ouroboros" / "ouroboros.db"
        assert not db_file.exists()

        result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert not db_file.exists(), "resume must not create the SQLite file"


# ---------------------------------------------------------------------------
# CLI integration — empty state
# ---------------------------------------------------------------------------


class TestResumeCLIEmpty:
    """Tests for the `ouroboros resume` command with no sessions."""

    def _invoke_with_empty_store(self) -> object:
        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = []
        event_store.close = AsyncMock()

        async def _fake_get_event_store(db_path=None):
            return event_store

        with (
            patch(
                "ouroboros.cli.commands.resume._get_event_store",
                side_effect=_fake_get_event_store,
            ),
            patch(_SESSION_REPO_PATH, autospec=True),
        ):
            return runner.invoke(app, [], catch_exceptions=False)

    def test_exit_code_zero_when_no_sessions(self) -> None:
        result = self._invoke_with_empty_store()
        assert result.exit_code == 0

    def test_message_printed_when_no_sessions(self) -> None:
        result = self._invoke_with_empty_store()
        assert "No in-flight sessions" in result.output


# ---------------------------------------------------------------------------
# CLI integration — corrupted / missing DB
# ---------------------------------------------------------------------------


class TestResumeCLICorrupted:
    """Tests for graceful handling of a bad or missing EventStore."""

    def test_corrupted_db_returns_pinned_exit_code(self) -> None:
        """If the EventStore raises during initialization, exit with EXIT_CORRUPTED_DB."""

        async def _raise(db_path=None):
            raise Exception("database disk image is malformed")

        with patch(
            "ouroboros.cli.commands.resume._get_event_store",
            side_effect=_raise,
        ):
            result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code == EXIT_CORRUPTED_DB
        assert "Failed to open EventStore" in result.output
        assert "database disk image is malformed" in result.output

    def test_get_all_sessions_exception_returns_pinned_exit_code(self) -> None:
        """If snapshot query raises mid-flight, exit with EXIT_CORRUPTED_DB."""
        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.side_effect = Exception("DB locked")
        event_store.close = AsyncMock()

        async def _fake_get_event_store(db_path=None):
            return event_store

        with patch(
            "ouroboros.cli.commands.resume._get_event_store",
            side_effect=_fake_get_event_store,
        ):
            result = runner.invoke(app, [], catch_exceptions=False)

        assert result.exit_code == EXIT_CORRUPTED_DB
        assert "Failed to read EventStore" in result.output


# ---------------------------------------------------------------------------
# CLI integration — sessions present, user selects one
# ---------------------------------------------------------------------------


class TestResumeCLIWithSessions:
    """Tests for interactive session selection."""

    def _build_mocks(self) -> tuple:
        tracker = _make_tracker()
        snapshot = _make_snapshot()

        event_store = AsyncMock()
        event_store.get_session_activity_snapshots.return_value = [snapshot]
        event_store.close = AsyncMock()

        ok_result = MagicMock()
        ok_result.is_err = False
        ok_result.value = tracker

        return tracker, event_store, ok_result

    def _invoke_with_sessions(self, input_text: str) -> object:
        tracker, event_store, ok_result = self._build_mocks()

        async def _fake_get_event_store(db_path=None):
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

    def test_inspect_hint_points_at_functional_command(self) -> None:
        """Inspect hint must be a *working* command (``tui monitor``).

        Pinned contract: the resume output must not direct users at the
        placeholder ``status execution`` handler (Finding #2).
        """
        result = self._invoke_with_sessions("1\n")
        assert "ouroboros tui monitor" in result.output
        assert "ouroboros status execution" not in result.output

    def test_resume_hint_matches_run_workflow_contract(self) -> None:
        """The output surfaces `ouroboros run workflow --orchestrator --resume <session_id>`."""
        result = self._invoke_with_sessions("1\n")
        assert "ouroboros run workflow --orchestrator --resume sess-abc123" in result.output


# ---------------------------------------------------------------------------
# Read-only enforcement at the SQLite connection layer
# ---------------------------------------------------------------------------


class TestResumeConnectionIsReadOnly:
    """Pin the core contract: ``resume`` opens the DB in true read-only mode.

    The earlier ``create_schema=False`` guard only skipped schema creation —
    the underlying SQLite connection was still read-write, so a future code
    path (or a library bug) could mutate the user's DB. These tests enforce
    the contract at the connection layer via the
    ``EventStore(..., read_only=True)`` URI form ``mode=ro&uri=true``.
    """

    @pytest.mark.asyncio
    async def test_cannot_insert_through_opened_event_store(self, tmp_path: Path) -> None:
        """Any INSERT against the opened connection must raise OperationalError."""
        # Seed a real on-disk SQLite file with the schema so the read-only
        # connection has something to refuse writes against.
        db_path = tmp_path / "ouroboros.db"
        from sqlalchemy import text

        from ouroboros.persistence.event_store import EventStore

        # Bootstrap schema via a separate RW store, then close it cleanly.
        bootstrap = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await bootstrap.initialize()
        await bootstrap.close()

        event_store = await _get_event_store(str(db_path))
        assert event_store is not None
        try:
            with pytest.raises(OperationalError) as excinfo:
                async with event_store._engine.begin() as conn:  # type: ignore[union-attr]
                    # Raw SQL — we don't care *which* write we attempt, only
                    # that the connection refuses every write. ``DELETE FROM
                    # events`` is trivially valid against the bootstrapped
                    # schema, so a failure here proves the connection itself
                    # is read-only (not a schema mismatch).
                    await conn.execute(text("DELETE FROM events"))
            assert "readonly database" in str(excinfo.value).lower()
        finally:
            await event_store.close()

    @pytest.mark.asyncio
    async def test_database_url_uses_readonly_uri_form(self, tmp_path: Path) -> None:
        """The constructed URL must include ``mode=ro`` and ``uri=true``."""
        db_path = tmp_path / "ouroboros.db"
        from ouroboros.persistence.event_store import EventStore

        bootstrap = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await bootstrap.initialize()
        await bootstrap.close()

        event_store = await _get_event_store(str(db_path))
        assert event_store is not None
        try:
            url = event_store._database_url  # type: ignore[attr-defined]
            assert "mode=ro" in url
            assert "uri=true" in url
        finally:
            await event_store.close()

    @pytest.mark.asyncio
    async def test_raw_sqlite_write_is_blocked(self, tmp_path: Path) -> None:
        """Belt-and-braces: even a raw sqlite3 connect over the URI refuses writes.

        Guards against someone later swapping in a non-aiosqlite driver that
        ignores our connect_args — the URI itself carries ``mode=ro``.
        """
        db_path = tmp_path / "ouroboros.db"
        from ouroboros.persistence.event_store import EventStore

        bootstrap = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await bootstrap.initialize()
        await bootstrap.close()

        event_store = EventStore(
            f"sqlite+aiosqlite:///{db_path}",
            read_only=True,
        )
        try:
            # Extract the ``file:...`` path from the rewritten URL so we can
            # hand it to sqlite3.connect directly, bypassing aiosqlite.
            url = event_store._database_url  # type: ignore[attr-defined]
            prefix = "sqlite+aiosqlite:///"
            assert url.startswith(prefix)
            raw_uri = url[len(prefix) :]

            with sqlite3.connect(raw_uri, uri=True) as conn:
                with pytest.raises(sqlite3.OperationalError) as excinfo:
                    conn.execute("DELETE FROM events")
                assert "readonly" in str(excinfo.value).lower()
        finally:
            await event_store.close()


# ---------------------------------------------------------------------------
# Printed guidance is parseable by the installed CLI
# ---------------------------------------------------------------------------


class TestResumeGuidanceIsCallable:
    """The printed next-step commands must be syntactically accepted by the CLI.

    We don't *execute* the happy path (it would require a real seed file and
    an MCP server), but ``--help`` on the parsed subcommand chain proves that
    the command string is one the installed CLI actually understands — i.e.
    we don't ship guidance that points at a non-existent command again
    (Finding #2).
    """

    def test_tui_monitor_subcommand_chain_is_valid(self) -> None:
        """``ouroboros tui monitor --help`` must succeed."""
        from ouroboros.cli.main import app as root_app

        result = CliRunner().invoke(root_app, ["tui", "monitor", "--help"])
        assert result.exit_code == 0, result.output
        assert "monitor" in result.output.lower() or "tui" in result.output.lower()

    def test_run_workflow_resume_subcommand_chain_is_valid(self) -> None:
        """``ouroboros run workflow --help`` must list ``--resume`` and ``--orchestrator``."""
        from ouroboros.cli.main import app as root_app

        result = CliRunner().invoke(root_app, ["run", "workflow", "--help"])
        assert result.exit_code == 0, result.output
        assert "--resume" in result.output
        assert "--orchestrator" in result.output

    def test_status_execution_is_not_surfaced_as_guidance(self) -> None:
        """``status execution`` is a placeholder — guidance must not point there.

        This pins Finding #2 (the printed re-attach hint used to claim
        ``ouroboros status execution <id>`` but that handler is still
        unimplemented — see src/ouroboros/cli/commands/status.py).
        """
        tracker = _make_tracker()
        assert "status execution" not in _format_reattach_guidance(tracker)
