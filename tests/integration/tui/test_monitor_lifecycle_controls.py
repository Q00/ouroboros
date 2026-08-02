"""Integration coverage for `ouroboros tui monitor` lifecycle controls.

The monitor observes the event store and owns no execution, so it must never
report a pause/resume transition the execution never reached
(Q00/ouroboros#1833). These tests exercise the production `tui monitor` wiring,
the footer bindings a user actually sees, and the durable control path an
embedding caller drives — including the round trip back out of `paused`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.tui import app as tui_app
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.tui.app import OuroborosTUI
from ouroboros.tui.events import PauseRequested

runner = CliRunner()


@pytest.fixture
async def event_store(tmp_path: Path):
    """Real SQLite-backed event store for the durable control path."""
    store = EventStore(sqlite_database_url(tmp_path / "ouroboros.db"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def _drain_control_tasks(action: Callable[[], None]) -> None:
    """Run ``action`` and await the control task its message handler spawns.

    The snapshot and the diff are taken with no ``await`` in between, so the
    difference is exactly what ``action`` created. Only call this on an app that
    is not running under ``run_test`` — a running app owns long-lived tasks that
    would never complete.
    """
    before = asyncio.all_tasks()
    action()
    for task in asyncio.all_tasks() - before:
        await task


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PANEL_CHROME = re.compile(r"[│╭╮╰╯─]")


def _plain(output: str) -> str:
    """Flatten a Rich panel back to a single searchable line."""
    return " ".join(_PANEL_CHROME.sub(" ", _ANSI.sub("", output)).split())


def _launch_monitor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[OuroborosTUI, Any]:
    """Run the production `tui monitor` command and capture its TUI instance."""
    captured: list[OuroborosTUI] = []

    async def fake_run_async(self: OuroborosTUI, *args: Any, **kwargs: Any) -> None:
        captured.append(self)

    monkeypatch.setattr(OuroborosTUI, "run_async", fake_run_async)

    result = runner.invoke(tui_app, ["monitor", "--db-path", str(tmp_path / "ouroboros.db")])

    assert result.exit_code == 0, result.output
    assert captured, "monitor_command did not construct a TUI"
    return captured[0], result


class TestProductionMonitorOwnsNoExecution:
    """`ouroboros tui monitor` must not advertise controls it cannot exercise."""

    def test_monitor_connects_no_execution_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tui, _ = _launch_monitor(monkeypatch, tmp_path)

        assert tui.pause_control_available is False
        assert tui.resume_control_available is False

    def test_monitor_states_that_controls_are_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, result = _launch_monitor(monkeypatch, tmp_path)

        assert "pause/resume are not available here" in _plain(result.output)
        assert "ouroboros cancel execution" in _plain(result.output)

    async def test_footer_does_not_advertise_pause_or_resume(self) -> None:
        """The user-visible surface: `p`/`r` must be absent from the bindings.

        Textual keeps a `None` binding in `active_bindings` (greyed out but
        still shown); only `False` removes it. Asserting on `active_bindings`
        rather than on `check_action` keeps that distinction honest.
        """
        tui = OuroborosTUI()
        async with tui.run_test() as pilot:
            await pilot.pause()

            assert "p" not in tui.screen.active_bindings
            assert "r" not in tui.screen.active_bindings
            assert "q" in tui.screen.active_bindings

    async def test_footer_advertises_controls_once_an_owner_connects(self) -> None:
        tui = OuroborosTUI()
        tui.set_pause_callback(MagicMock())
        tui.set_resume_callback(MagicMock())
        async with tui.run_test() as pilot:
            await pilot.pause()

            assert "p" in tui.screen.active_bindings
            assert "r" in tui.screen.active_bindings

    async def test_pressing_p_without_an_owner_does_not_pause(self) -> None:
        """End-to-end: the key must not fake a lifecycle transition."""
        tui = OuroborosTUI()
        async with tui.run_test() as pilot:
            await pilot.pause()
            tui.set_execution("exec_keypress", "sess_keypress")

            await pilot.press("p")
            await pilot.pause()

            assert tui.state.status == "running"
            assert tui.state.is_paused is False

    async def test_pressing_p_with_an_owner_reaches_the_owner(self) -> None:
        """With an owner connected, `p` forwards the request but does not fake state."""
        requested: list[str] = []

        tui = OuroborosTUI()
        tui.set_pause_callback(requested.append)
        async with tui.run_test() as pilot:
            await pilot.pause()
            tui.set_execution("exec_keypress", "sess_keypress")

            await pilot.press("p")
            await pilot.pause()

            assert requested == ["exec_keypress"]
            # Still no optimistic transition — only an acknowledged event flips it.
            assert tui.state.status == "running"
            assert tui.state.is_paused is False


class TestAcknowledgedDurableControlPath:
    """A connected owner drives the display only via persisted lifecycle events."""

    async def test_pause_reaches_event_store_and_status_follows_acknowledgement(
        self, event_store: EventStore
    ) -> None:
        repository = SessionRepository(event_store)
        created = await repository.create_session(
            execution_id="exec_1833",
            seed_id="seed_1833",
            session_id="sess_1833",
        )
        assert created.is_ok, created.error

        # No event_store on the app: this test drives projection explicitly so the
        # 0.5s poller cannot race the "not yet acknowledged" assertion.
        tui = OuroborosTUI()
        tui.set_execution("exec_1833", "sess_1833")

        async def pause_owner(_execution_id: str) -> None:
            result = await repository.mark_paused(
                "sess_1833",
                reason="Paused from TUI monitor",
            )
            assert result.is_ok, result.error

        tui.set_pause_callback(pause_owner)

        # The request alone must not move the display; the durable control path
        # runs in the task the handler spawns.
        await _drain_control_tasks(lambda: tui.on_pause_requested(PauseRequested("exec_1833")))
        assert tui.state.status == "running"
        assert tui.state.is_paused is False

        events, _ = await event_store.get_recent_aggregate_events(
            "session",
            "sess_1833",
            event_types={"orchestrator.session.paused"},
        )
        assert [event.type for event in events] == ["orchestrator.session.paused"]

        # Only the acknowledged lifecycle event flips the display.
        tui._update_state_from_event(events[0])
        assert tui.state.status == "paused"
        assert tui.state.is_paused is True

    async def test_resume_is_acknowledged_by_progress_runtime_status(
        self, event_store: EventStore
    ) -> None:
        """A paused display must be able to get back to running.

        There is no `orchestrator.session.resumed` event; a resumed run is
        acknowledged by progress carrying `runtime_status`. Without projecting
        it, a monitor that stops writing state optimistically would be stuck on
        `paused` for the rest of the run.
        """
        repository = SessionRepository(event_store)
        created = await repository.create_session(
            execution_id="exec_resume",
            seed_id="seed_resume",
            session_id="sess_resume",
        )
        assert created.is_ok, created.error

        paused = await repository.mark_paused("sess_resume", reason="Paused from TUI monitor")
        assert paused.is_ok, paused.error

        tui = OuroborosTUI()
        tui.set_execution("exec_resume", "sess_resume")

        paused_events, _ = await event_store.get_recent_aggregate_events(
            "session",
            "sess_resume",
            event_types={"orchestrator.session.paused"},
        )
        tui._update_state_from_event(paused_events[0])
        assert tui.state.is_paused is True

        # The execution really resumes and reports it through progress.
        tracked = await repository.track_progress(
            "sess_resume",
            {"runtime_status": "running", "completed_count": 1, "total_count": 3},
        )
        assert tracked.is_ok, tracked.error

        progress_events, _ = await event_store.get_recent_aggregate_events(
            "session",
            "sess_resume",
            event_types={"orchestrator.progress.updated"},
        )
        assert progress_events, "no progress event was persisted"
        for event in progress_events:
            tui._update_state_from_event(event)

        assert tui.state.status == "running"
        assert tui.state.is_paused is False

    async def test_progress_without_runtime_status_does_not_clear_paused(
        self, event_store: EventStore
    ) -> None:
        """Ordinary progress is not a resume acknowledgement."""
        repository = SessionRepository(event_store)
        created = await repository.create_session(
            execution_id="exec_quiet",
            seed_id="seed_quiet",
            session_id="sess_quiet",
        )
        assert created.is_ok, created.error

        tui = OuroborosTUI()
        tui.set_execution("exec_quiet", "sess_quiet")
        tui.state.status = "paused"
        tui.state.is_paused = True

        tracked = await repository.track_progress(
            "sess_quiet", {"completed_count": 1, "total_count": 3}
        )
        assert tracked.is_ok, tracked.error

        progress_events, _ = await event_store.get_recent_aggregate_events(
            "session",
            "sess_quiet",
            event_types={"orchestrator.progress.updated"},
        )
        for event in progress_events:
            tui._update_state_from_event(event)

        assert tui.state.status == "paused"
        assert tui.state.is_paused is True

    async def test_unacknowledged_pause_persists_nothing_and_keeps_running(
        self, event_store: EventStore
    ) -> None:
        repository = SessionRepository(event_store)
        created = await repository.create_session(
            execution_id="exec_noack",
            seed_id="seed_noack",
            session_id="sess_noack",
        )
        assert created.is_ok, created.error

        tui = OuroborosTUI()
        tui.set_execution("exec_noack", "sess_noack")

        async def rejecting_owner(_execution_id: str) -> None:
            raise RuntimeError("owner refused the pause")

        tui.set_pause_callback(rejecting_owner)
        await _drain_control_tasks(lambda: tui.on_pause_requested(PauseRequested("exec_noack")))

        events, _ = await event_store.get_recent_aggregate_events(
            "session",
            "sess_noack",
            event_types={"orchestrator.session.paused"},
        )
        assert events == []
        assert tui.state.status == "running"
        assert tui.state.is_paused is False
        assert tui.state.logs[-1]["level"] == "error"
